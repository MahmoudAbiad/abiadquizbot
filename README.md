# 🚀 Quiz Maker Bot — abiadquizbot

> **منصة اختبارات تفاعلية متكاملة على تيليجرام مدعومة بالذكاء الاصطناعي (aiogram v3 + FastAPI + Supabase + Redis + Cloudflare R2)**

يُتيح **بوت أبيض (abiadquizbot)** للطلاب والأساتذة توليد اختبارات ذكية أو استخراج اختبارات جاهزة من ملفات PDF، مستندات Office (Word/PowerPoint/TXT)، ألبومات الصور، النصوص المباشرة، والمحاضرات الصوتية، مع منظومة متكاملة للتصدير الاحترافي (PDF/Word/Excel)، الاختبارات الجماعية التنافسية، التدريب على الأخطاء، نظام التحديات والأبراج (PvP)، ولعبة «رحلة أبيض» التعليمية (Telegram Mini App).

## ✨ المميزات الأساسية

### 🧠 التوليد الذكي ومعالجة الوسائط

* **تعدد مزوّدي الذكاء الاصطناعي (Dynamic AI Providers):** إدارة كاملة من لوحة الأدمن لسلاسل النماذج (Cascade) عبر Google Gemini API، Google Vertex AI، والواجهات المتوافقة مع OpenAI (Groq، OpenAI، OpenRouter، Cloudflare Workers AI) مع تدوير تلقائي للمفاتيح، تبريد ذكي (Adaptive Cooldown)، واكتشاف حي لكتالوج نماذج Google.

* **التوليد الموسّع واستخراج الاختبارات الجاهزة (`batch_generation`):** دعم توليد حتى 120 سؤالاً أو استخراج أسئلة الامتحانات الجاهزة كما هي (أو حلها بالذكاء الاصطناعي) عبر دفعات متوازنة (حتى 20 سؤالاً للدفعة) مع حفظ التقدم المؤقت (Checkpoints) في Redis لمدة ساعتين وإعادة استخدام جلسات رفع ملفات Gemini Files API.

* **دعم شامل للمدخلات:**

  * ملفات **PDF، DOCX، PPTX، TXT** (عبر محادثة البوت حتى 20MB، أو عبر صفحة الرفع Mini App حتى 100MB / 150 صفحة مباشرة إلى Cloudflare R2).

  * **الصور والألبومات:** استقبال الصور المضغوطة أو غير المضغوطة (PNG/JPG/WebP) في تيليجرام، أو رفع حتى 50 صورة عبر الـ Mini App.

  * **المحاضرات الصوتية:** تفريغ صوتي أكاديمي وتلخيص منظم (حتى 250MB / 240 دقيقة عبر الـ Mini App) مع إمكانية التصدير أو تحويل التفريغ إلى كويز مباشرة.

  * **استيراد Excel مباشر (`.xlsx`):** تحويل ملفات Excel المعدّلة إلى كويز تفاعلي فوراً دون استهلاك نقاط أو استدعاء AI.

* **نمط الرياضيات المصوّر (LaTeX Image Mode) والمسائل المشابهة:** اكتشاف تلقائي للمحتوى الرياضي ورسم الأسئلة والمعادلات والجداول والمصفوفات كصور عالية الدقة مع دعم توليد مسائل رياضية مشابهة ومحرر ويب تفاعلي لتعديل الأسئلة الرياضية (`question_edit.html`).

* **تخصيص الكويز والكاش الذكي:** اختيار نوع الأسئلة ومستوى الصعوبة والترجمة (للمحتوى الإنجليزي/الفرنسي)، مع كاش ذكي يدعم الفلترة المحلية وسقف خصم مستقل لكل تركيبة، وعزل كامل لتصنيف المحتوى بين الجلسات (`subject_classify:v3:`).

* **تقدير زمني حي (Generation ETA):** شريط تقدم وتقدير زمني ذكي أثناء انتظار التوليد مبني على القياسات الفعلية وميزانيات الوقت التكيفية (`time_budget.py`).

### 🎯 تجربة الطالب والتعلّم التفاعلي

* **التدرب على الأخطاء ومراجعتها (`mistake_practice`):** مراجعة تفصيلية للإجابات الخاطئة مع الشرح، وإمكانية خوض جولات تدريب معزولة على الأخطاء فقط حتى إتقانها بالكامل دون خصم نقاط أو التأثير على لوحة الشرف.

* **حماية الجلسات النشطة (`session_navigation`):** تنبيه ذكي عند محاولة رفع ملف أو فتح كويز أثناء وجود اختبار جارٍ (خيّر الطالب بين استئناف اختباره الحالي أو إنهائه والبدء بالجديد).

* **المفضلة، المشاركة، ولوحة الشرف:** حفظ الكويزات في أقسام مفضلة قابلة للبحث، مشاركتها عبر روابط عميقة، ولوحة شرف اختيارية لكل كويز.

* **بوابة الموافقة القانونية والاستئناف التلقائي (`consent`):** التحقق من الموافقة على شروط الاستخدام وسياسة الخصوصية (مع دعم تحديث الإصدارات من لوحة الأدمن) واستئناف رسالة/ملف الطالب تلقائياً فور ضغط «أوافق».

### 📁 التصدير الاحترافي وسيرفر الرسم المنفصل

* **تصدير PDF وWord وExcel بنسختين (طالب / أستاذ):** أنماط متعددة (بسيط، عصري، أكاديمي) مع شارة ختام موحدة وتقدير وقت التجهيز.

* **إعدادات ورقة الاختبار المتقدمة (`export_options`):**

  * تقسيم الصفحة إلى **عمودين** مع خط فاصل اختياري وشبكة خيارات مضغوطة (حتى **16 سؤالاً بالصفحة**).

  * إضافة **مسودة حل** تحت كل سؤال (صغيرة/متوسطة/كبيرة — أسطر، شبكة مربعات، أو إطار فارغ).

  * قوالب تخطيط جاهزة بضغطة واحدة، وتعبئة سريعة لبيانات الترويسة برسالة واحدة، وحفظ الإعدادات الدائمة للأستاذ في قاعدة البيانات (`export_profiles`).

* **سيرفر الرسم والتصدير المنفصل (`render_worker.py`):** خدمة HTTP مستقلة عديمة الحالة (Stateless) لمعالجة رسم صور الرياضيات، الشهادات، فيديو حفل التكريم، وتصدير الملفات في عمليتين فرعيتين معزولتين مع قاطع دائرة (Circuit Breaker) واحتياط محلي آمن.

### 👥 الكويزات الجماعية (الغروبات والقنوات)

* **شاشة إعداد موحّدة بالخاص (`gqs:`):** إعداد كامل من محادثة البوت الخاصة عبر روابط عميقة موقّعة للمشرفين (`gqsetup_`)، مع دعم الوضع التنافسي أو المجهول، وتحديد عدد الأوائل (`top_n` من 1 إلى 20).

* **أنماط الإرسال:** مؤقت لكل سؤال (`chain_to_timer`)، فاصل زمني ثابت (`fixed_interval`)، أو إرسال كل الأسئلة دفعة واحدة (`burst`).

* **قفل المحادثة الآمن (`group_quiz_lock`):** قفل اختياري لإرسال الرسائل في المجموعة أثناء الاختبار الموقّت مع دفتر يومية (Journal) في قاعدة البيانات يضمن استعادة الصلاحيات الأصلية تلقائياً عند الانتهاء أو الإلغاء أو إعادة تشغيل الخادم.

* **شهادات التقدير المصوّرة (`group_certificate`):** توليد وإرسال شهادات تقدير بتصاميم وألوان متعددة للأوائل مع معاينة حية للأدمن.

* **حفل التكريم باللهب والموسيقى (`group_ceremony`):** توليد صورة أو فيديو MP4 بلهب متحرك وموسيقى حماسية يعرض لوحة شرف الأوائل بعد انتهاء الكويز الجماعي.

### 💰 المحفظة الذرية، الهدايا، التجديد اليومي، والشحن الذاتي

* **المحفظة الذرية (`wallet_debit`):** فصل دقيق بين النقاط المجانية اليومية، نقاط الهدايا (بصلاحية زمنية)، والنقاط المدفوعة، مع أولوية خصم تلقائية (المجاني ← الهدايا الأقرب انتهاءً ← المدفوع) واسترجاع ذري إلى نفس المصدر عند الفشل.

* **أكواد الهدايا وحملات الإهداء (`gifts` & `gift_campaigns`):** إنشاء أكواد فردية أو جماعية بروابط شحن تلقائي (`?start=gift_...`)، أرشفة أو سحب هدايا كود معين، وإطلاق حملات إهداء جماعية مباشرة لجميع الطلاب المسجلين.

* **التجديد اليومي التلقائي (`daily_renewal`):** تجديد الرصيد المجاني يومياً الساعة **20:00 بتوقيت دمشق** عبر `pg_cron` مع نظام إشعارات موثوق وتفضيلات إشعارات يتحكم بها الطالب من `/settings`.

* **الشحن الذاتي عبر شام كاش (`payments`):** ربط مع تطبيق MacroDroid لاستقبال إشعارات وسجل حوالات شام كاش، مع دعم المراجعة اليدوية في مجموعة الأدمن أو **الشحن التلقائي المشروط** (`payments_auto_confirm`) بضوابط وسقوف أمان صارمة.

### ⚔️ نظام التحديات ولعبة «رحلة أبيض»

* **نظام التحديات والأبراج (`challenge_*`):** بنك محتوى هرمي (فروع ← مواد ← وحدات ← اختبارات)، أرض وأبراج دفاعية لكل طالب، هجمات غير متزامنة (Async Land Attacks)، مواجهات حية مباشرة (Live PvP Matchmaking)، ووضع المشاهدة الحية للمدافع (Spectator Mode 👁️).

* **لعبة رحلة أبيض التعليمية (`webapp/runner/`):** لعبة جري ثلاثية المسارات (Canvas 2D Mini App) تعرض أسئلة الكويز كبوابات إجابة مع بروتوكول جاهزية المؤقت (`/ready`)، كاش صور مسبق التجهيز لمدة 7 أيام، عملات محفوظة، نقاط معرفة وإتقان، هدف دراسي يومي، ومتجر دائم (حقيبة طيران نفاثة وسيارة تيسلا بـ 3 مستويات).

### 🛠️ لوحة الأدمن المتكاملة والموثوقية

* **لوحة تحكم شاملة (`/admin`):** إحصاءات عامة، إدارة المستخدمين والبحث المتقدم وسجل نشاط كل طالب، تحليلات الاستخدام وتصدير CSV، طرق الانضمام والإحالات، إدارة مزوّدي ونماذج الذكاء الاصطناعي وسجل التوكنز والتكلفة، إدارة الهدايا والحملات، إدارة التحديات والفروع المؤرشفة، إحصاءات الكويز الجماعي، مفاتيح التحكم (`feature_flags`)، ووضع الصيانة اليدوي (`maintenance`).

* **طابور Webhook موثوق (`webhook_queue`):** تخزين التحديثات ذرياً في Redis قبل الرد بـ HTTP 200 مع عقود إيجار (Leases) قابلة للتجديد، منع التكرار، وإعادة المحاولة التلقائية.

* **نقطة نهاية إحصاءات الموقع العام (`/api/public-stats`):** واجهة قراءة فقط محمية بـ CORS وكاش داخلي (تتحدث كل 15 دقيقة) لعرض عدد الطلاب والأسئلة على موقع `abiad.me`.

## 🏗️ الهيكل الفعلي للمشروع

```
abiadquizbot/
├── main.py                          # نقطة الدخول (تشغيل Webhook أو Polling، أو تحويل لـ render_worker)
├── webhook_server.py                # خادم FastAPI (Webhook، نقاط نهاية Mini Apps، الدفع، Runner، الإحصاءات)
├── render_worker.py                 # سيرفر الرسم والتصدير المستقل (APP_ROLE=render_worker)
├── config.py                        # تهيئة البوت، اتصال Redis المحمي، وحالات FSM (QuizState / ExportState)
├── constants.py                     # الثوابت، البرومبتات، الحدود، ورسائل الواجهة
├── keyboards.py                     # بناء كافة لوحات الأزرار التفاعلية (Inline Keyboards)
├── middlewares.py                   # الميدل وير (GroupQuizOnly, ErrorTracking, Maintenance, Throttling, Usage)
├── validators.py                    # التحقق من صحة المدخلات والملفات
├── error_context.py                 # سياق الطلب (ContextVars) لربط الأخطاء بالطالب
├── logger.py                        # نظام التسجيل (Logging) والربط مع تحليلات الأخطاء
├── mem_utils.py                     # أدوات تحرير الذاكرة (gc + malloc_trim) لحاويات 512MB
├── task_utils.py                    # جدولة المهام الخلفية الآمنة (spawn)
├── telegram_webapp_auth.py          # التحقق من توقيع Telegram Mini App (initData HMAC-SHA256)
├── utils.py                         # دوال مساعدة عامة وتنظيف النصوص والملفات
├── requirements.txt                 # متطلبات خادم البوت الرئيسي
├── requirements.worker.txt          # متطلبات سيرفر الرسم المستقل (render_worker)
├── Dockerfile / Procfile / app.json # ملفات النشر والتشغيل (Docker / Heroku / Render)
├── .env.example                     # قالب شامل لجميع متغيرات البيئة المدعومة
│
├── assets/                          # الأصول الثابتة
│   ├── abiad_logo_full_badge.png    # شعار البوت للشهادات والتصدير
│   ├── shamcash_qr.jpg              # رمز QR للشحن الذاتي عبر شام كاش
│   ├── ceremony/                    # أصول حفل التكريم (flames_loop.mp4, flames_still.png, theme.mp3)
│   └── fonts/                       # خطوط الرندر والتصدير (Amiri, NotoNaskhArabic, NotoSans)
│
├── webapp/                          # تطبيقات Telegram Mini Apps (تُخدّم تحت /webapp)
│   ├── upload_hub.html              # بوابة اختيار نوع الرفع
│   ├── file_upload.html             # رفع المستندات الكبيرة وألبومات الصور مباشرة إلى R2
│   ├── audio_upload.html            # رفع المحاضرات الصوتية الكبيرة مباشرة إلى R2
│   ├── question_edit.html           # محرر الأسئلة الرياضية والجداول والمصفوفات
│   └── runner/                      # لعبة «رحلة أبيض» التعليمية (index.html, game.js, world.js, audio.js, style.css)
│
├── handlers/                        # معالجات تيليجرام (Routers)
│   ├── start.py                     # الترحيب، القائمة الرئيسية، الحساب، المساعدة، استرداد روابط الهدايا
│   ├── files.py                     # استقبال المستندات/الصور/النصوص/Excel، الكاش، وبدء التوليد
│   ├── audio.py                     # استقبال ومعالجة المحاضرات الصوتية (تفريغ، تلخيص، تصدير، كويز)
│   ├── quiz_options.py              # تخصيص نوع الأسئلة ومستوى الصعوبة والنوع الحر
│   ├── quiz_runner.py               # حل الكويز، شاشة النتيجة، التقييم، وتعديل الأسئلة
│   ├── session_navigation.py        # حماية الجلسات النشطة واستئناف/إنهاء الاختبار الجاري
│   ├── export.py                    # تصدير الكويزات (PDF/Word/Excel) وإدارة الإعدادات الإضافية والقوالب
│   ├── favorites.py                 # إدارة المفضلة والأقسام والبحث
│   ├── sharing.py                   # مشاركة الكويزات بروابط عميقة
│   ├── leaderboard.py               # لوحة الشرف وإظهار/إخفاء النتيجة
│   ├── group_quiz.py                # الكويز الجماعي (الإعداد الموحد، المؤقت، القفل، الشهادات، حفل التكريم)
│   ├── gifts.py                     # محفظة الطالب، عرض الهدايا وصلاحيتها، وشحن الأكواد
│   ├── payments.py                  # الشحن الذاتي للطالب وأزرار القبول/الرفض لمجموعة الأدمن
│   ├── consent.py                   # أزرار الموافقة القانونية واستئناف الطلبات المعلّقة
│   ├── settings.py                  # إعدادات الطالب وتفضيلات إشعارات التجديد اليومي
│   ├── runner.py                    # إصدار روابط إطلاق لعبة «رحلة أبيض» الموقّعة
│   ├── challenge_game.py            # واجهة التحديات والأرض والأبراج والهجمات غير المتزامنة
│   ├── challenge_live.py            # واجهة المواجهات المباشرة (Live PvP)
│   ├── challenge_spectator.py       # وضع المشاهدة الحية للهجمات الجارية على أرض المدافع
│   ├── quiz_delete.py               # حذف الكويز نهائياً (للمالك أو الأدمن)
│   ├── tutorial.py                  # وحدة الدليل التفاعلي القديم (محفوظة للتوافق، غير مسجلة في main.py)
│   └── admin/                       # لوحة تحكم الأدمن
│       ├── dashboard.py             # الشاشة الرئيسية للوحة الأدمن والإحصاءات السريعة
│       ├── users.py                 # إدارة المستخدمين، البحث، الشحن، وسجل نشاط الطالب
│       ├── analytics.py             # تحليلات الاستخدام، الأخطاء، الإحالات، وتصدير CSV
│       ├── join_sources.py          # إحصاءات طرق الانضمام وروابط المشاركة
│       ├── ai_control.py            # إدارة سلاسل النماذج، مستويات التفكير، وسجل التوكنز والتكلفة
│       ├── ai_providers.py          # إضافة وتعديل وحذف مزوّدي الذكاء الاصطناعي
│       ├── google_catalog.py        # اكتشاف وفحص وتفعيل نماذج Google Gemini حياً
│       ├── gifts.py                 # إدارة أكواد الهدايا، المستفيدين، وحملات الإهداء الجماعية
│       ├── settings.py              # إعدادات النقاط، مهلة AI، وإصدارات الوثائق القانونية
│       ├── feature_flags.py         # مفاتيح تشغيل/إيقاف الميزات (Feature Flags)
│       ├── maintenance.py           # تشغيل/إيقاف وضع الصيانة وتخصيص رسالته
│       ├── group_quiz_stats.py      # إحصاءات وجلسات الكويز الجماعي النشطة والسابقة
│       ├── challenges.py            # إدارة بنك التحديات (الفروع، المواد، الوحدات، الاختبارات، الأرشيف)
│       ├── challenge_settings.py    # إعدادات مهل وحدود هجمات التحديات
│       ├── feedbacks.py             # استعراض ملاحظات وشكاوى الطلاب
│       └── admin_utils.py           # أدوات مساعدة مشتركة للوحة الأدمن
│
├── services/                        # منطق العمل الأساسي (Business Logic)
│   ├── ai/                          # طبقة الذكاء الاصطناعي المستقلة عن المزوّدين
│   │   ├── contracts.py             # عقود الطلبات والمرفقات والنتائج (AIRequest, Attachment, AIResult)
│   │   ├── adapters.py              # محولات الاتصال (Gemini, Vertex, OpenAI-compatible, Cloudflare Vision)
│   │   ├── runtime.py               # اختيار النماذج، تدوير المفاتيح، والتبريد (Cascade Execution)
│   │   ├── provider_presets.py      # قوالب الإعداد الجاهزة (OpenRouter, Cloudflare, Groq, OpenAI)
│   │   ├── rate_limits.py           # تصنيف أخطاء الحصص (429) وحساب فترات التبريد المتكيّف
│   │   ├── time_budget.py           # ميزانيات الوقت التكيفية لتوليد الكويزات
│   │   ├── thinking.py              # ترجمة إعدادات مستوى التفكير (Thinking Budget / Reasoning Effort)
│   │   ├── file_session.py          # إدارة جلسات رفع الملفات في Gemini Files API عبر الدفعات
│   │   ├── google_catalog.py        # فحص وتصفية كتالوج نماذج Google API
│   │   ├── diagnostics.py           # تشخيصات مراحل الطلب والشبكة الآمنة (AI_DIAG)
│   │   ├── media.py                 # تحويل صفحات PDF لصور عند فحص المحتوى بنماذج الرؤية
│   │   ├── parsing.py               # تنظيف وترميم وتحليل استجابات JSON/LaTeX
│   │   └── state.py                 # حالة الطلب المحلية (ContextVars) للتوكنز والميتاداتا
│   ├── quiz_service.py              # تنسيق توليد الكويزات، الخصم، الاسترجاع، وحفظ الكاش
│   ├── quiz_engine.py               # إرسال الأسئلة، رسم الصور عند الحاجة، وحفظ روابطها
│   ├── quiz_result_ui.py            # بناء شاشات النتيجة ومراجعة الأخطاء
│   ├── mistake_practice.py          # جلسات التدرب المعزولة على الأخطاء
│   ├── subject_classifier.py        # تصنيف المادة واكتشاف الاختبارات الجاهزة والرياضيات
│   ├── detection_common.py          # أدوات أخذ العينات المشتركة للتصنيف
│   ├── generation_eta.py            # حساب الوقت المتوقع للتوليد ونصوص شريط التقدم
│   ├── generation_heartbeat.py      # حلقة تحديث رسالة انتظار التوليد
│   ├── file_service.py              # استخراج النصوص من مستندات Word/PowerPoint/TXT
│   ├── excel_import.py              # استيراد والتحقق من ملفات الكويز بصيغة XLSX
│   ├── audio_service.py             # تفريغ وتلخيص المحاضرات الصوتية وتنسيق HTML
│   ├── audio_flow_service.py        # إدارة تدفق الصوت واسترجاع النقاط التناسبي
│   ├── audio_duration_service.py    # قياس مدة الملفات الصوتية عبر ffmpeg/ffprobe
│   ├── image_quiz_renderer.py       # محرك رسم الأسئلة الرياضية والجداول والمصفوفات كصور
│   ├── print_quiz_renderer.py       # رسم كتل الأسئلة الرياضية المدمجة للطباعة والتصدير
│   ├── latex_text.py                # معالجة وتحويل صيغ LaTeX والنصوص العربية
│   ├── export_service.py            # بناء ملفات التصدير (PDF / Word / Excel)
│   ├── export_options.py            # خيارات التصدير الإضافية، القوالب، والتعبئة السريعة
│   ├── export_layout.py             # تخطيط الصفحات والأعمدة وتوزيع المسودات
│   ├── export_branding.py           # شارة الهوية البصرية للملفات المصدّرة
│   ├── offload.py                   # عميل الاتصال بسيرفر الرسم المنفصل وقاطع الدائرة
│   ├── offload_worker_core.py       # تنفيذ مهام الرسم والتصدير داخل سيرفر الـ Worker
│   ├── offload_process.py           # نقطة دخول العمليات الفرعية المعزولة في الـ Worker
│   ├── processing_limits.py         # بوابات التزامن المشتركة لحماية ذاكرة 512MB
│   ├── group_quiz_store.py          # إدارة جلسات الكويز الجماعي وحلقة التنظيف
│   ├── group_permissions.py         # التحقق من صلاحيات المشرفين في المجموعات والقنوات
│   ├── group_quiz_lock.py           # قفل واستعادة صلاحيات المجموعة أثناء الاختبار
│   ├── group_certificate.py         # منطق أهلية وإعدادات شهادات التقدير الجماعية
│   ├── certificate_renderer.py      # رسم شهادات التقدير (Pillow + Arabic Reshaper)
│   ├── group_ceremony.py            # منطق أهلية وإعدادات حفل التكريم الجماعي
│   ├── ceremony_renderer.py         # رسم صور وتوليد فيديو حفل التكريم باللهب والموسيقى
│   ├── payment_service.py           # معالجة أحداث الدفع، الشحن الذري، التلقائي، والـ Watchdog
│   ├── payment_parser.py            # محلل إشعارات وشاشات تطبيق شام كاش
│   ├── gift_campaigns.py            # عامل توزيع حملات الهدايا الجماعية وإشعاراتها
│   ├── gift_links.py                # بناء والتحقق من روابط الشحن التلقائي للهدايا
│   ├── gift_presentation.py         # تنسيق شاشات المحفظة والهدايا
│   ├── wallet_errors.py             # استثناءات التمييز بين رفض الرصيد والأخطاء التقنية
│   ├── daily_renewal.py             # عامل إرسال إشعارات التجديد اليومي وتقرير الأدمن
│   ├── consent.py                   # ميدل وير الموافقة القانونية وحفظ واستئناف الطلبات المعلّقة
│   ├── legal_consent.py             # منطق التحقق من إصدارات الشروط والخصوصية
│   ├── maintenance.py               # حالة وضع الصيانة في Redis وكاش التنبيهات
│   ├── webhook_queue.py             # طابور التحديثات الموثوق في Redis مع عقود الإيجار
│   ├── redis_reliability.py         # أقفال المستخدمين الموزعة (UserLock) ومنع تكرار الرفع
│   ├── upload_validation.py         # فحص صيغ وأبعاد الملفات والصور وتنظيف التنزيلات
│   ├── runner_core.py               # القواعد النقية للعبة «رحلة أبيض» وبناء الأسئلة
│   ├── runner_api.py                # نقاط نهاية API للعبة «رحلة أبيض» (/start, /question, /ready, /answer, ...)
│   ├── runner_images.py             # كاش صور اللعبة لمدة 7 أيام والرسم المسبق (Prewarm)
│   ├── runner_economy.py            # عمليات متجر ومحفظة لعبة Runner
│   ├── runner_cleanup.py            # حلقة تنظيف جولات وصور لعبة Runner المنتهية
│   ├── runner_availability.py       # فحص تفعيل اللعبة (حراسة البيئة + مفتاح الأدمن)
│   ├── runner_static.py             # تقديم ملفات لعبة Runner بترويسة Cache-Control: no-store
│   ├── challenge_content.py         # التحقق من محتوى بنك التحديات واستيراد JSON/الكويزات
│   ├── challenge_game.py            # القواعد النقية لهجمات الأراضي والأبراج
│   ├── challenge_live.py            # القواعد النقية للمواجهات المباشرة
│   ├── challenge_live_ui.py         # بناء شاشات المواجهات المباشرة
│   ├── challenge_spectator_presence.py # إدارة حضور المشاهدين في Redis (Sorted Sets + Lua)
│   ├── challenge_spectator_ui.py    # بناء شاشات وضع المشاهدة الحية
│   ├── challenge_presentation.py    # بيانات عرض صور وخيارات التحديات
│   ├── challenge_notifications.py   # تنسيق إشعارات الهجوم والدفاع
│   ├── challenge_diagnostics.py     # تشخيصات آمنة لأخطاء التحديات
│   ├── public_website_stats.py      # كاش ونقطة نهاية إحصاءات الموقع العام (/api/public-stats)
│   ├── usage_events.py              # تصنيف وتسميات أحداث التحليلات
│   ├── usage_tracking.py            # تسجيل أحداث الاستخدام ورفض الرفع بالخلفية
│   ├── ai_audit.py                  # بيانات تدقيق مصدر الطلبات (telegram / web_upload)
│   ├── quiz_permissions.py          # صلاحيات تعديل وحذف الكويزات
│   ├── quiz_timing.py               # حساب الأوقات التقديرية للأسئلة وبصمات المحتوى
│   ├── flow_state.py                # بناء كائن FSMContext لمحادثات المستخدمين
│   └── async_client_init.py         # تهيئة عملاء SDK غير المتزامنين بأمان
│
├── helpers/                         # الحزم المساعدة والتكامل مع الخدمات الخارجية
│   ├── gemini_helper/               # تخطيط برومبتات الكويز، الدفعات (batch_generation)، والدمج
│   ├── supabase_helper/             # طبقة قاعدة البيانات المقسّمة حسب المجال (users, gifts, quiz_cache, challenges, ...)
│   ├── ai_providers_helper.py       # عمليات CRUD لمزوّدي الذكاء الاصطناعي
│   ├── ai_models_helper.py          # إدارة سلاسل النماذج وترتيبها وقدراتها
│   ├── settings_helper.py           # قراءة وتحديث إعدادات التطبيق (app_settings) مع الكاش
│   ├── r2_helper.py                 # توليد روابط الرفع الموقّعة وإدارة الملفات المؤقتة على Cloudflare R2
│   ├── points_calculator.py         # حساب تسعير توليد الكويزات وتفريغ الصوت
│   ├── student_ui.py                # نصوص وقوالب واجهة الطالب الموحدة
│   └── challenge_delivery.py        # إرسال وتحديث رسائل وصور التحديات
│
├── db/                              # قاعدة البيانات (راجع db/README.md للتفاصيل الكاملة)
│   ├── migrations/                  # الترحيلات المرقّمة والمؤرخة بالترتيب (0001 إلى 20261108)
│   ├── operations/                  # إجراءات تشغيلية (مثل انتقال التجديد اليومي للساعة 20:00)
│   ├── review/                      # سكربتات مراجعة وتقوية الصلاحيات
│   └── archive/                     # ترحيلات تاريخية قديمة مدمجة مسبقاً في 0001_baseline.sql
│
├── scripts/                         # أدوات بناء الأصول والفحص
│   ├── build_ceremony_assets.py     # توليد أصول اللهب لحفل التكريم
│   ├── build_runner_audio.py        # توليد المؤثرات والموسيقى الأصلية للعبة Runner (CC0)
│   └── cloudflare_probe.py          # أداة تشخيصية لاختبار اتصال Cloudflare Workers AI
│
├── tests/                           # منظومة الاختبارات الشاملة
│   ├── test_*.py                    # 66 ملف اختبار وحدات وتكامل (Python unittest/pytest)
│   ├── sql/                         # اختبارات PostgreSQL / PGlite للترحيلات والعمليات الذرية (.mjs / .sql)
│   └── runner_*.cjs / .mjs          # اختبارات منطق المتصفح وواجهة لعبة Runner
│
└── docs/                            # التوثيق التقني والتشغيلي المفصّل (انظر الفهرس أدناه)

```

## 🎯 البدء السريع

### 1. متغيرات البيئة

انسخ ملف `.env.example` إلى `.env` وعبّئ القيم المطلوبة:

* **أساسية:** `BOT_TOKEN`، `ADMIN_ID`، `SUPABASE_URL`، `SUPABASE_KEY` (مفتاح `service_role`)، `REDIS_URL`.

* **الذكاء الاصطناعي:** `GEMINI_API_KEYS` (مفصولة بفواصل)، واختيارياً `GROQ_API_KEY`، `GOOGLE_VERTEX_PROJECT`، `GOOGLE_VERTEX_LOCATION`، `GOOGLE_VERTEX_SA_KEY_JSON`، `OPENROUTER_API_KEY`، `CLOUDFLARE_API_TOKEN`.

* **التخزين السحابي (للـ Mini Apps):** `R2_ACCOUNT_ID`، `R2_ACCESS_KEY_ID`، `R2_SECRET_ACCESS_KEY`، `R2_AUDIO_BUCKET`، `R2_FILE_BUCKET`.

* **الإنتاج (Webhook):** `WEBHOOK_URL` و`TELEGRAM_WEBHOOK_SECRET`. بدون `WEBHOOK_URL` يعمل البوت تلقائياً بوضع **Polling** للتطوير المحلي.

### 2. تثبيت المتطلبات

```
# لخادم البوت الرئيسي
pip install -r requirements.txt

# أو لسيرفر الرسم المنفصل فقط (render_worker)
pip install -r requirements.worker.txt

```

### 3. إعداد قاعدة البيانات (Supabase)

لبناء قاعدة بيانات جديدة بالكامل، شغّل ملفات الترحيل في `db/migrations/` بالترتيب الموضّح بالتفصيل في [**db/README.md**](db/README.md) (بدءاً من `0001_baseline.sql` ثم `0002_seed_config.sql` ثم الترحيلات اللاحقة). لا تشغّل ملفات `db/archive/` ولا ملفات `*_rollback.sql`.

### 4. التشغيل المحلي

```
# تشغيل البوت (وضع Polling إذا كان WEBHOOK_URL فارغاً)
python main.py

# تشغيل سيرفر الرسم المنفصل محلياً (اختياري)
APP_ROLE=render_worker OFFLOAD_WORKER_SECRET=mysecret python main.py

```

### 5. تشغيل الاختبارات

```
# فحص سلامة الصياغة والاستيراد لجميع الوحدات
python -m compileall -q .
python tests/smoke_import_test.py

# تشغيل اختبارات Python
python -m unittest discover -s tests -p 'test_*.py'

# تشغيل اختبارات قاعدة البيانات المعزولة (PGlite)
npm ci --prefix tests/sql --ignore-scripts
node tests/sql/gift_wallet_test.mjs

```

## 🚀 النشر والتشغيل السحابي

* **Heroku / Render (Web Service):** يستخدم `Procfile` (`web: env MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072 python main.py`) مع Python 3.11 (`.python-version`).

* **سيرفر الرسم المنفصل (Render Worker):** يُنشر من نفس المستودع ونفس الكود مع ضبط `APP_ROLE=render_worker` و`OFFLOAD_WORKER_SECRET`، وتثبيت `requirements.worker.txt`.

* **Docker:** متاح عبر `Dockerfile` المجهّز بإعدادات ضبط ذاكرة `glibc`.

* راجع [**docs/deploy.md**](docs/deploy.md) و[docs/DEPLOYMENT_NOTES.md](docs/DEPLOYMENT_NOTES.md) و[docs/offload.md](docs/offload.md) لخطوات النشر التفصيلية.

## 📚 فهرس ملفات التوثيق (`docs/`)

### المعمارية والنشر والتشغيل

* [**docs/CURRENT_STATE.md**](docs/CURRENT_STATE.md) — المرجع المعماري الشامل للحالة الفعلية للمشروع (يُقرأ قبل أي تعديل).

* [**docs/HISTORY_LOG.md**](docs/HISTORY_LOG.md) — السجل التاريخي التفصيلي للميزات والإصلاحات.

* [**db/README.md**](db/README.md) — دليل هيكل قاعدة البيانات وترتيب ملفات الترحيل (Migrations).

* [**docs/deploy.md**](docs/deploy.md) — دليل النشر والتشغيل (Heroku / Docker / Render / Azure).

* [**docs/DEPLOYMENT_NOTES.md**](docs/DEPLOYMENT_NOTES.md) — ملاحظات النقل بين المنصات وقائمة فحص يوم النقل.

* [**docs/RELIABILITY_PASS_2026-10-02.md**](docs/RELIABILITY_PASS_2026-10-02.md) — هندسة الموثوقية، الأقفال الموزعة، وطابور الـ Webhook.

* [**docs/offload.md**](docs/offload.md) — إعداد وتشغيل سيرفر الرسم والتصدير المنفصل (`render_worker`).

* [**docs/public-website-stats.md**](docs/public-website-stats.md) — نقطة نهاية إحصاءات الموقع العام (`/api/public-stats`).

### الذكاء الاصطناعي والتوليد

* [**docs/ai-providers.md**](docs/ai-providers.md) — إدارة مزوّدي الذكاء الاصطناعي، OpenRouter، Cloudflare Workers AI، وكتالوج Google.

* [**docs/ai-latency-diagnostics.md**](docs/ai-latency-diagnostics.md) — تشخيصات زمن استجابة ومراحل طلبات الذكاء الاصطناعي (`AI_DIAG`).

* [**docs/quiz-timeout-policy.md**](docs/quiz-timeout-policy.md) — سياسة مهل التوليد التكيفية (`time_budget`).

* [**docs/generation-eta-policy.md**](docs/generation-eta-policy.md) — حساب الوقت المتوقع للتوليد وتحديثات رسالة الانتظار.

* [**docs/token-tracking.md**](docs/token-tracking.md) — تتبّع التوكنز وحساب التكلفة وسجل التوليد في لوحة الأدمن.

* [**docs/content-classification-isolation.md**](docs/content-classification-isolation.md) — عزل تصنيف المحتوى بين الجلسات والمدخلات.

### المحفظة، الهدايا، الدفع، والتجديد اليومي

* [**docs/GIFT_WALLET.md**](docs/GIFT_WALLET.md) — نظام المحفظة الذرية وأكواد الهدايا وصلاحيتها.

* [**docs/GIFT_DEEP_LINKS.md**](docs/GIFT_DEEP_LINKS.md) — روابط الشحن التلقائي لأكواد الهدايا.

* [**docs/admin-gift-campaigns.md**](docs/admin-gift-campaigns.md) — إدارة الهدايا المتقدمة وحملات الإهداء الجماعية المباشرة.

* [**docs/wallet-debit-precision.md**](docs/wallet-debit-precision.md) — الدقة الحسابية في خصم المحفظة ومعالجة أخطاء RPC.

* [**docs/payments.md**](docs/payments.md) — نظام الشحن الذاتي عبر شام كاش + MacroDroid والشحن التلقائي.

* [**docs/daily_renewal.md**](docs/daily_renewal.md) — التجديد اليومي التلقائي الساعة 20:00 بتوقيت دمشق وإشعاراته.

### تجربة الطالب، التصدير، الكويز الجماعي، والامتثال القانوني

* [**docs/SESSION_NAVIGATION_2026-10-09.md**](docs/SESSION_NAVIGATION_2026-10-09.md) — حماية جلسات الكويز النشطة والتنقل الآمن.

* [**docs/mistake-practice.md**](docs/mistake-practice.md) — ميزة التدرب على الأخطاء ومراجعتها.

* [**docs/EXPORT_OPTIONS.md**](docs/EXPORT_OPTIONS.md) — إعدادات التصدير الإضافية، تقسيم العمودين، والمسودات.

* [**docs/EXPORT_SETTINGS_UX.md**](docs/EXPORT_SETTINGS_UX.md) — تصميم تجربة المستخدم لشاشة إعدادات التصدير والقوالب الجاهزة.

* [**docs/EXPORT_PRINT_512MB.md**](docs/EXPORT_PRINT_512MB.md) — هندسة تصدير الملفات المدمجة ضمن سقف ذاكرة 512MB.

* [**docs/GROUP_QUIZ_SAFETY.md**](docs/GROUP_QUIZ_SAFETY.md) — أمان الكويز الجماعي، قفل المحادثة، واستعادة الصلاحيات.

* [**docs/HANDOFF_CONSENT_RESUME_PHASE2.md**](docs/HANDOFF_CONSENT_RESUME_PHASE2.md) & [**docs/CONSENT_RESUME_PRODUCTION_CHECKLIST.md**](docs/CONSENT_RESUME_PRODUCTION_CHECKLIST.md) — بوابة الموافقة القانونية واستئناف الطلبات المعلّقة.

* [**docs/analytics.md**](docs/analytics.md) — نظام التحليلات وتتبّع الأخطاء وسجل نشاط الطالب.

### الألعاب التفاعلية (التحديات ولعبة Runner)

* [**docs/RUNNER_MVP.md**](docs/RUNNER_MVP.md)، [**docs/RUNNER_STORE.md**](docs/RUNNER_STORE.md)، [**docs/RUNNER_V2_HANDOFF.md**](docs/RUNNER_V2_HANDOFF.md)، [**docs/RUNNER_PRODUCTION_ROLLOUT.md**](docs/RUNNER_PRODUCTION_ROLLOUT.md) — توثيق لعبة «رحلة أبيض» التعليمية ومتجرها وخطة إطلاقها.

* [**docs/challenges/DESIGN.md**](docs/challenges/DESIGN.md) وملفات `docs/challenges/HANDOFF_*.md` — تصميم وتنفيذ بنك التحديات، هجمات الأراضي، المواجهات المباشرة، ووضع المشاهدة.