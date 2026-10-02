"""
Smoke test: يستورد كل وحدات المشروع ويتأكد أن الـ routers تُسجَّل.
لا يتصل بأي خدمة خارجية (متغيرات البيئة وهمية). يكشف: أخطاء استيراد، دورات، أسماء مفقودة.

التشغيل (من جذر المشروع):  python tests/smoke_import_test.py
"""
import importlib
import os
import pkgutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# قيم وهمية فقط لتجاوز فحوصات البيئة عند الاستيراد
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_SMOKE_ONLY")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

SKIP_DIRS = {"__pycache__", "tests", "logs", "assets", "webapp"}


def iter_modules():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(dirpath, ROOT)
        pkg = "" if rel == "." else rel.replace(os.sep, ".")
        for f in sorted(filenames):
            if not f.endswith(".py") or f == "__init__.py":
                continue
            name = f[:-3]
            yield f"{pkg}.{name}" if pkg else name
        if pkg and "__init__.py" in filenames:
            yield pkg


def main() -> int:
    failures = []
    ok = 0
    for mod in sorted(set(iter_modules())):
        if mod == "main":  # main يشغّل البوت عند التنفيذ؛ نستورده منفصلاً بالأسفل
            continue
        try:
            importlib.import_module(mod)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((mod, f"{type(exc).__name__}: {exc}"))

    # الـ routers
    try:
        import handlers
        for name in handlers.__all__:
            router = getattr(handlers, name)
            if not hasattr(router, "sub_routers"):
                failures.append((f"handlers.{name}", "ليس Router"))
    except Exception as exc:  # noqa: BLE001
        failures.append(("handlers(routers)", f"{type(exc).__name__}: {exc}"))

    print(f"imported OK: {ok}")
    if failures:
        print(f"FAILED: {len(failures)}")
        for m, e in failures:
            print(f"  - {m}: {e}")
        return 1
    print("ALL GOOD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
