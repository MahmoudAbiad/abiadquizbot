import asyncio
from task_utils import spawn
from typing import Optional
from urllib.parse import urlencode
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from config import bot, QuizState
from keyboards import get_main_menu_keyboard
from helpers.supabase_helper import (
    check_or_add_user, get_shared_quiz, get_favorite_quiz_by_global_id, 
    supabase, log_usage_event, mark_quiz_attempt_stopped
)
from logger import get_logger, log_info
from handlers.quiz_runner import start_loaded_quiz
from constants import ADMIN_CONTACT, SUPPORT_BOT_URL, OFFICIAL_CHANNEL_URL
from utils import safe_title
from helpers.student_ui import UPLOAD_TEXT, HELP_TEXT, HOME_TEXT, welcome_text as build_welcome_text
from helpers.supabase_helper.gifts import gift_balances

logger = get_logger(__name__)
router = Router()
# 🩹 FIX (تسريب خصوصية): هاي الأوامر (start/support/channel) بترد بمعلومات شخصية
# (رصيد النقاط، رابط الإحالة...) بنفس المحادثة اللي انكتب فيها الأمر. بلا هالفلتر،
# أي عضو بأي غروب مضاف فيه البوت كان يقدر يكتبها ويكشف بياناته الخاصة للجميع هناك
# (حتى لو ما ظهرت بقائمة الأوامر الزرقاء بالغروب بعد تعديل config.py::set_bot_commands -
# الفلتر هون هو الحماية الفعلية، مش مجرد إخفاء القائمة). نفس نمط الفلتر المستخدم
# أصلاً بـ group_quiz_router (راجع handlers/group_quiz.py).
router.message.filter(F.chat.type == "private")

# 🆕 مصادر الوصول المسموحة القادمة من روابط خارجية عبر /start <source>، مثال: الموقع التعريفي
# abiad.me يستخدم t.me/AbiadQuizMakerbot?start=website. قائمة بيضاء صريحة عن قصد حتى لا
# يُخزَّن أي نص عشوائي بالتحليلات. هذه القيم ليست أرقاماً ولا تبدأ بـ share_/quiz_ فلا تتداخل
# مع الإحالات ولا الروابط العميقة، وتُسجَّل فقط ضمن حدث bot_start بدون أي تغيير على منطق التسجيل.
ALLOWED_START_SOURCES = {"website"}

async def launch_deep_linked_quiz(target_msg: types.Message, state: FSMContext, args_payload: str):
    """الماكينة المسؤولة عن تحميل وتشغيل الكويز القادم من الروابط العميقة بأمان"""
    actual_quiz_id = None
    shared = None
    
    if args_payload.startswith("share_"):
        share_id = args_payload.replace("share_", "", 1)
        shared = await get_shared_quiz(share_id)
        if shared:
            # استخراج الـ UUID المركزي الحقيقي للكويز المشترك
            actual_quiz_id = shared.get("id")
    else:
        share_id = args_payload.replace("quiz_", "", 1)
        shared = await get_favorite_quiz_by_global_id(share_id)
        if not shared:
            # فولباك في حال كان الرابط قديماً أو مشتركاً مباشرة
            shared = await get_shared_quiz(share_id)
            if shared:
                actual_quiz_id = shared.get("id")
        else:
            # جلب الـ UUID المركزي الحقيقي المرتبط بجدول المفضلة لضمان عدم انهيار جدول العلامات
            fav_res = await supabase.table("favorite_quizzes").select("quiz_id").eq("favorite_id", share_id).execute()
            if fav_res.data:
                actual_quiz_id = fav_res.data[0]["quiz_id"]
            
    if shared and actual_quiz_id:
        # 🆕 لو المستخدم جديد كلياً فهو انضم عبر هذا الرابط المشترك: نمرّر المصدر والكويز
        # ليُخزَّنا ذرياً مع إنشاء الحساب (لا أثر على المستخدمين الحاليين).
        link_user_info = await check_or_add_user(
            target_msg.from_user.id,
            target_msg.from_user.username or "Unknown",
            target_msg.from_user.first_name,
            target_msg.from_user.last_name or "Unknown",
            None,
            join_source="shared_link",
            join_quiz_id=str(actual_quiz_id),
        )
        is_new_via_link = link_user_info.get("status") == "new"
        quiz_title = shared.get('title') or shared.get('source_title') or 'كويز مشترك'

        spawn(log_usage_event(target_msg.from_user.id, "shared_link_opened", {
            "payload_type": "share" if args_payload.startswith("share_") else "quiz",
            "quiz_id": str(actual_quiz_id),
            "is_new_user": is_new_via_link,
        }))

        await target_msg.answer(f"🚀 جاري تحميل الاختبار المشترك: <b>{safe_title(quiz_title)}</b>...", parse_mode="HTML")
        from handlers.runner import launch_keyboard
        from services.runner_core import build_questions
        try:
            build_questions(shared["quiz_data"])
            game_keyboard = launch_keyboard(actual_quiz_id, target_msg.from_user.id)
            if game_keyboard:
                await target_msg.answer("🏃 هذا الاختبار متاح أيضاً كلعبة ركض:", reply_markup=game_keyboard)
        except ValueError:
            pass
        await start_loaded_quiz(
            target_msg, state, shared["quiz_data"], 
            quiz_title, 
            origin="shared", 
            quiz_id=str(actual_quiz_id) # تمرير الـ UUID المركزي الحقيقي الصالح لجدول السكور والتقييمات
        )
    else:
        await target_msg.answer("❌ عذراً، هذا الرابط منتهي الصلاحية أو لم يعد موجوداً في قاعدة البيانات.")


@router.message(Command("start"))
async def start(msg: types.Message, command: CommandObject, state: FSMContext):
    try:
        bot_info = await bot.get_me()
        
        # حماية التدفق: التحقق من وجود كويز معلق قادم من رابط عميق
        if command.args and (command.args.startswith("share_") or command.args.startswith("quiz_")):
            await launch_deep_linked_quiz(msg, state, command.args)
            return

        # Extract referrer ID from command arguments
        referrer_id: Optional[int] = None
        if command.args and command.args.isdigit():
            referrer_id = int(command.args)
            log_info(logger, f"User {msg.from_user.id} joined via referral from {referrer_id}")
        
        # Check or create user in database بشكل مباشر دون استهلاك خيوط
        user_info = await check_or_add_user(
            msg.from_user.id,
            msg.from_user.username or "Unknown",
            msg.from_user.first_name,
            msg.from_user.last_name or "Unknown",
            referrer_id,
            join_source="website" if command.args == "website" else None,
        )
        
        points = user_info["points"]
        free_points = user_info.get("free_points", 0)
        paid_points = user_info.get("paid_points", 0)
        gift_points = user_info.get("gift_points", 0)
        status = user_info["status"]

        # 🆕 تسجيل حدث بدء تشغيل البوت لتتبع نمط عودة/تجدد الطلاب
        start_source = command.args if command.args in ALLOWED_START_SOURCES else None
        bot_start_meta = {
            "status": status,
            "has_referrer": bool(referrer_id),
        }
        if start_source:
            bot_start_meta["source"] = start_source
        spawn(log_usage_event(msg.from_user.id, "bot_start", bot_start_meta))
        
        # 🆕 (2026-08-28) إلغاء دليل الـ5 خطوات نهائياً بناءً على طلب صريح من المستخدم:
        # لا نفع حقيقي كان يُضاف من الشرح المطوّل - استبدلناه برسالة ترحيب من سطرين
        # فقط + زر إجراء واحد بارز يقود مباشرة لصلب الاستخدام (إرسال ملف). الضغط على
        # الزر لا يفعل شيئاً "سحرياً" (لأن الفعل الحقيقي هو إرسال المستخدم لملف/نص من
        # طرفه) لكنه يُستخدم فقط لتأكيد الفهم وإظهار القائمة الرئيسية بعدها مباشرة -
        # حتى لا يبقى المستخدم بلا أي أزرار وصول (شحن رصيد/مفضلة/دعم) إلى أن يكتب
        # /start يدوياً من جديد (نفس المبدأ اللي كان بمعالج tut_finish القديم بالدليل
        # المحذوف الآن بالكامل - راجع docs/HISTORY_LOG.md لتفاصيل الإزالة).
        if status == "new":
            intro_text = (
                "👋 <b>أهلاً بك في أبيض منشئ الكويزات</b>\n\n"
                "حوّل ملفاتك وصورك ونصوصك إلى أسئلة تفاعلية للمراجعة.\n"
                f"🎁 رصيد هديتك: <b>{gift_points:g} نقطة</b>.\n\n"
                "ابدأ بإرسال محتواك، وراجع التكلفة قبل تأكيد التوليد."
            )
            start_kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📝 إنشاء أول كويز", callback_data="new_user_send_now")],
                [types.InlineKeyboardButton(text="🎁 صلاحية هديتي", callback_data="gift:list:0")]
            ])

            await msg.answer(intro_text, parse_mode="HTML", reply_markup=start_kb)
            log_info(logger, f"User {msg.from_user.id} started bot. Status: new, Points: {points}")
            return

        welcome_text = build_welcome_text(points, free_points, gift_points, paid_points, status == "renewed")
        
        await msg.answer(
            welcome_text,
            parse_mode="HTML",
            reply_markup=await get_main_menu_keyboard(bot_info.username, msg.from_user.id)
        )
        log_info(logger, f"User {msg.from_user.id} started bot. Status: {status}, Points: {points}")
        
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await msg.answer(
            "⚠️ <b>عذراً، واجهنا مشكلة بسيطة أثناء تجهيز حسابك.</b>\n"
            "يرجى محاولة الضغط على /start مرة أخرى بعد قليل.",
            parse_mode="HTML"
        )

# ==================== معالجات تأكيد/إلغاء دهس الكويزات النشطة ====================

@router.callback_query(F.data == "deep_link_overwrite_confirm")
async def handle_deep_link_overwrite_confirm(call: types.CallbackQuery, state: FSMContext):
    await call.answer("هذا زر قديم. افتح رابط الاختبار مجددًا لاختيار الانتقال بأمان.", show_alert=True)

@router.callback_query(F.data == "deep_link_overwrite_cancel")
async def handle_deep_link_overwrite_cancel(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.update_data(interrupted_deep_link=None)
        await call.message.delete()
        await call.message.answer("🔄 تم الحفاظ على اختبارك الحالي بنجاح. يمكنك متابعة الإجابة دون أي قلق!")
    except Exception as e:
        logger.error(f"Error in deep_link_overwrite_cancel: {e}")
    finally:
        await call.answer()

@router.callback_query(F.data == "new_user_send_now")
async def ack_new_user_start(call: types.CallbackQuery):
    """تأكيد بسيط بعد ضغط زر «أرسل أول ملف الآن» بترحيب المستخدم الجديد - لا محتوى
    تعليمي هنا عمداً (الدليل القديم حُذف بالكامل).
    🆕 (2026-08-28) لم تعد القائمة الرئيسية تظهر هنا: أُجِّل ظهورها عمداً إلى ما بعد
    أول اختبار كامل ينهيه هذا المستخدم فعلياً (راجع
    handlers/quiz_runner.py::_handle_quiz_completion) - بحيث لا يرى مستخدم جديد أي
    أزرار/خيارات إضافية قبل أن يجرّب صلب المنتج فعلياً ويحصل على أول نتيجة ملموسة."""
    try:
        await call.message.edit_text(
            UPLOAD_TEXT,
            parse_mode="HTML",
        )
        spawn(log_usage_event(call.from_user.id, "start_cta_acknowledged"))
    except Exception as e:
        logger.error(f"Error in ack_new_user_start: {e}")
    finally:
        await call.answer()


def _student_info_keyboard():
    from constants import WEBAPP_PUBLIC_BASE_URL
    rows = []
    if WEBAPP_PUBLIC_BASE_URL:
        rows.append([types.InlineKeyboardButton(text="📤 رفع ملف كبير أو محاضرة", callback_data="upload_open:all")])
    rows.append([types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="student_home"),
                 types.InlineKeyboardButton(text="💬 الدعم", url=SUPPORT_BOT_URL)])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _student_home_text(user_id: int) -> str:
    """Rebuild the home copy with the current wallet without modifying user/FSM state."""
    try:
        result = await supabase.table("users").select("free_points,paid_points").eq("user_id", user_id).limit(1).execute()
        if not result.data:
            return HOME_TEXT
        wallet = result.data[0]
        gifts = await gift_balances([user_id])
        free = float(wallet.get("free_points") or 0)
        paid = float(wallet.get("paid_points") or 0)
        gift = float(gifts.get(user_id, 0))
        return build_welcome_text(free + paid + gift, free, gift, paid)
    except Exception as exc:
        logger.warning(f"Could not refresh student home balance: {exc}")
        return HOME_TEXT


async def _show_student_navigation_screen(call: types.CallbackQuery, text: str, reply_markup):
    """Navigate within one Telegram message; fall back only if editing is unavailable."""
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    except TelegramBadRequest as exc:
        # Repeated taps on an already-open screen must not create extra messages.
        if "message is not modified" in str(exc).lower():
            return
        logger.warning(f"Could not edit student navigation message: {exc}")
    except (AttributeError, TypeError) as exc:
        # Old/inaccessible callback messages may not support edit_text.
        logger.warning(f"Student navigation message is not editable: {exc}")

    # Fallback for messages that Telegram no longer allows us to edit.
    send = getattr(call.message, "answer", None)
    if callable(send):
        await send(text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await bot.send_message(call.from_user.id, text, parse_mode="HTML", reply_markup=reply_markup)


@router.callback_query(F.data.in_({"student_create_quiz", "student_help", "student_home"}), F.message.chat.type == "private")
async def student_navigation(call: types.CallbackQuery):
    # Informational screens never clear FSM or replace an ongoing quiz/upload.
    await call.answer()
    if call.data == "student_home":
        info = await bot.get_me()
        menu = await get_main_menu_keyboard(info.username, call.from_user.id)
        await _show_student_navigation_screen(call, await _student_home_text(call.from_user.id), menu)
    else:
        content = UPLOAD_TEXT if call.data == "student_create_quiz" else HELP_TEXT
        await _show_student_navigation_screen(call, content, _student_info_keyboard())


@router.message(Command("help"))
async def student_help(msg: types.Message):
    await msg.answer(HELP_TEXT, parse_mode="HTML", reply_markup=_student_info_keyboard())


@router.callback_query(F.data == "recharge_info")
async def show_recharge_info(call: types.CallbackQuery):
    try:
        # 💳 الشحن الذاتي (فلاغ payments_enabled + باقات نشطة بجدول payment_packages). لو غير متاح
        # نكمل بشاشة الباقات اليدوية (RECHARGE_PACKAGES) أدناه عبر الدعم.
        from handlers.payments import try_show_payment_packages
        if await try_show_payment_packages(call):
            return
        recharge_text = (
            "🔋 <b>شحن النقاط</b>\n\n"
            "اختر الباقة المطلوبة، ثم اضغط «تواصل للشحن» لمعرفة طريقة الدفع.\n\n"
            "💵 الأسعار بالليرة السورية."
        )
        await call.message.answer(
            recharge_text, parse_mode="HTML", reply_markup=get_recharge_keyboard()
        )
        log_info(logger, f"User {call.from_user.id} requested recharge info")
        
        # تسجيل حدث طلب معلومات الشحن
        spawn(log_usage_event(call.from_user.id, "recharge_info_viewed"))
    except Exception as e:
        logger.error(f"Error in show_recharge_info: {e}")
    finally:
        await call.answer()

RECHARGE_PACKAGES = {"200": "65", "500": "130", "1000": "260"}


def get_recharge_keyboard(selected_package: Optional[str] = None):
    """Keep the selected package in this message's keyboard, without changing quiz state."""
    rows = [
        [types.InlineKeyboardButton(
            text=f"{'✅' if points == selected_package else '◯'} {points} نقطة — {price} ل.س",
            callback_data=f"recharge_package:{points}",
        )]
        for points, price in RECHARGE_PACKAGES.items()
    ]
    if selected_package in RECHARGE_PACKAGES:
        price = RECHARGE_PACKAGES[selected_package]
        draft = (
            f"مرحباً، أرغب بشحن باقة {selected_package} نقطة مقابل {price} ل.س. ما طريقة الدفع؟"
        )
        contact_button = types.InlineKeyboardButton(
            text="💬 تواصل للشحن",
            url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}?{urlencode({'text': draft})}",
        )
    else:
        contact_button = types.InlineKeyboardButton(
            text="💬 تواصل للشحن", callback_data="recharge_choose_package"
        )
    rows.append([contact_button])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "recharge_choose_package")
async def prompt_recharge_package(call: types.CallbackQuery):
    await call.answer("اختر الباقة المطلوبة أولاً 👆", show_alert=True)


@router.callback_query(F.data.startswith("recharge_package:"))
async def select_recharge_package(call: types.CallbackQuery):
    package = call.data.split(":", 1)[1]
    if package not in RECHARGE_PACKAGES:
        await call.answer("هذه الباقة غير متاحة. افتح قائمة الشحن مجدداً.", show_alert=True)
        return
    try:
        keyboard = get_recharge_keyboard(package)
        if call.message.reply_markup != keyboard:
            await call.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest as e:
        # Repeated taps can reach Telegram after the same markup was already applied.
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error in select_recharge_package: {e}")
    except Exception as e:
        logger.error(f"Error in select_recharge_package: {e}")
    finally:
        await call.answer()

async def set_bot_commands(bot):
    # ملاحظة: هذه نسخة مستقلة تُستخدم فقط في وضع Polling المحلي (main.py)؛
    # النسخة الفعلية المستخدمة على Railway/الويبهوك هي config.py::set_bot_commands
    commands = [
        types.BotCommand(command="wallet", description="رصيدي وهداياي وصلاحيتها"),
        types.BotCommand(command="start", description="تشغيل البوت والتحقق من الرصيد"),
        types.BotCommand(command="favorites", description="⭐ قائمتي المفضلة المنظمة"),
    ]
    await bot.set_my_commands(commands)

@router.message(Command("support"))
async def cmd_support(message: types.Message):
    """معالج أمر الدعم الفني من القائمة الزرقاء"""
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💬 فتح محادثة الدعم الفني", url=SUPPORT_BOT_URL)]
    ])
    
    text = (
        "🛠️ <b>قسم الدعم الفني والمساعدة</b>\n\n"
        "هل تواجه مشكلة، أو لديك استفسار بشأن الكويزات أو النقاط؟\n"
        "يمكنك التواصل مباشرة مع فريق الدعم عبر بوت الدعم المخصص للرد على استفساراتكم 👇"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    
    # تسجيل حدث فتح الدعم الفني
    spawn(log_usage_event(message.from_user.id, "support_opened"))


@router.message(Command("channel"))
async def cmd_channel(message: types.Message):
    """معالج أمر قناة التحديثات"""
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📢 الانضمام لقناة الأخبار والتحديثات", url=OFFICIAL_CHANNEL_URL)]
    ])
    
    text = (
        "📢 <b>القناة الرسمية للبوت</b>\n\n"
        "تابِع قناتنا الرسمية ليصلك كل جديد من:\n"
        "• التحديثات والمميزات الجديدة ✨\n"
        "• العروض واكواد شحن النقاط المجانية 🎁\n"
        "• التنبيهات والصيانة المجدولة 🛠️\n\n"
        "اضغط على الزر أدناه للانضمام 👇"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    
    # تسجيل حدث فتح القناة
    spawn(log_usage_event(message.from_user.id, "channel_opened"))


