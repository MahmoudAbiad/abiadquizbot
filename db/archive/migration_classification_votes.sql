-- ============================================================
-- Migration: Community-Verified Subject Classification
-- بدل التثبيت الفوري لنتيجة classify_subject (services/subject_classifier.py)
-- بمجرد رجوعها من AI (كانت تُخزَّن مباشرة بـ Redis TTL=7 أيام بلا أي تحقق بشري)،
-- هذه الهجرة تضيف طبقة تحقق مجتمعي دائمة:
--   1) classification_votes -> صوت واحد لكل (مستخدم، ملف) يقول "نعم/لا" على تصنيف
--      مُعرَّف مسبقاً (subject) شاهده هذا المستخدم فعلياً.
--   2) classification_locks -> بمجرد أن يصل تصنيف محدد (نفس subject) لعدد أصوات
--      "نعم" >= CLASSIFICATION_VOTE_THRESHOLD (راجع constants.py) من مستخدمين
--      مختلفين على نفس الملف، يُثبَّت هنا نهائياً (لا TTL، لا يُمسح) - classify_subject
--      يتحقق من هذا الجدول أولاً قبل أي كاش Redis أو استدعاء AI جديد.
-- آمن للتشغيل عدة مرات (IF NOT EXISTS في كل مكان).
-- ============================================================

-- 1) التصويتات الخام - صوت واحد فقط لكل (file_hash, user_id) عبر القيد الفريد أدناه،
--    يمنع تصويت نفس المستخدم عدة مرات على نفس الملف (سواء نعم أو لا).
CREATE TABLE IF NOT EXISTS classification_votes (
    id BIGSERIAL PRIMARY KEY,
    file_hash TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('yes', 'no')),
    subject TEXT NOT NULL,              -- قيمة classification.subject التي شاهدها المستخدم وقت التصويت
    classification_data JSONB,          -- لقطة كاملة لنتيجة SubjectClassification وقت التصويت (لتثبيتها لاحقاً لو فازت)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (file_hash, user_id)
);

CREATE INDEX IF NOT EXISTS idx_classification_votes_file_subject
    ON classification_votes(file_hash, subject);

-- 2) التصنيفات المثبّتة نهائياً - بمجرد إدخال صف هنا، classify_subject لا يستدعي
--    AI مرة أخرى لهذا الملف إطلاقاً (راجع services/subject_classifier.py).
CREATE TABLE IF NOT EXISTS classification_locks (
    file_hash TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    classification_data JSONB NOT NULL, -- نتيجة SubjectClassification الكاملة الجاهزة لإعادة استخدامها مباشرة
    yes_count INT NOT NULL DEFAULT 0,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3) دالة تصويت ذرّية (Atomic RPC) - نفس فلسفة vote_on_quiz الموجودة أصلاً (منع تكرار
--    + حساب فوري بضربة واحدة بدل عدة استعلامات متتالية من جهة تطبيق البوت عرضة لتضارب
--    السباق (Race Condition) لو صوّت عدة طلاب بنفس اللحظة تقريباً على نفس الملف).
--    ترجع JSONB بمفاتيح:
--      duplicate  -> true لو المستخدم صوّت مسبقاً على هذا الملف (لم يُسجَّل شيء جديد)
--      locked_now -> true لو هذا التصويت بالذات كان السبب بوصول العداد للعتبة وتثبيت التصنيف
--      yes_count  -> عدد أصوات "نعم" الحالي لنفس (file_hash, subject) بعد هذا التصويت
CREATE OR REPLACE FUNCTION vote_on_classification(
    p_file_hash TEXT,
    p_user_id BIGINT,
    p_vote TEXT,
    p_subject TEXT,
    p_classification_data JSONB,
    p_threshold INT DEFAULT 3
) RETURNS JSONB AS $$
DECLARE
    v_yes_count INT := 0;
    v_locked_now BOOLEAN := false;
BEGIN
    -- محاولة إدخال الصوت؛ لو موجود مسبقاً لنفس (file_hash, user_id) القيد الفريد يمنعه.
    BEGIN
        INSERT INTO classification_votes (file_hash, user_id, vote, subject, classification_data)
        VALUES (p_file_hash, p_user_id, p_vote, p_subject, p_classification_data);
    EXCEPTION WHEN unique_violation THEN
        RETURN jsonb_build_object('duplicate', true, 'locked_now', false, 'yes_count', 0);
    END;

    IF p_vote <> 'yes' THEN
        RETURN jsonb_build_object('duplicate', false, 'locked_now', false, 'yes_count', 0);
    END IF;

    SELECT COUNT(*) INTO v_yes_count
    FROM classification_votes
    WHERE file_hash = p_file_hash AND subject = p_subject AND vote = 'yes';

    -- IF NOT EXISTS يمنع محاولة تثبيت مزدوجة لو كان مثبّتاً أصلاً (مثلاً سباق نادر جداً
    -- بين طلبين وصلا للعتبة بنفس اللحظة تقريباً) - PRIMARY KEY على file_hash يحمي أيضاً.
    IF v_yes_count >= p_threshold AND NOT EXISTS (
        SELECT 1 FROM classification_locks WHERE file_hash = p_file_hash
    ) THEN
        BEGIN
            INSERT INTO classification_locks (file_hash, subject, classification_data, yes_count)
            VALUES (p_file_hash, p_subject, p_classification_data, v_yes_count);
            v_locked_now := true;
        EXCEPTION WHEN unique_violation THEN
            v_locked_now := false; -- طلب موازٍ سبقنا بالتثبيت بنفس اللحظة - لا مشكلة
        END;
    END IF;

    RETURN jsonb_build_object('duplicate', false, 'locked_now', v_locked_now, 'yes_count', v_yes_count);
END;
$$ LANGUAGE plpgsql;
