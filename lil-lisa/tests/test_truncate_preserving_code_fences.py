"""
Unit tests for fence-aware Slack/block truncation.

Run from lil-lisa:
    PYTHONPATH=src python3 tests/test_truncate_preserving_code_fences.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils import truncate_preserving_code_fences  # noqa: E402


class TruncatePreservingCodeFencesTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "Restart VDS."
        self.assertEqual(truncate_preserving_code_fences(text, 100), text)

    def test_even_fences_hard_slice_plus_ellipsis(self):
        text = "before " + "x" * 50
        out = truncate_preserving_code_fences(text, 20)
        self.assertEqual(out, text[:20] + "...")
        self.assertEqual(out.count("```"), 0)

    def test_cut_inside_fence_backs_up_to_before_fence(self):
        text = "intro\n```\ncode that is very long and should not be split\n```"
        max_length = text.find("very") + 4
        out = truncate_preserving_code_fences(text, max_length)
        self.assertIn("intro", out)
        self.assertTrue(out.endswith("..."))
        self.assertEqual(out.count("```"), 0)

    def test_truncation_inside_first_fence_closes_it(self):
        text = "```\n" + ("line\n" * 40) + "```"
        out = truncate_preserving_code_fences(text, 30)
        self.assertTrue(out.startswith("```"))
        self.assertTrue(out.endswith("\n```..."))
        self.assertEqual(out.count("```"), 2)


if __name__ == "__main__":
    unittest.main()
