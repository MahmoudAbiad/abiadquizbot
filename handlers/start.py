import asyncio
from typing import Optional
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from config import bot, QuizState
from keyboards import get_main_menu_keyboard
from supabase_helper import (
    check_or_add_user, get_shared_quiz, get_favorite_quiz_by_global_id, 
    supabase, log_usage_event, mark_quiz_attempt_stopped
)
from logger import get_logger, log_info
from constants import ADMIN_CONTACT, SUPPORT_BOT_URL, OFFICIAL_CHANNEL_URL
from settings_helper import get_setting

logger = get_logger(__name__)
router = Router()

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
        await check_or_add_user(
            target_msg.from_user.id,
            target_msg.from_user.username or "Unknown",
            target_msg.from_user.first_name,
            target_msg.from_user.last_name or "Unknown",
            None
        )
        from handlers.quiz_runner import _start_loaded_quiz
        quiz_title = shared.get('title') or shared.get('source_title') or 'كويز مشترك'

        asyncio.create_task(log_usage_event(target_msg.from_user.id, "shared_link_opened", {
            "payload_type": "share" if args_payload.startswith("share_") else "quiz",
        }))

        await target_msg.answer(f"🚀 جاري تحميل الاختبار المشترك: <b>{quiz_title}</b>...", parse_mode="HTML")
        await _start_loaded_quiz(
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
            current_state = await state.get_state()
            
            # إذا كان المستخدم يحل اختباراً حالياً، نمنع دهس البيانات ونطلب تأكيده بوضوح
            if current_state == QuizState.answering_quiz:
                await state.update_data(interrupted_deep_link=command.args)
                overwrite_kb = types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="⚠️ نعم، ألغِ الحالي وافتح الجديد", callback_data="deep_link_overwrite_confirm")],
                    [types.InlineKeyboardButton(text="🔄 لا، أريد إكمال اختباري الحالي", callback_data="deep_link_overwrite_cancel")]
                ])
                await msg.answer(
                    "⚠️ <b>تنبيه حرج!</b>\n\n"
                    "أنت تقوم بحل اختبار نشط حالياً. الضغط على الرابط سيؤدي إلى خسارة تقدمك الحالي وإلغاء الكويز.\n"
                    "هل أنت متأكد من رغبتك في إنهاء الاختبار الحالي وفتح الرابط الجديد؟",
                    reply_markup=overwrite_kb,
                    parse_mode="HTML"
                )
                return
            
            # إذا كانت حالته نظيفة، يتم تشغيل الكويز مباشرة
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
            referrer_id
        )
        
        points = user_info["points"]
        free_points = user_info.get("free_points", 0)
        paid_points = user_info.get("paid_points", 0)
        status = user_info["status"]

        # 🆕 تسجيل حدث بدء تشغيل البوت لتتبع نمط عودة/تجدد الطلاب
        asyncio.create_task(log_usage_event(msg.from_user.id, "bot_start", {
            "status": status,
            "has_referrer": bool(referrer_id),
        }))
        
        # 🆕 (2026-08-28) إلغاء دليل الـ5 خطوات نهائياً بناءً على طلب صريح من المستخدم:
        # لا نفع حقيقي كان يُضاف من الشرح المطوّل - استبدلناه برسالة ترحيب من سطرين
        # فقط + زر إجراء واحد بارز يقود مباشرة لصلب الاستخدام (إرسال ملف). الضغط على
        # الزر لا يفعل شيئاً "سحرياً" (لأن الفعل الحقيقي هو إرسال المستخدم لملف/نص من
        # طرفه) لكنه يُستخدم فقط لتأكيد الفهم وإظهار القائمة الرئيسية بعدها مباشرة -
        # حتى لا يبقى المستخدم بلا أي أزرار وصول (شحن رصيد/مفضلة/دعم) إلى أن يكتب
        # /start يدوياً من جديد (نفس المبدأ اللي كان بمعالج tut_finish القديم بالدليل
        # المحذوف الآن بالكامل - راجع HISTORY_LOG.md لتفاصيل الإزالة).
        if status == "new":
            intro_text = (
                "👋 <b>أهلاً بك في بوت الكويزات الذكي!</b>\n"
                f"🎁 أضفنا لحسابك <b>{points} نقطة مجانية</b> — استخدمها الآن لتوليد أول اختبار لك 🚀"
            )
            start_kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🚀 أرسل أول ملف الآن", callback_data="new_user_send_now")]
            ])

            await msg.answer(intro_text, parse_mode="HTML", reply_markup=start_kb)
            log_info(logger, f"User {msg.from_user.id} started bot. Status: new, Points: {points}")
            return

        if status == "renewed":
            welcome_text = (
                "☀️ <b>يا أهلاً، يومك سعيد!</b>\n\n"
                f"دائماً معك في رحلتك الدراسية.. تم تجديد رصيدك اليومي وإضافة <b>{free_points:.0f} نقطة مجانية جديدة</b> لحسابك. 🔄\n"
            )
        else:
            welcome_text = "👋 <b>يا مرحباً بك مجدداً!</b>\n جاهز لاختبار جديد اليوم؟ ✍️\n"
        
        # 📌 نسخة مختصرة وقابلة للمسح البصري (Scannable): سطر واحد للبدء + صندوق مضغوط
        # للتكلفة والرصيد. الشرح التفصيلي لخطوات الاستخدام انتقل بالكامل إلى الدليل
        # التفاعلي (/help أو زر «كيف يعمل البوت؟») لتفادي إغراق المستخدم بجدار نصوص.
        welcome_text += (
            f"\n📥 <b>ابدأ الآن:</b> أرسل ملف (PDF/Word/PPT/TXT)، صورة، أو نصاً مباشراً — وحدد عدد الأسئلة. ذلك كل شيء! 🔥\n\n"
            f"💰 <code>1</code> نقطة تقريباً لكل صفحة/سؤال، وخصم <code>90%</code> عند إعادة استخدام كويز جاهز.\n"
            f"📊 رصيدك: <code>{points:.2f}</code> نقطة (🎁 {free_points:.2f} · 💳 {paid_points:.2f})\n\n"
            f"🎯 شارك رابط دعوتك مع زملائك واكسب نقاطاً إضافية مع كل مشترك جديد! 🎁\n"
            f"❓ محتاج شرح أوضح بالخطوات؟ اضغط 🎬 «كيف يعمل البوت؟» بالأسفل، أو أرسل /help بأي وقت."
        )
        
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
    try:
        data = await state.get_data()
        payload = data.get("interrupted_deep_link")
        
        # إصلاح التتبع: إغلاق محاولة الكويز القائم قبل مسح الحالة كي لا تبقى معلقة بالداتابيز
        if data.get("attempt_id"):
            asyncio.create_task(mark_quiz_attempt_stopped(data["attempt_id"]))

        await state.clear()
        if payload:
            await call.message.delete()
            await launch_deep_linked_quiz(call.message, state, payload)
        else:
            await call.answer("❌ عذراً، انتهت صلاحية هذا الطلب.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in deep_link_overwrite_confirm: {e}")
    finally:
        await call.answer()

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
            "📥 تمام! أرسل الآن ملفك (PDF/Word/PPT/TXT)، صورة، أو نصاً مباشرة في هذه المحادثة 🔥",
            parse_mode="HTML",
        )
        asyncio.create_task(log_usage_event(call.from_user.id, "start_cta_acknowledged"))
    except Exception as e:
        logger.error(f"Error in ack_new_user_start: {e}")
    finally:
        await call.answer()


@router.callback_query(F.data == "recharge_info")
async def show_recharge_info(call: types.CallbackQuery):
    try:
        daily_renewal_points = await get_setting("daily_renewal_points")
        recharge_text = (
            "🔋 <b>شحن النقاط وزيادة الرصيد</b>\n\n"
            "هل استهلكت نقاطك المجانية وتحتاج للمزيد? لا تقلق! "
            "يمكنك شحن رصيدك بكميات مخصصة لتوليد اختبارات بلا حدود والتحضير للامتحانات بكل راحة. 📚\n\n"
            # 🩹 UX: نذكّر بالبديل المجاني قبل الدفع مباشرة — الشحن مفيد فقط لمن يحتاج
            # نقاطاً أكثر الآن، لا لمن يستطيع الانتظار ليوم واحد فقط.
            f"💡 <b>تذكير:</b> نقاطك المجانية تتجدد تلقائياً كل يوم بـ <b>{daily_renewal_points:.0f} نقطة</b> "
            "بدون أي مقابل — الشحن اختياري فقط إن كنت بحاجة الى نقاط إضافية الآن.\n\n"
            "لطلب الشحن، كل ما عليك هو التواصل مباشرة مع الدعم والإدارة عبر الرابط التالي:\n"
            f"👉 <b>{ADMIN_CONTACT}</b>\n\n"
            "📝 <b>طريقة الشحن:</b>\n"
            "1️⃣ تواصل معنا وحدد عدد النقاط التي تحتاجها.\n"
            "2️⃣ سيتم تأكيد وتفعيل النقاط في حسابك فوراً.\n"
            "3️⃣ يمكنك العودة ومتابعة المذاكرة وتوليد الكويزات دون أي انقطاع! 🚀\n"
            "<b>الاسعار تبدأ من 0.5$ فقط ! 💰</b>\n\n"
        )
        await call.message.answer(recharge_text, parse_mode="HTML")
        log_info(logger, f"User {call.from_user.id} requested recharge info")
        
        # تسجيل حدث طلب معلومات الشحن
        asyncio.create_task(log_usage_event(call.from_user.id, "recharge_info_viewed"))
    except Exception as e:
        logger.error(f"Error in show_recharge_info: {e}")
    finally:
        await call.answer()

async def set_bot_commands(bot):
    # ملاحظة: هذه نسخة مستقلة تُستخدم فقط في وضع Polling المحلي (main.py)؛
    # النسخة الفعلية المستخدمة على Railway/الويبهوك هي config.py::set_bot_commands
    commands = [
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
    asyncio.create_task(log_usage_event(message.from_user.id, "support_opened"))


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
    asyncio.create_task(log_usage_event(message.from_user.id, "channel_opened"))