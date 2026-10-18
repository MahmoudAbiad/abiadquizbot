"""Historical entry point retained; run the multi-provider replacement checks.

The original harness depended on removed Google client globals. This suite now
covers equivalent public results/metadata and the new provider boundary offline.
"""
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(pathlib.Path(__file__).parent), pattern="test_ai_providers.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
