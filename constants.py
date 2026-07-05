"""
Constants and configuration settings for the quiz maker bot.
Centralized management of all magic numbers and configuration values.
"""

# ==================== File Size Limits ====================
MAX_DOC_SIZE = 20 * 1024 * 1024  # 20MB
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB
MAX_PDF_PAGES = 30

# ==================== AI Model Configuration ====================
GEMINI_MODEL = "gemini-2.5-flash"
MAX_TEXT_LENGTH_FOR_AI = 10000  # Characters
AI_REQUEST_TIMEOUT = 30  # Seconds

# ==================== Points System ====================
WELCOME_POINTS = 20
DAILY_RENEWAL_POINTS = 15
REFERRAL_BONUS_POINTS = 10
POINTS_PER_QUESTION = 1

# ==================== API Rate Limiting ====================
KEY_BLOCK_QUOTA_EXHAUSTED = 24  # Hours
KEY_BLOCK_TEMPORARY_ERROR = 2  # Minutes

# ==================== Quiz Settings ====================
MIN_QUESTIONS_TO_GENERATE = 1
MAX_QUESTIONS_TO_GENERATE = 50
OPTION_COUNT = 4

# ==================== Message Content ====================
ADMIN_CONTACT = "@abiadd"

# ==================== Error Messages ====================
ERROR_FILE_TOO_LARGE = "❌ هذا الملف كبير جداً! الحد الأقصى المموح به هو {max_size} ميجابايت."
ERROR_INVALID_PDF_PAGES = "❌ الملف يحتوي على ({page_count}) صفحة! الحد الأقصى المسموح به هو {max_pages} صفحة."
ERROR_PDF_READ_FAILED = "❌ عذراً، فشل البوت في قراءة ملف الـ PDF."
ERROR_IMAGE_TOO_LARGE = "❌ حجم الصورة كبير جداً! الحد الأقصى هو {max_size} ميجابايت."
ERROR_NO_TEXT_EXTRACTED = "لم نتمكن من استخراج أي نصوص مقروءة."
ERROR_NO_QUESTIONS_GENERATED = "❌ لم يتمكن الذكاء الاصطناعي من استخراج أسئلة مفيدة."
ERROR_INSUFFICIENT_POINTS = "❌ رصيدك الحالي ({current}) لا يكفي لتوليد {required} أسئلة."
ERROR_API_KEYS_NOT_CONFIGURED = "❌ خطأ: لم يتم ضبط مفاتيح GEMINI_API_KEYS في ملف .env"
ERROR_ALL_KEYS_EXHAUSTED = "❌ فشلت جميع محاولات قراءة الصورة بسبب نفاد مخصصات كافة المفاتيح المتاحة حالياً."
ERROR_ALL_KEYS_BLOCKED = "❌ جميع مفاتيح API محظورة حالياً. يرجى المحاولة لاحقاً."

# ==================== Success Messages ====================
SUCCESS_FILE_UPLOADED = "✅ تم رفع وقراءة الملف بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)"
SUCCESS_PHOTO_UPLOADED = "✅ تم استقبال الصورة بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)"
SUCCESS_POINTS_CHARGED = "✅ تم بنجاح شحن {amount} نقطة للمستخدم `{user_id}`.\n💰 رصيده الجديد أصبح: {balance} نقطة."
SUCCESS_QUIZ_COMPLETED = "🏁 **اكتمل الاختبار بنجاح!**\n\n🎯 نتيجتك النهائية: {score} من {total}"

# ==================== Processing Messages ====================
MSG_PROCESSING = "🤖 جاري قراءة المحتوى ومعالجته بالذكاء الاصطناعي..."
MSG_KEY_SKIPPED = "ℹ️ تخطي المفتاح {key_idx} تلقائياً لأنه محظور (ينتهي الحظر في: {unblock_time})"
MSG_KEY_BLOCKED_QUOTA = "🛑 تم حظر المفتاح رقم {key_idx} لمدة 24 ساعة بسبب استنفاد الحصة اليومية."
MSG_KEY_BLOCKED_ERROR = "⚠️ خطأ مؤقت أو شبكي في المفتاح رقم {key_idx}، تم تعليقه لدقيقتين. الخطأ: {error}"

# ==================== Prompts ====================
SYSTEM_PROMPT_EXTRACT_TEXT = "استخرج النص الموجود في هذه الصورة بدقة. أخرج النص فقط دون أي مقدمات."

SYSTEM_PROMPT_GENERATE_QUESTIONS = """
أنت خبير تعليمي، مهمتك استخراج {count} أسئلة اختيار من متعدد بناءً على النص المرفق حصراً ووفق القواعد الصارمة التالية:
1. الاستناد الكامل: استخرج الأسئلة من العبارات والمعلومات الموجودة في النص المرفق فقط. لا تقم بإضافة معلومات خارجية.
2. معالجة عيوب النصوص (OCR): افحص النص بدقة، وإذا وجدت كلمات مشوهة قم بتعديلها لتصبح مناسبة للسياق، واجعل قيمة `was_corrupted_text_fixed` تساوي `true`.
3. صغ لكل سؤال تلميحاً ذكياً (hint) يساعد الطالب على الاستنتاج دون إعطائه الحل المباشر.
"""

# ==================== Validation Rules ====================
VALID_QUESTIONS_RANGE = (MIN_QUESTIONS_TO_GENERATE, MAX_QUESTIONS_TO_GENERATE)
QUOTA_ERROR_KEYWORDS = ["429", "resource_exhausted", "quota"]

# ==================== Webhook Configuration ====================
import os
WEBHOOK_HOST = "0.0.0.0"  # الاستماع لجميع الواجهات داخل الحاوية
WEBHOOK_PORT = int(os.getenv("PORT", 8080))  # الاعتماد الافتراضي على المنفذ 8080 ليتوافق مع ريلواي
WEBHOOK_PATH = "/webhook"  # المسار الخاص باستقبال طلبات تليجرام