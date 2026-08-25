-- ============================================================
-- Migration: Quiz Options (نوع المادة / نوع الأسئلة / الصعوبة)
-- يضيف الأعمدة اللازمة لدعم:
--   1) عرض تفاصيل كل كويز مخزّن بالكاش (نوع + صعوبة + عدد أسئلة)
--      بدل عرض الكويزات كوحدة واحدة متجانسة كما كان سابقاً.
--   2) سقف مستقل لكل تركيبة (نوع المادة × نوع الأسئلة × الصعوبة) بدل
--      سقف مشترك واحد لكل ملف (راجع idx_quizzes_combo أدناه، يُستخدم
--      لحساب عدد الكويزات ضمن نفس التركيبة بسرعة).
-- آمن للتشغيل عدة مرات (IF NOT EXISTS في كل مكان).
-- ============================================================

-- 1) نوع المادة المكتشف تلقائياً: 'math' | 'english' | 'other'
--    (يحل محل الاعتماد الحصري على is_math_quiz لتحديد نمط العرض)
ALTER TABLE quizzes
    ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL DEFAULT 'other';

-- 2) نوع الأسئلة الفعلي المختار من الطالب:
--    - قيمة ثابتة من القوائم الجاهزة حسب المادة (مثال: 'problems' لمادة رياضيات،
--      'grammar' لمادة إنجليزي)، أو 'custom' إذا كتب الطالب تفضيله الخاص نصاً،
--      أو 'general' كافتراضي عندما لا يختار الطالب أي تخصيص.
ALTER TABLE quizzes
    ADD COLUMN IF NOT EXISTS question_type TEXT NOT NULL DEFAULT 'general';

-- 3) نص وصفي قصير يُعرض للطالب بجانب كل كويز مخزّن بالكاش (عربي، جاهز للعرض
--    مباشرة بدون أي تحويل إضافي وقت الاستعلام) - يشمل حالة "custom" (تفضيل
--    الطالب النصي الحرّ) وحالة الاقتراحات المولّدة من AI للمواد غير المصنّفة.
ALTER TABLE quizzes
    ADD COLUMN IF NOT EXISTS question_type_label TEXT;

-- 4) مستوى الصعوبة: 'easy' | 'medium' | 'advanced'
ALTER TABLE quizzes
    ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT 'medium';

-- فهرس مركّب لتسريع حساب "كم كويز موجود بنفس تركيبة (ملف + نوع مادة + نوع
-- أسئلة + صعوبة)" - هذا الاستعلام يُنفَّذ في كل مرة يرسل فيها طالب نفس الملف
-- للتحقق من سقف تلك التركيبة تحديداً (سقف مستقل لكل تركيبة، وليس سقف مشترك
-- لكل الكويزات المخزنة للملف بغض النظر عن نوعها كما كان سابقاً).
CREATE INDEX IF NOT EXISTS idx_quizzes_combo
    ON quizzes(file_hash, subject_type, question_type, difficulty);

-- فهارس فردية إضافية مفيدة للتقارير الإدارية والفلترة المستقبلية
CREATE INDEX IF NOT EXISTS idx_quizzes_subject_type ON quizzes(subject_type);
CREATE INDEX IF NOT EXISTS idx_quizzes_difficulty ON quizzes(difficulty);

-- ملاحظة توافقية: العمود القديم is_math_quiz يبقى كما هو دون حذف (لا يزال
-- يُستخدم في مسارات أخرى مثل الكويز المصوّر LaTeX). بيانات الكويزات المخزّنة
-- سابقاً (قبل هذه الـ migration) ستُصنَّف تلقائياً كـ subject_type='other' و
-- question_type='general' و difficulty='medium' بفعل القيم الافتراضية أعلاه -
-- وهذا سلوك مقصود ومقبول لأنها كويزات "عامة" فعلاً من ناحية التصنيف الجديد.
