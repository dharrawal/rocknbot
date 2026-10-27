"""Unit tests for the Slack-free wrappers in src/techsupport_cron.py."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import techsupport_cron


class TechsupportCronHttpWrapperTests(unittest.TestCase):
    def test_channel_sync_plan_delegates_under_lock(self) -> None:
        with patch.object(
            techsupport_cron,
            "get_channel_sync_plan",
            return_value={"due": True, "channel_id": "C_TS"},
        ) as plan_mock:
            res = techsupport_cron.channel_sync_plan(
                "C_TS", is_product_channel=True, product="IDA", force=True
            )
        self.assertEqual(res, {"due": True, "channel_id": "C_TS"})
        plan_mock.assert_called_once_with(
            "C_TS", is_product_channel=True, product="IDA", force=True
        )

    def test_checkpoint_channel_discovery_delegates_under_lock(self) -> None:
        with patch.object(
            techsupport_cron,
            "record_channel_discovery",
            return_value={"candidate_thread_ids": ["1.0"]},
        ) as rec_mock:
            res = techsupport_cron.checkpoint_channel_discovery(
                "C_TS",
                is_product_channel=False,
                scan_timestamp="100.0",
                catchup_due=True,
                new_threads={"1.0": "1.0"},
                refreshed_threads={},
            )
        self.assertEqual(res, {"candidate_thread_ids": ["1.0"]})
        rec_mock.assert_called_once_with(
            "C_TS",
            is_product_channel=False,
            scan_timestamp="100.0",
            catchup_due=True,
            new_threads={"1.0": "1.0"},
            refreshed_threads={},
        )

    def test_process_thread_payload_delegates_under_lock(self) -> None:
        with patch.object(
            techsupport_cron,
            "process_formatted_thread",
            return_value={"outcome": "added", "changed": True},
        ) as proc_mock:
            res = techsupport_cron.process_thread_payload(
                "C_IDA", "1.0", "[1.0] Alice: hi", product="IDA"
            )
        self.assertEqual(res, {"outcome": "added", "changed": True})
        proc_mock.assert_called_once_with(
            "C_IDA", "1.0", "[1.0] Alice: hi", product="IDA"
        )

    def test_finalize_run_records_last_run_and_invokes_reload_callback(self) -> None:
        reload_cb = MagicMock()
        with patch.object(
            techsupport_cron,
            "finalize_pipeline_run",
            return_value={"step_errors": []},
        ) as fin_mock:
            res = techsupport_cron.finalize_run(
                changed_this_run=True,
                run_maintenance=True,
                reload_retrievers=reload_cb,
            )
        self.assertEqual(res, {"step_errors": []})
        fin_mock.assert_called_once_with(
            changed_this_run=True,
            run_maintenance=True,
            reload_callback=reload_cb,
        )
        self.assertEqual(techsupport_cron.last_run_status()["failed"], False)


if __name__ == "__main__":
    unittest.main()
