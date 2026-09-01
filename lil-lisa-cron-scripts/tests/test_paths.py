"""
Unit tests for lil-lisa-cron-scripts path layout.

Run from lil-lisa-cron-scripts:
    python3 tests/test_paths.py
"""

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))

import paths  # noqa: E402


class PathsTests(unittest.TestCase):
    def test_package_root_is_cron_scripts_dir(self):
        self.assertEqual(paths.PACKAGE_ROOT, PACKAGE_ROOT)
        self.assertTrue((paths.PACKAGE_ROOT / "nightly_pipeline.py").is_file())

    def test_server_root_is_sibling_by_default(self):
        self.assertEqual(paths.LILLISA_SERVER_ROOT, (PACKAGE_ROOT.parent / "LilLisa_Server").resolve())
        self.assertEqual(
            paths.LILLISA_SERVER_ENV_PATH,
            paths.LILLISA_SERVER_ROOT / "env" / "lillisa_server.env",
        )

    def test_thread_tags_stay_on_the_server_tree(self):
        self.assertEqual(
            paths.THREAD_TAGS_PATH,
            paths.LILLISA_SERVER_ROOT / "scripts" / "techsupport_thread_tags.json",
        )


if __name__ == "__main__":
    unittest.main()
