"""
handlers/study_schedule.py
==============================================================================
معالج إنشاء الجداول الدراسية وتنظيم الوقت.
يتيح للطلاب إنشاء جداول أسبوعية أو خطط سنوية كاملة للمناهج السورية (علمي/أدبي)
ولطلاب الجامعات بمختلف التخصصات، مع تصدير بصور فائقة النقاء وملفات PDF
بأربعة تصاميم وسمات لونية مستوحاة من أحدث اتجاهات التصميم العالمية.
==============================================================================
"""

from typing import Optional
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.fsm.context import FSMContext

from logger import get_logger, log_info, log_error
from keyboards import (
    get_study_schedule_menu_keyboard,
    get_study_schedule_categories_keyboard,
    get_study_schedule_university_tracks_keyboard,
    get_study_schedule_theme_keyboard,
    get_study_schedule_result_keyboard,
)
from services.study_schedule_data import (
    CATEGORIES,
    CATEGORY_BAC_SCI,
    CATEGORY_BAC_LIT,
    CATEGORY_UNIV,
    SCHEDULE_TYPE_WEEKLY,
    SCHEDULE_TYPE_ANNUAL,
    UNIVERSITY_TRACKS,
    TRACK_GENERAL,
    THEMES,
    DEFAULT_THEME,
    BAC_SCI_WEEKLY,
    BAC_SCI_ANNUAL,
    BAC_LIT_WEEKLY,
    BAC_LIT_ANNUAL,
    UNIV_ANNUAL,
    get_univ_weekly,
)
from services.study_schedule_renderer import (
    render_weekly_schedule_async,
    render_annual_schedule_async,
)

logger = get_logger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")


# ==============================================================================
# نصوص الشاشات
# ==============================================================================

SCHEDULE_MAIN_TEXT = """📅 <b>صانع الجداول الدراسية وتنظيم الوقت الذكي</b>

مرحباً بك! تنظيم وقتك هو الخطوة الأولى والأهم نحو التفوق ونيل أعلى الدرجات.
اختر نوع البرنامج الذي ترغب في تصميمه وتصديره:

🗓️ <b>جدول أسبوعي («ماذا يجب أن أدرس خلال الأسبوع»):</b>
توزيع ذكي ومتوازن لكل يوم (فترات صباحية، بعد الظهر، مسائية، واستراحة أسبوعية)، مع خانات متابعة المهام.

🏛️ <b>جدول لكامل العام (البرنامج السنوي الشامل):</b>
خطة استراتيجية مقسمة إلى 4 مراحل زمنية أساسية مخصصة للمناهج السورية (بكالوريا علمي وأدبي) ومسارات الجامعات، مع أسرار سلالم التصحيح الوزارية.

👇 <b>اختر نوع الجدول للبدء:</b>"""

SCHEDULE_CAT_TEXT = """🎯 <b>اختر الفئة الدراسية المطلوبة:</b>

نوفر برامج مدروسة ومخصصة لكل فئة:
🔬 <b>بكالوريا علمي (سوريا):</b> تركيز على الرياضيات، الفيزياء، الكيمياء، العلوم، واللغات.
📚 <b>بكالوريا أدبي (سوريا):</b> استراتيجيات التكرار المتباعد للفلسفة، التاريخ، الجغرافيا، والعربي.
🎓 <b>طلاب الجامعة (مخصص):</b> برامج للكليات الطبية، الهندسية، الاقتصادية والنظرية.

👇 <b>اختر فئتك الدراسية:</b>"""

SCHEDULE_UNIV_TEXT = """🎓 <b>اختر تخصصك الجامعي:</b>

لكل تخصص طبيعة دراسية مختلفة؛ اختر التخصص الأقرب لك لتوليد جدول يتناسب تماماً مع متطلبات مقرراتك:

🩺 <b>الكليات الطبية والصحية:</b> طب بشري، أسنان، صيدلة، علوم صحية
⚙️ <b>الكليات الهندسية والتقنية:</b> معلوماتية، مدني، عمارة، ميكانيك، اتصالات
⚖️ <b>الكليات الاقتصادية والنظرية:</b> اقتصاد، إدارة، حقوق، آداب، لغات
📖 <b>برنامج جامعي عام:</b> خطة مرنة متوازنة تناسب جميع الاختصاصات

👇 <b>اضغط على تخصصك:</b>"""

SCHEDULE_THEME_TEXT = """🎨 <b>اختر التصميم والشكل اللوني المفضل لديك:</b>

تتوفر تصاميم عصرية مستوحاة من أحدث قوالب التصميم العالمية، مصممة بخطوط عربية أصيلة:

🤍 <b>عصري مينيمال:</b> تصميم نقي ومريح جداً للعين ومثالي للطباعة.
👑 <b>أكاديمي ملكي:</b> فخامة كحلية مع لمسات ذهبية للشعور بالانضباط والجدية.
🌸 <b>باستيل ستاديغرام:</b> ألوان هادئة ومبهجة تشجع على التركيز والدراسة.
🌙 <b>الوضع الليلي:</b> مظهر داكن أنيق جداً لشاشات الهواتف والسهر.

👇 <b>اضغط على السمة التي تفضلها لتوليد الجدول فوراً:</b>"""


# ==============================================================================
# معالجات الأوامر والأزرار
# ==============================================================================

@router.message(Command("schedule", "study_plan", "jadwal", "table"))
async def cmd_schedule_entry(message: types.Message):
    """أمر مباشر لفتح صانع الجداول الدراسية."""
    await message.answer(SCHEDULE_MAIN_TEXT, parse_mode="HTML", reply_markup=get_study_schedule_menu_keyboard())


@router.callback_query(F.data == "study_sched:menu")
async def handle_schedule_main_menu(call: types.CallbackQuery):
    """عرض القائمة الرئيسية لاختيار نوع الجدول."""
    await call.answer()
    if call.message:
        try:
            await call.message.edit_text(SCHEDULE_MAIN_TEXT, parse_mode="HTML", reply_markup=get_study_schedule_menu_keyboard())
        except Exception:
            await call.message.answer(SCHEDULE_MAIN_TEXT, parse_mode="HTML", reply_markup=get_study_schedule_menu_keyboard())


@router.callback_query(F.data.startswith("study_sched:type:"))
async def handle_schedule_type_selection(call: types.CallbackQuery):
    """اختيار نوع الجدول (أسبوعي أم سنوي)."""
    await call.answer()
    parts = call.data.split(":")
    sched_type = parts[2] if len(parts) > 2 else SCHEDULE_TYPE_WEEKLY

    type_title = "🗓️ الجدول الأسبوعي" if sched_type == SCHEDULE_TYPE_WEEKLY else "🏛️ البرنامج السنوي الشامل"
    text = f"{SCHEDULE_CAT_TEXT}\n\n<i>النوع المختار: {type_title}</i>"

    if call.message:
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_study_schedule_categories_keyboard(sched_type))
        except Exception:
            await call.message.answer(text, parse_mode="HTML", reply_markup=get_study_schedule_categories_keyboard(sched_type))


@router.callback_query(F.data.startswith("study_sched:cat:"))
async def handle_category_selection(call: types.CallbackQuery):
    """اختيار الفئة الدراسية أو الانتقال لاختيار التخصص الجامعي/السمة."""
    await call.answer()
    parts = call.data.split(":")
    if len(parts) < 4:
        return

    sched_type = parts[2]
    category = parts[3]

    if category == CATEGORY_UNIV:
        # عرض مسارات وتخصصات الجامعة
        if call.message:
            try:
                await call.message.edit_text(SCHEDULE_UNIV_TEXT, parse_mode="HTML", reply_markup=get_study_schedule_university_tracks_keyboard(sched_type))
            except Exception:
                await call.message.answer(SCHEDULE_UNIV_TEXT, parse_mode="HTML", reply_markup=get_study_schedule_university_tracks_keyboard(sched_type))
    else:
        # بكالوريا علمي أو أدبي ⟵ الانتقال لاختيار التصميم اللوني
        cat_info = CATEGORIES.get(category, {})
        cat_name = cat_info.get("title", category)
        text = f"{SCHEDULE_THEME_TEXT}\n\n📚 <b>الفئة:</b> {cat_name}"

        if call.message:
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_study_schedule_theme_keyboard(sched_type, category, track="none"))
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=get_study_schedule_theme_keyboard(sched_type, category, track="none"))


@router.callback_query(F.data.startswith("study_sched:univ:"))
async def handle_university_track_selection(call: types.CallbackQuery):
    """اختيار التخصص الجامعي ⟵ الانتقال لاختيار السمة اللونية والتصميم."""
    await call.answer()
    parts = call.data.split(":")
    if len(parts) < 4:
        return

    sched_type = parts[2]
    track = parts[3]

    track_info = UNIVERSITY_TRACKS.get(track, {})
    track_title = track_info.get("title", track)
    text = f"{SCHEDULE_THEME_TEXT}\n\n🎓 <b>المسار الجامعي:</b> {track_title}"

    if call.message:
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_study_schedule_theme_keyboard(sched_type, CATEGORY_UNIV, track=track))
        except Exception:
            await call.message.answer(text, parse_mode="HTML", reply_markup=get_study_schedule_theme_keyboard(sched_type, CATEGORY_UNIV, track=track))


@router.callback_query(F.data.startswith("study_sched:gen:"))
async def handle_generate_study_schedule(call: types.CallbackQuery):
    """توليد ورسم الجدول الدراسي بالسمة المختارة وتصدير الصورة وPDF."""
    parts = call.data.split(":")
    if len(parts) < 6:
        await call.answer("بيانات غير مكتملة، حاول مجدداً.", show_alert=True)
        return

    sched_type = parts[2]
    category = parts[3]
    track = parts[4]
    theme_id = parts[5]

    await call.answer("🎨 جاري رسم الجدول بأعلى جودة وتنسيق الخطوط...", show_alert=False)

    user_full_name = call.from_user.full_name or call.from_user.first_name or "طالب مجتهد"
    theme_info = THEMES.get(theme_id, THEMES[DEFAULT_THEME])

    # تحديد البيانات المناسبة
    if sched_type == SCHEDULE_TYPE_WEEKLY:
        if category == CATEGORY_BAC_SCI:
            data = BAC_SCI_WEEKLY
            cat_label = "بكالوريا علمي (سوريا)"
        elif category == CATEGORY_BAC_LIT:
            data = BAC_LIT_WEEKLY
            cat_label = "بكالوريا أدبي (سوريا)"
        else:
            data = get_univ_weekly(track if track != "none" else TRACK_GENERAL)
            t_info = UNIVERSITY_TRACKS.get(track, {})
            cat_label = f"جامعي · {t_info.get('title', 'عام')}"

        status_msg = await call.message.answer("⏳ <b>جاري تصميم وتوليد الجدول الأسبوعي بدقة عالية...</b>", parse_mode="HTML")

        try:
            png_bytes, pdf_bytes = await render_weekly_schedule_async(data, theme_id=theme_id, student_name=user_full_name)
        except Exception as e:
            logger.error(f"Error rendering weekly schedule: {e}", exc_info=True)
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء رسم الجدول. حاول مرة أخرى.")
            return

    else:  # SCHEDULE_TYPE_ANNUAL
        if category == CATEGORY_BAC_SCI:
            data = BAC_SCI_ANNUAL
            cat_label = "بكالوريا علمي (سوريا)"
        elif category == CATEGORY_BAC_LIT:
            data = BAC_LIT_ANNUAL
            cat_label = "بكالوريا أدبي (سوريا)"
        else:
            data = UNIV_ANNUAL
            cat_label = "طلاب الجامعات السورية"

        status_msg = await call.message.answer("⏳ <b>جاري تصميم وتوليد الخطة السنوية الشاملة بدقة عالية...</b>", parse_mode="HTML")

        try:
            png_bytes, pdf_bytes = await render_annual_schedule_async(data, theme_id=theme_id, student_name=user_full_name)
        except Exception as e:
            logger.error(f"Error rendering annual schedule: {e}", exc_info=True)
            await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء رسم الخطة السنوية. حاول مرة أخرى.")
            return

    # إعداد الكابشن التوضيحي الغني
    type_ar = "الجدول الأسبوعي" if sched_type == SCHEDULE_TYPE_WEEKLY else "الخطة السنوية الشاملة"
    caption = (
        f"✨ <b>تم إنشاء {type_ar} بنجاح!</b>\n"
        f"🎯 <b>الفئة:</b> {cat_label}\n"
        f"🎨 <b>التصميم:</b> {theme_info['name']}\n"
        f"👤 <b>مخصص للطالب:</b> {user_full_name}\n\n"
        f"💡 <i>{data.get('quote', '')}</i>\n\n"
        f"👇 <b>يمكنك تبديل اللون والشكل بلمسة واحدة من الأزرار أدناه:</b>"
    )

    photo_filename = f"schedule_{sched_type}_{category}_{theme_id}.png"
    pdf_filename = f"جدول_دراسي_{cat_label.replace(' ', '_')}_{sched_type}.pdf"

    # حذف رسالة الانتظار المؤقتة
    try:
        await status_msg.delete()
    except Exception:
        pass

    # 1. إرسال صورة المعاينة الفخمة في الشات
    photo_file = BufferedInputFile(png_bytes, filename=photo_filename)
    await call.message.answer_photo(
        photo=photo_file,
        caption=caption,
        parse_mode="HTML",
        reply_markup=get_study_schedule_result_keyboard(sched_type, category, theme_id, track=track),
    )

    # 2. إرسال ملف الـ PDF الجاهز للطباعة المباشرة بدقة 300 DPI
    pdf_file = BufferedInputFile(pdf_bytes, filename=pdf_filename)
    doc_caption = f"📄 <b>نسخة PDF فائقة الدقة (Print-Ready)</b>\nجاهزة للطباعة المباشرة على ورق A4 أو حفظها كمرجع دراسي دائم."
    await call.message.answer_document(document=pdf_file, caption=doc_caption, parse_mode="HTML")

    log_info(logger, f"Generated {sched_type} schedule for user {call.from_user.id} ({cat_label}, theme: {theme_id})")
