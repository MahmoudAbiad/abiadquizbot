-- ✅ تم تطبيقها مباشرة على Supabase (مشروع pqawoanhbgvdtbdhnbod) عبر MCP - ما في شي
-- مطلوب منك تشغيله يدوياً. مرفقة هون فقط للتوثيق ولو حبيت تطبقها ببيئة تانية.

CREATE TABLE IF NOT EXISTS ai_model_pricing (
    provider text NOT NULL,
    model_name text NOT NULL,
    input_price_per_million numeric NOT NULL DEFAULT 0,
    output_price_per_million numeric NOT NULL DEFAULT 0,
    -- توكنز التفكير (thoughts_tokens) بتُحاسَب بسعر الإخراج (نفس منطق Google الفعلي:
    -- output pricing يشمل thinking tokens ضمنياً).
    verified boolean NOT NULL DEFAULT false,
    notes text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model_name)
);

COMMENT ON TABLE ai_model_pricing IS 'أسعار الموديلات بالدولار لكل مليون توكن (إدخال/إخراج) - تُستخدم لحساب تكلفة كل كويز تقديرياً بلوحة الأدمن. verified=false يعني السعر تقريبي من بحث خارجي ولسا ما تأكّد من فاتورة حقيقية.';

INSERT INTO ai_model_pricing (provider, model_name, input_price_per_million, output_price_per_million, verified, notes) VALUES
    ('gemini', 'gemini-3.8-flash', 0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 (بيتضاعف بعدها) - غير مؤكد من فاتورتك'),
    ('vertex', 'gemini-3.8-flash', 0.75, 3.75, false, 'نفس سعر AI Studio تقريباً حسب المصادر - غير مؤكد من فاتورة Vertex الفعلية'),
    ('gemini', 'gemini-3.7-flash', 0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 - غير مؤكد من فاتورتك'),
    ('gemini', 'gemini-3.6-flash', 0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 - غير مؤكد من فاتورتك'),
    ('gemini', 'gemini-3.5-flash', 1.50, 9.00, false, 'رقم من مصدر واحد غير رسمي بحثت فيه - غير موثوق كفاية، تأكد منه'),
    ('gemini', 'gemini-3.5-flash-lite', 0.30, 2.50, false, 'مصادر متضاربة بشدة على هالموديل (شفت أرقام من 0.10 لـ0.30) - لازم تتأكد بنفسك'),
    ('groq', 'openai/gpt-oss-120b', 0.15, 0.75, false, 'سعر Groq الرسمي المتداول - أعلى ثقة نسبياً من باقي الصفوف، بس ما تأكدت من فاتورتك مباشرة')
ON CONFLICT (provider, model_name) DO NOTHING;
