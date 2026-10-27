"""Unit tests for lil-lisa/src/techsupport_harvester.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from expert_group import ExpertLookupError  # noqa: E402
import techsupport_harvester as harvester  # noqa: E402


class TechsupportHarvesterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "TECHSUPPORT_CHANNEL_ID": "C_TS",
            "CHANNEL_ID_IDA": "C_IDA",
            "ADMIN_CHANNEL_ID": "C_ADMIN",
            "LIL_LISA_SLACK_USERID": "U_BOT",
            "LIL_LISA_SERVER_URL": "http://backend.test",
            "AUTHENTICATION_KEY": "secret",
        }
        self.backend = MagicMock()

    def test_product_thread_without_expert_reply_is_skipped_before_backend_call(self) -> None:
        self.backend.get_channel_state.side_effect = [
            {"due": False, "reason": "interval_not_elapsed"},
            {
                "due": True,
                "last_run_timestamp": "100.0",
                "known_thread_ids": [],
                "lookup_ids": [],
                "catchup_due": False,
                "scan_timestamp": "200.0",
            },
        ]
        self.backend.checkpoint_channel_discovery.return_value = {
            "new_thread_ids": ["150.0"],
            "updated_thread_ids": [],
            "candidate_thread_ids": ["150.0"],
            "deferred_thread_ids": [],
        }
        self.backend.finalize_run.return_value = {"step_errors": []}

        messages = [
            {"ts": "150.0", "user": "U_USER", "text": "question"},
            {"ts": "151.0", "user": "U_BOT", "text": "bot answer"},
        ]
        with patch.object(harvester, "find_new_threads", return_value={"150.0": "151.0"}), patch.object(
            harvester, "refresh_known_threads", return_value={}
        ), patch.object(harvester, "paginate_messages", return_value=messages):
            summary = harvester.run_techsupport_pipeline(
                self.env,
                resolve_experts=lambda _product: ["U_EXPERT"],
                backend=self.backend,
            )

        self.backend.process_thread.assert_not_called()
        self.assertEqual(summary["product_channels"]["products"]["IDA"]["skipped_no_expert_reply"], 1)
        self.backend.finalize_run.assert_called_once_with(changed_this_run=False, run_maintenance=True)

    def test_product_thread_with_expert_reply_formats_and_calls_backend(self) -> None:
        self.backend.get_channel_state.side_effect = [
            {"due": False, "reason": "interval_not_elapsed"},
            {
                "due": True,
                "last_run_timestamp": "100.0",
                "known_thread_ids": [],
                "lookup_ids": [],
                "catchup_due": False,
                "scan_timestamp": "200.0",
            },
        ]
        self.backend.checkpoint_channel_discovery.return_value = {
            "new_thread_ids": ["150.0"],
            "updated_thread_ids": [],
            "candidate_thread_ids": ["150.0"],
            "deferred_thread_ids": [],
        }
        self.backend.process_thread.return_value = {"outcome": "corrected", "changed": True}
        self.backend.finalize_run.return_value = {"step_errors": []}

        messages = [
            {"ts": "150.0", "user": "U_USER", "text": "question"},
            {"ts": "151.0", "user": "U_BOT", "text": "bot answer"},
            {"ts": "152.0", "user": "U_EXPERT", "text": "use flag --foo"},
        ]
        with patch.object(harvester, "find_new_threads", return_value={"150.0": "152.0"}), patch.object(
            harvester, "refresh_known_threads", return_value={}
        ), patch.object(harvester, "paginate_messages", return_value=messages), patch.object(
            harvester, "_resolve_user_name", side_effect=lambda uid, _tok: f"Name({uid})"
        ):
            summary = harvester.run_techsupport_pipeline(
                self.env,
                resolve_experts=lambda _product: ["U_EXPERT"],
                backend=self.backend,
            )

        self.backend.process_thread.assert_called_once()
        args, kwargs = self.backend.process_thread.call_args
        self.assertEqual(args[0], "C_IDA")
        self.assertEqual(args[1], "150.0")
        self.assertIn("Lil Lisa (bot): bot answer", args[2])
        self.assertIn("Name(U_EXPERT) (expert): use flag --foo", args[2])
        self.assertEqual(kwargs["product"], "IDA")
        self.assertEqual(summary["product_channels"]["products"]["IDA"]["corrected"], 1)
        self.backend.finalize_run.assert_called_once_with(changed_this_run=True, run_maintenance=True)

    def test_expert_lookup_error_posts_product_channel_failure_alert(self) -> None:
        self.backend.get_channel_state.return_value = {"due": False, "reason": "interval_not_elapsed"}
        self.backend.finalize_run.return_value = {"step_errors": []}

        def fail_experts(_product: str):
            raise ExpertLookupError("IDA", "@ida-experts", RuntimeError("missing_scope"))

        with patch.object(harvester, "post_admin_alert") as alert_mock:
            summary = harvester.run_techsupport_pipeline(
                self.env,
                resolve_experts=fail_experts,
                backend=self.backend,
            )

        self.assertEqual(len(summary["errors"]), 1)
        alert_mock.assert_called_once()
        alert_text = alert_mock.call_args.args[2]
        self.assertIn("1 product channel(s) failed", alert_text)
        self.assertIn("channel C_IDA: [IDA]", alert_text)


if __name__ == "__main__":
    unittest.main()
