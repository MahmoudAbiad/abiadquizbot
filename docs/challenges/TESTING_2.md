# التحقق من المرحلة الثانية

تاريخ التشغيل: 2026-10-04. لا اتصال حي بـ Telegram أو Gemini أو تعديل Supabase خلال الاختبارات.

## Python وSmoke

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python tests/smoke_import_test.py
```

نتائج بيئة التحقق المعزولة Python 3.12:

- `unittest discover`: 243 اختبارًا، **OK** (230 من النسخة الأصلية + 13 جديدة).
- `test_challenge*`: 38 اختبارًا للمرحلتين.
- `smoke_import_test.py`: **imported OK: 100 — ALL GOOD**؛ الاستيراد وتسجيل الراوترات مع بيانات بيئة وهمية، لا تشغيل بوت فعلي.
- أضيف `socksio==1.0.0` إلى بيئة التحقق المحلية فقط لأن بيئة التنفيذ تستخدم SOCKS proxy. ليس تغييرًا على requirements الخاصة بالمشروع.
- compile للأجزاء المعدلة و`git diff --check`: ناجحان.

## SQL محلي مستقل

المطلوب Node.js وأداة PGlite معزولة عن تطبيق البوت:

```bash
repo_path="$(pwd)"
challenge_test_dir="$(mktemp -d)"
npm install --prefix "$challenge_test_dir" --no-audit --no-fund @electric-sql/pglite@0.3.10
cp tests/sql/run_challenge_game.mjs "$challenge_test_dir/run_challenge_game.mjs"
node "$challenge_test_dir/run_challenge_game.mjs" "$repo_path"
```

النتيجة: **31 مجموعة سيناريو SQL ناجحة**، وتشمل المجموعة الأولى اختبارات المرحلة الأولى الـ12.
المحرك: PostgreSQL 17.5 (WASM/Emscripten، PGlite 0.3.10). تنشأ قاعدة مؤقتة ببيانات مصطنعة، ثم يغلقها السكربت. لا أسرار أو DSN مطلوبان. لا تستخدم هذا السكربت لنشر migrations على Supabase.

اختُبرت grants بأدوار anon/authenticated/service_role، وSnapshot، والإجابة المتكررة والمتأخرة، وإنهاء الهجوم، والنجوم والمكافآت والسقوف، والاستقلال عن paid points، والـ rollback مع فشل متعمد قبل إكمال التحديثات، وإعادة المحاولة بعد إزالة الفشل. اختُبرت إعادة تطبيق migration والـ rollback/reapply أيضًا.

## غير مثبت بالاختبارات المحلية

- PGlite اتصال واحد؛ لا يثبت سلوك التزامن بين اتصالين. قبل النشر، اختبر طلبين متزامنين للإجابة/الإنهاء، وهجومين متعاكسين، وworkers للإشعارات على PostgreSQL/Supabase الاختباري.
- API الحقيقي لـ PostgREST، صلاحيات المشروع الحقيقية، Telegram edit/send ومؤقتاته في الشبكة، إعادة التشغيل الفعلية لخادم البوت، والتحميل الكبير: تحتاج التجربة اليدوية المدرجة في `HANDOFF_2.md`.
- اختبارات الواجهة تستعمل أجزاء الإنتاج مع dependencies معزولة؛ لا تدّعي محاكاة Telegram كاملة.
