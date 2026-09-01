"""
Unit tests for nightly techsupport thread-update detection (hot window,
catch-up age cap, parent latest_reply lookups).

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_nightly_techsupport_sync.py
"""

import sys
import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import nightly_techsupport_sync as sync_mod  # noqa: E402


class SelectParentLookupsTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000.0
        day = sync_mod.SECONDS_PER_DAY
        self.threads = {
            "hot": {"last_seen_reply_ts": str(self.now - 5 * day)},
            "warm": {"last_seen_reply_ts": str(self.now - 45 * day)},
            "cold": {"last_seen_reply_ts": str(self.now - 120 * day)},
            "hottest": {"last_seen_reply_ts": str(self.now - 1 * day)},
        }
        self.ids = ["hot", "warm", "cold", "hottest"]

    def test_hot_window_excludes_warm_and_cold(self):
        chosen, capped = sync_mod.select_parent_lookups(
            self.ids, self.threads, self.now, window_days=30, max_lookups=200
        )
        self.assertEqual(chosen, ["hottest", "hot"])
        self.assertEqual(capped, 0)

    def test_catchup_age_cap_includes_warm_excludes_cold(self):
        chosen, capped = sync_mod.select_parent_lookups(
            self.ids, self.threads, self.now, window_days=90, max_lookups=200
        )
        self.assertEqual(chosen, ["hottest", "hot", "warm"])
        self.assertEqual(capped, 0)
        self.assertNotIn("cold", chosen)

    def test_cap_keeps_hottest_first(self):
        chosen, capped = sync_mod.select_parent_lookups(
            self.ids, self.threads, self.now, window_days=90, max_lookups=2
        )
        self.assertEqual(chosen, ["hottest", "hot"])
        self.assertEqual(capped, 1)

    def test_exclude_skips_already_checked_hot_ids(self):
        chosen, capped = sync_mod.select_parent_lookups(
            self.ids,
            self.threads,
            self.now,
            window_days=90,
            max_lookups=200,
            exclude=["hottest", "hot"],
        )
        self.assertEqual(chosen, ["warm"])
        self.assertEqual(capped, 0)

    def test_missing_last_seen_uses_parent_ts(self):
        parent_ts = str(self.now - 10 * sync_mod.SECONDS_PER_DAY)
        chosen, _ = sync_mod.select_parent_lookups(
            [parent_ts], {}, self.now, window_days=30, max_lookups=10
        )
        self.assertEqual(chosen, [parent_ts])


class CatchupDueTests(unittest.TestCase):
    def test_missing_timestamp_is_due(self):
        self.assertTrue(sync_mod.is_catchup_due({}, now=100.0, interval_days=7))

    def test_recent_catchup_is_not_due(self):
        now = 1_000_000.0
        state = {"last_catchup_timestamp": str(now - 2 * sync_mod.SECONDS_PER_DAY)}
        self.assertFalse(sync_mod.is_catchup_due(state, now=now, interval_days=7))

    def test_stale_catchup_is_due(self):
        now = 1_000_000.0
        state = {"last_catchup_timestamp": str(now - 8 * sync_mod.SECONDS_PER_DAY)}
        self.assertTrue(sync_mod.is_catchup_due(state, now=now, interval_days=7))


class FetchParentLatestReplyTsTests(unittest.TestCase):
    def test_uses_history_not_replies_and_reads_latest_reply(self):
        with patch.object(sync_mod, "slack_api_call") as mock_call:
            mock_call.return_value = {
                "ok": True,
                "messages": [{"ts": "100.0", "latest_reply": "150.0"}],
            }
            newest = sync_mod.fetch_parent_latest_reply_ts("token", "C1", "100.0")
        self.assertEqual(newest, "150.0")
        mock_call.assert_called_once()
        args, _kwargs = mock_call.call_args
        self.assertEqual(args[0], "conversations.history")
        self.assertEqual(args[2]["oldest"], "100.0")
        self.assertEqual(args[2]["limit"], 1)
        self.assertEqual(args[2]["inclusive"], "true")

    def test_falls_back_to_parent_ts_when_no_replies(self):
        with patch.object(sync_mod, "slack_api_call") as mock_call:
            mock_call.return_value = {"ok": True, "messages": [{"ts": "100.0"}]}
            newest = sync_mod.fetch_parent_latest_reply_ts("token", "C1", "100.0")
        self.assertEqual(newest, "100.0")


class SyncHybridTests(unittest.TestCase):
    def test_nightly_only_looks_up_hot_threads_and_skips_replies(self):
        now = 2_000_000_000.0
        day = sync_mod.SECONDS_PER_DAY
        state = {
            "last_run_timestamp": str(now - day),
            "last_catchup_timestamp": str(now - day),
            "threads": {
                "hot": {"last_seen_reply_ts": str(now - 2 * day), "added_to_verified_db": True},
                "warm": {"last_seen_reply_ts": str(now - 45 * day)},
                "cold": {"last_seen_reply_ts": str(now - 120 * day)},
            },
        }
        parent_calls: List[str] = []

        def fake_fetch(_token, _channel, thread_ts):
            parent_calls.append(thread_ts)
            if thread_ts == "hot":
                return str(now - 1)
            return state["threads"][thread_ts]["last_seen_reply_ts"]

        with (
            patch.object(sync_mod, "load_env", return_value={"SLACK_BOT_TOKEN": "t", "TECHSUPPORT_CHANNEL_ID": "C"}),
            patch.object(sync_mod, "load_state", return_value=state),
            patch.object(sync_mod, "save_state") as mock_save,
            patch.object(sync_mod, "find_new_threads", return_value=[]),
            patch.object(sync_mod, "fetch_parent_latest_reply_ts", side_effect=fake_fetch),
            patch.object(sync_mod, "paginate_messages") as mock_pages,
            patch.object(sync_mod, "get_hot_days", return_value=30.0),
            patch.object(sync_mod, "get_catchup_age_days", return_value=90.0),
            patch.object(sync_mod, "get_catchup_interval_days", return_value=7.0),
            patch.object(sync_mod, "get_max_parent_lookups", return_value=200),
            patch.object(sync_mod.time, "time", return_value=now),
            patch.object(sync_mod.time, "sleep"),
        ):
            result = sync_mod.sync()

        self.assertEqual(parent_calls, ["hot"])
        self.assertEqual(result["updated_thread_ids"], ["hot"])
        self.assertEqual(result["new_thread_ids"], [])
        mock_pages.assert_not_called()
        saved = mock_save.call_args[0][0]
        self.assertTrue(saved["threads"]["hot"]["added_to_verified_db"])
        self.assertEqual(saved["threads"]["cold"]["last_seen_reply_ts"], str(now - 120 * day))
        self.assertEqual(saved["last_catchup_timestamp"], str(now - day))

    def test_catchup_looks_up_warm_but_not_cold(self):
        now = 2_000_000_000.0
        day = sync_mod.SECONDS_PER_DAY
        state = {
            "last_run_timestamp": str(now - day),
            "threads": {
                "hot": {"last_seen_reply_ts": str(now - 2 * day)},
                "warm": {"last_seen_reply_ts": str(now - 45 * day)},
                "cold": {"last_seen_reply_ts": str(now - 120 * day)},
            },
        }
        parent_calls: List[str] = []

        def fake_fetch(_token, _channel, thread_ts):
            parent_calls.append(thread_ts)
            if thread_ts == "warm":
                return str(now - 40 * day)
            return state["threads"][thread_ts]["last_seen_reply_ts"]

        with (
            patch.object(sync_mod, "load_env", return_value={"SLACK_BOT_TOKEN": "t", "TECHSUPPORT_CHANNEL_ID": "C"}),
            patch.object(sync_mod, "load_state", return_value=state),
            patch.object(sync_mod, "save_state") as mock_save,
            patch.object(sync_mod, "find_new_threads", return_value=[]),
            patch.object(sync_mod, "fetch_parent_latest_reply_ts", side_effect=fake_fetch),
            patch.object(sync_mod, "get_hot_days", return_value=30.0),
            patch.object(sync_mod, "get_catchup_age_days", return_value=90.0),
            patch.object(sync_mod, "get_catchup_interval_days", return_value=7.0),
            patch.object(sync_mod, "get_max_parent_lookups", return_value=200),
            patch.object(sync_mod.time, "time", return_value=now),
            patch.object(sync_mod.time, "sleep"),
        ):
            result = sync_mod.sync()

        self.assertEqual(set(parent_calls), {"hot", "warm"})
        self.assertNotIn("cold", parent_calls)
        self.assertEqual(result["updated_thread_ids"], ["warm"])
        saved = mock_save.call_args[0][0]
        self.assertEqual(saved["last_catchup_timestamp"], f"{now:.6f}")
        self.assertEqual(saved["threads"]["cold"]["last_seen_reply_ts"], str(now - 120 * day))


class PipelineChannelIdAssertTests(unittest.TestCase):
    def test_matching_optional_ids_ok(self):
        sync_mod.assert_pipeline_matches_product_channel_ids(
            {
                "TECHSUPPORT_CHANNEL_ID": "Cshared",
                "TECHSUPPORT_CHANNEL_ID_IDA": "Cshared",
                "TECHSUPPORT_CHANNEL_ID_IDDM": "Cshared",
            }
        )

    def test_mismatch_raises(self):
        with self.assertRaises(RuntimeError):
            sync_mod.assert_pipeline_matches_product_channel_ids(
                {
                    "TECHSUPPORT_CHANNEL_ID": "Cshared",
                    "TECHSUPPORT_CHANNEL_ID_IDA": "Cother",
                }
            )


if __name__ == "__main__":
    unittest.main()
