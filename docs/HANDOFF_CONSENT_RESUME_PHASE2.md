# HANDOFF — استئناف الطلب الأصلي بعد الموافقة على الشروط (المرحلة 2)

## المشكلة
بعد إضافة بوابة الموافقة (`services/consent.py::ConsentMiddleware`)، الطالب يرسل ملفاً/نصاً/صورة،
تظهر له رسالة الموافقة، يضغط «أوافق»، لكن الرسالة الأصلية كانت تضيع (البوابة تبتلعها ولا يُحفظ
إلا payload الـ /start). كان على الطالب إعادة الإرسال.

## ما تم في المرحلة 1 (منفَّذ + مُختبَر بوحدات اختبار معزولة)
الملفات المعدّلة: `services/consent.py`, `handlers/consent.py`, و`tests/test_consent_resume.py` (جديد).

1. `services/consent.py`
   - ثوابت: `_PENDING_TTL = 3600`، `_PENDING_MAX_ALBUM = 10`.
   - `remember_pending_message(message)`: عند حجب رسالة خاصة (أي حالة غير OK) تُخزَّن كـ JSON
     في قائمة Redis `consent:pending:{user_id}` (TTL ساعة).
     * رسالة عادية تستبدل المعلّقة السابقة (الأحدث فقط).
     * أجزاء الألبوم (`media_group_id`) تتجمع معاً (حد 10).
     * `/start` بكل أشكاله لا يُخزَّن (له مساره: زر «▶️ متابعة» عبر `remember_start_payload`).
   - `pop_pending_messages(user_id, bot)` سحب ذرّي (pipeline) + إعادة بناء `Message` عبر `model_validate_json(...).as_(bot)`.
   - `replay_pending_messages(bot, messages)` يمرّرها إلى `dp.feed_update(bot, Update(...))` بالترتيب؛
     فشل رسالة لا يوقف البقية.
   - `clear_pending_messages(user_id)`, و`build_accepted_text(resuming=...)` ("⏳ جارٍ متابعة طلبك السابق…").
   - الـ middleware يستدعي `remember_pending_message` قبل إظهار شاشة الموافقة (حتى لو كُتم الإشعار بسبب `_NOTICE_TTL`).
2. `handlers/consent.py`
   - عند «أوافق»: بعد `remember_accepted` تُسحب الرسائل المعلّقة، يُعدَّل نص الرسالة، ثم `spawn(replay_pending_messages)`.
     تعمل أيضاً عند ضغط «أوافق» مرتين (`changed=False`).
   - عند «لا أوافق»: `clear_pending_messages` (خصوصية).

## ما لم يُنفَّذ (مهام المرحلة 2)
1. **أزرار inline (callback_query)**: عند الضغط على زر قبل الموافقة يتم تجاهل الضغطة ولا تُستأنف.
   المطلوب تصميم استئناف آمن: نحفظ `callback_data` + `message_id` فقط، وبعد الموافقة نعيد
   تنفيذ المعالج دون `call.answer()` القديم (انتهى `query id`) أو نرسل رسالة "اضغط الزر مجدداً" مع إعادة عرض الشاشة.
   انتبه: `ThrottlingMiddleware` و`PracticeCallbackGuard` و`UsageCallbackMiddleware` تلفّ callback_query.
2. **اختبار تكاملي حقيقي** (لم يكن aiogram مثبتاً في بيئة المرحلة 1 فاختُبرت الدوال معزولة عبر `tests/reliability_support.py::load_functions`):
   شغّل البوت محلياً/على `Test_telegramquizbot` وتحقق يدوياً من الحالات:
   - طالب جديد يرسل PDF ← موافقة ← يبدأ التوليد تلقائياً.
   - طالب قديم (STATE_UPDATE) يرسل نصاً/صورة/صوت/ملف Excel.
   - ألبوم صور (3+ صور) ← الموافقة ← تجميع الألبوم في `handlers/files.py` (يستخدم Redis + مؤقت).
   - رسالة يتوقعها معالج FSM (مثل اسم الاختبار/الجهة في `group_quiz_cert_router`، وحالات `waiting_for_*`).
3. **التداخل مع الحماية**:
   - `ThrottlingMiddleware(limit=1.2)` على الرسائل: إعادة التمرير تمر عبره؛ لو ضغط الطالب «أوافق»
     خلال 1.2 ثانية من آخر رسالة قد تُتجاهل الرسالة المعادة بصمت. فكّر بإعفاء الرسائل المعادة (علامة في `data`/`Update`) أو `cache.pop`.
   - `MaintenanceMiddleware`/`ErrorTrackingMiddleware` (outer) تعمل على `feed_update` المعاد؛ تأكد من عدم تسجيل خطأ مضلل.
   - `update_id` المُولَّد اصطناعي (`ms % 2e9`)؛ تأكد أنه لا يصطدم بإزالة التكرار في `services/webhook_queue.py` (يعمل عند enqueue فقط، لذا غالباً آمن).
4. **سلامة الاستئناف**: عدم تكرار التنفيذ لو ضغط الطالب «أوافق» من نسختين (السحب ذرّي لكن تحقق)،
   وماذا يحدث لو انتهت صلاحية `file_id`/الملف المرفوع. اعرض رسالة واضحة بدل الصمت عند فشل الاستئناف
   (حالياً يُسجَّل الخطأ فقط في `replay_pending_messages`).
5. **القيود/الخصوصية**: الرسائل المعلّقة تُخزَّن في Redis ساعة واحدة وتُحذف عند الرفض. راجع `tests/test_logging_privacy.py`
   وقرّر هل يلزم ذكرها في سياسة الخصوصية. قيمة الـ TTL قابلة للتعديل (`_PENDING_TTL`).
6. شغّل الحزمة كاملة: `cd tests && python -m unittest discover` ثم أضف اختبارات للـ handler (`handlers/consent.py::_decide`).

## قيود المشروع (من المالك)
لا تغيّر نقاط الدفع/المدفوعات، لا push ولا PR، اعمل داخل نسخة master المرفقة فقط.



