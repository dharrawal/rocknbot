"""Unit tests for the Slack-free backend entry points in nightly_pipeline.py.

Covers:
  - get_channel_sync_plan (due gating, initial lookback seeding for product
    channels, hot + catch-up lookup_ids selection)
  - record_channel_discovery (new/refreshed thread state persistence,
    PRODUCT_SCAN_MAX_THREADS_PER_RUN cap + pending_thread_ids deferral)
  - process_formatted_thread (techsupport add/enrich/replace + product
    corrected/added/replaced/skipped_no_expert_insight, immediate state save)
  - finalize_pipeline_run (in-process reload callback, GitHub push, review sync,
    contextual re-embed, step_errors collection)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

CRON_DIR = Path(__file__).resolve().parent.parent
if str(CRON_DIR) not in sys.path:
    sys.path.insert(0, str(CRON_DIR))

import nightly_pipeline as pipeline  # noqa: E402
import nightly_techsupport_sync as sync_mod  # noqa: E402


class SlackFreePipelineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "techsupport_sync_state.json"
        patcher = patch.object(sync_mod, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _saved_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_get_channel_sync_plan_seeds_product_channel_lookback_on_first_run(self) -> None:
        now = 1_700_000_000.0
        with patch.object(pipeline, "get_scan_product_channels", return_value=True), patch.object(
            pipeline, "get_product_scan_initial_lookback_days", return_value=10.0
        ), patch("nightly_pipeline.time.time", return_value=now):
            plan = pipeline.get_channel_sync_plan("C_IDA", is_product_channel=True, product="IDA", now=now)

        self.assertTrue(plan["due"])
        expected_oldest = f"{now - 10.0 * 86400:.6f}"
        self.assertEqual(plan["last_run_timestamp"], expected_oldest)
        self.assertEqual(plan["known_thread_ids"], [])
        self.assertEqual(plan["lookup_ids"], [])

    def test_get_channel_sync_plan_skips_when_interval_not_elapsed(self) -> None:
        now = 1_700_000_000.0
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "channels": {"C_TS": {"last_run_timestamp": f"{now - 60:.6f}", "threads": {}}},
                }
            ),
            encoding="utf-8",
        )
        with patch.object(pipeline, "get_sync_interval_hours", return_value=24.0), patch(
            "nightly_pipeline.time.time", return_value=now
        ):
            plan = pipeline.get_channel_sync_plan("C_TS", is_product_channel=False, force=False, now=now)
            forced = pipeline.get_channel_sync_plan("C_TS", is_product_channel=False, force=True, now=now)

        self.assertFalse(plan["due"])
        self.assertEqual(plan["reason"], "interval_not_elapsed")
        self.assertTrue(forced["due"])

    def test_record_channel_discovery_updates_state_and_defers_product_overflow(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "channels": {
                        "C_IDA": {
                            "last_run_timestamp": "100.0",
                            "threads": {
                                "100.0": {"last_seen_reply_ts": "100.0"},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch.object(pipeline, "get_product_scan_max_threads_per_run", return_value=1):
            result = pipeline.record_channel_discovery(
                "C_IDA",
                is_product_channel=True,
                scan_timestamp="500.000000",
                catchup_due=True,
                new_threads={"300.0": "350.0"},
                refreshed_threads={"100.0": "200.0"},
            )

        self.assertEqual(result["new_thread_ids"], ["300.0"])
        self.assertEqual(result["updated_thread_ids"], ["100.0"])
        # Hottest first (350.0 > 200.0), capped at 1.
        self.assertEqual(result["candidate_thread_ids"], ["300.0"])
        self.assertEqual(result["deferred_thread_ids"], ["100.0"])

        saved = self._saved_state()["channels"]["C_IDA"]
        self.assertEqual(saved["last_run_timestamp"], "500.000000")
        self.assertEqual(saved["last_catchup_timestamp"], "500.000000")
        self.assertEqual(saved["pending_thread_ids"], ["100.0"])
        self.assertEqual(saved["threads"]["300.0"]["last_seen_reply_ts"], "350.0")
        self.assertEqual(saved["threads"]["100.0"]["last_seen_reply_ts"], "200.0")

    def test_process_formatted_thread_techsupport_persists_added_immediately(self) -> None:
        with patch.object(pipeline, "get_related_entry_title", return_value=None), patch.object(
            pipeline,
            "classify_formatted_thread",
            return_value={"is_useful": True, "is_conclusive": True, "conversation_thread": "[1.0] Alice: fixed"},
        ), patch.object(pipeline, "add_verified_qa_pair", return_value={}) as add_mock:
            res = pipeline.process_formatted_thread("C_TS", "1.0", "[1.0] Alice: fixed")

        self.assertEqual(res["outcome"], "added")
        self.assertTrue(res["changed"])
        add_mock.assert_called_once_with("[1.0] Alice: fixed", thread_ts="1.0")
        self.assertTrue(self._saved_state()["channels"]["C_TS"]["threads"]["1.0"]["added_to_verified_db"])

    def test_process_formatted_thread_product_correction_persists_outcome(self) -> None:
        with patch.object(pipeline, "has_expert_insight", return_value=True), patch.object(
            pipeline, "get_cited_entry_title", return_value="Existing Entry"
        ), patch.object(
            pipeline,
            "classify_formatted_thread",
            return_value={"is_useful": True, "is_conclusive": None, "conversation_thread": "[2.0] Bob (expert): fix"},
        ), patch.object(pipeline, "correct_verified_entry", return_value={}) as correct_mock:
            res = pipeline.process_formatted_thread(
                "C_IDA", "2.0", "[2.0] Bob (expert): fix", product="IDA"
            )

        self.assertEqual(res["outcome"], "corrected")
        self.assertTrue(res["changed"])
        correct_mock.assert_called_once_with(
            "Existing Entry",
            "[2.0] Bob (expert): fix",
            source_channel_id="C_IDA",
            source_thread_ts="2.0",
        )
        saved_thread = self._saved_state()["channels"]["C_IDA"]["threads"]["2.0"]
        self.assertTrue(saved_thread["added_to_verified_db"])
        self.assertEqual(saved_thread["outcome"], "corrected")

    def test_finalize_pipeline_run_uses_in_process_reload_callback(self) -> None:
        reload_cb = MagicMock()
        with patch.object(pipeline, "push_verified_qa_pairs", return_value={"pushed": True}), patch.object(
            pipeline, "sync_edited_entries", return_value={"updated": 0}
        ), patch.object(pipeline, "run_reembed_if_due", return_value={"ran": False}):
            summary = pipeline.finalize_pipeline_run(
                changed_this_run=True,
                run_maintenance=True,
                reload_callback=reload_cb,
            )

        reload_cb.assert_called_once_with()
        self.assertEqual(summary["techsupport_index_reload_after_ingest"], {"reloaded": True, "mode": "in_process"})
        self.assertEqual(summary["step_errors"], [])


if __name__ == "__main__":
    unittest.main()
