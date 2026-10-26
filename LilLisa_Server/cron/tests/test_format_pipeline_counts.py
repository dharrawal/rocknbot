"""
Unit tests for pipeline_summary.format_pipeline_counts.

Run from LilLisa_Server/cron:
    PYTHONPATH=. python3 tests/test_format_pipeline_counts.py
"""

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from pipeline_summary import format_pipeline_counts, format_pipeline_error_alert  # noqa: E402


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

    def test_product_channel_counts_render_in_order(self):
        # The product-channel pass has no "enriched" and adds "corrected" plus
        # the two expert gates -- every key is optional, none may vanish.
        counts = {
            "checked": 5,
            "corrected": 2,
            "added": 1,
            "replaced": 0,
            "left_as_is_not_useful": 0,
            "left_as_is_not_conclusive": 0,
            "skipped_no_expert_reply": 2,
            "skipped_no_expert_insight": 3,
            "skipped_not_useful": 0,
            "skipped_not_conclusive": 0,
            "errored": 0,
        }
        rendered = format_pipeline_counts(counts)
        self.assertNotIn("enriched", rendered)
        self.assertIn("corrected=2", rendered)
        self.assertIn("skipped_no_expert_reply=2", rendered)
        self.assertIn("skipped_no_expert_insight=3", rendered)
        self.assertLess(rendered.index("added=1"), rendered.index("corrected=2"))
        self.assertLess(rendered.index("corrected=2"), rendered.index("replaced=0"))
        self.assertLess(
            rendered.index("skipped_no_expert_reply=2"), rendered.index("skipped_no_expert_insight=3")
        )
        self.assertLess(rendered.index("skipped_no_expert_insight=3"), rendered.index("skipped_not_useful=0"))

    def test_extra_product_metadata_still_shows_up(self):
        rendered = format_pipeline_counts({"checked": 0, "channel_id": "C_IDA", "skipped_reason": "no_experts"})
        self.assertIn("channel_id=C_IDA", rendered)
        self.assertIn("skipped_reason=no_experts", rendered)

    def test_unknown_keys_are_appended_sorted(self):
        counts = self._sample_counts()
        counts["zeta_new"] = 9
        counts["alpha_new"] = 8
        rendered = format_pipeline_counts(counts)
        self.assertTrue(rendered.endswith("alpha_new=8 zeta_new=9"))
        self.assertLess(rendered.index("errored=0"), rendered.index("alpha_new=8"))


class FormatPipelineErrorAlertTests(unittest.TestCase):
    """pr42-ujb: the admin alert headline and parenthetical count the same failures."""

    def _techsupport_counts(self, **overrides):
        counts = {
            "checked": 0,
            "added": 0,
            "enriched": 0,
            "replaced": 0,
            "left_as_is_not_useful": 0,
            "left_as_is_not_conclusive": 0,
            "skipped_not_useful": 0,
            "skipped_not_conclusive": 0,
            "errored": 0,
        }
        counts.update(overrides)
        return counts

    def _product_counts(self, **overrides):
        counts = {
            "checked": 0,
            "corrected": 0,
            "added": 0,
            "replaced": 0,
            "skipped_no_expert_reply": 0,
            "skipped_no_expert_insight": 0,
            "errored": 0,
        }
        counts.update(overrides)
        return counts

    def test_two_unresolved_expert_groups_are_channel_failures(self):
        # The reported admin-channel line: both products fail before any thread
        # is scanned, so the headline used to say "2 thread(s) errored out of 0
        # checked" while the parenthetical, built from the techsupport loop
        # alone, said errored=0.
        errors = [
            {"thread_ts": "channel C07E5K0UD53", "error": "[IDA] missing_scope"},
            {"thread_ts": "channel C07EXE7D116", "error": "[IDDM] missing_scope"},
        ]
        text = format_pipeline_error_alert(
            "nightly_pipeline.py",
            errors,
            self._techsupport_counts(),
            self._product_counts(errored=2),
        )
        self.assertTrue(text.startswith("nightly_pipeline.py: 2 product channel(s) failed ("))
        self.assertIn("errored=2", text)
        self.assertNotIn("errored=0", text)
        self.assertNotIn("thread(s)", text)
        self.assertIn("- channel C07E5K0UD53: [IDA] missing_scope", text)
        self.assertIn("- channel C07EXE7D116: [IDDM] missing_scope", text)
        # Zero product-only keys stay out; the techsupport parenthetical stays.
        self.assertNotIn("corrected=", text)
        self.assertIn("enriched=0", text)
        self.assertNotIn("checked=", text.split("\n", 1)[0])

    def test_thread_errors_keep_the_thread_wording(self):
        text = format_pipeline_error_alert(
            "nightly_pipeline.py",
            [{"thread_ts": "100.0", "error": "lancedb down"}],
            self._techsupport_counts(checked=4, errored=1),
            self._product_counts(),
        )
        self.assertIn("1 thread(s) errored out of 4 checked", text)
        self.assertNotIn("product channel", text)
        self.assertIn("errored=1", text)
        self.assertNotIn("corrected=", text)

    def test_thread_and_channel_failures_are_both_in_the_total(self):
        errors = [
            {"thread_ts": "100.0", "error": "lancedb down"},
            {"thread_ts": "channel C_IDA", "error": "[IDA] missing_scope"},
            {"thread_ts": "channel C_IDDM", "error": "[IDDM] missing_scope"},
        ]
        text = format_pipeline_error_alert(
            "nightly_pipeline.py",
            errors,
            self._techsupport_counts(checked=4, errored=1),
            self._product_counts(errored=2, corrected=2),
        )
        self.assertIn(
            "1 thread(s) errored out of 4 checked; 2 product channel(s) failed",
            text,
        )
        self.assertIn("errored=3", text)
        self.assertIn("corrected=2", text)


if __name__ == "__main__":
    unittest.main()
