"""
Unit tests for pipeline_summary.format_pipeline_counts.

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_format_pipeline_counts.py
"""

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from pipeline_summary import format_pipeline_counts  # noqa: E402


class FormatPipelineCountsTests(unittest.TestCase):
    def _sample_counts(self):
        return {
            "checked": 10,
            "added": 1,
            "enriched": 4,
            "replaced": 2,
            "left_as_is_not_useful": 0,
            "left_as_is_not_conclusive": 0,
            "skipped_not_useful": 3,
            "skipped_not_conclusive": 0,
            "errored": 0,
        }

    def test_includes_enriched_between_added_and_replaced(self):
        rendered = format_pipeline_counts(self._sample_counts())
        self.assertIn("enriched=4", rendered)
        added_at = rendered.index("added=1")
        enriched_at = rendered.index("enriched=4")
        replaced_at = rendered.index("replaced=2")
        self.assertLess(added_at, enriched_at)
        self.assertLess(enriched_at, replaced_at)

    def test_enrich_only_night_is_visible(self):
        counts = self._sample_counts()
        counts["added"] = 0
        counts["replaced"] = 0
        counts["enriched"] = 7
        rendered = format_pipeline_counts(counts)
        self.assertIn("added=0", rendered)
        self.assertIn("enriched=7", rendered)
        self.assertIn("replaced=0", rendered)

    def test_omit_checked_for_admin_alert_parenthetical(self):
        rendered = format_pipeline_counts(self._sample_counts(), omit=("checked",))
        self.assertNotIn("checked=", rendered)
        self.assertTrue(rendered.startswith("added=1"))
        self.assertIn("enriched=4", rendered)

    def test_unknown_keys_are_appended_sorted(self):
        counts = self._sample_counts()
        counts["zeta_new"] = 9
        counts["alpha_new"] = 8
        rendered = format_pipeline_counts(counts)
        self.assertTrue(rendered.endswith("alpha_new=8 zeta_new=9"))
        self.assertLess(rendered.index("errored=0"), rendered.index("alpha_new=8"))


if __name__ == "__main__":
    unittest.main()
