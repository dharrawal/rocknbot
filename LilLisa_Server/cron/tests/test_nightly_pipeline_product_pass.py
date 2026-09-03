"""
Unit tests for nightly_pipeline's product-channel expert-correction pass
(pr42-w1x.4): an expert replies in an IDA/IDDM/IDO thread to correct or
augment a bot answer, and the nightly run turns that into a correction of the
cited verified entry (or a new entry).

No Slack, no LLM, no LanceDB: Slack pagination, the classifier, the three
ingest writers, the cited-entry lookup and the expert-group resolver are all
patched. Only the state file is real, in a temp dir, so the per-thread
add/outcome bookkeeping is exercised end to end.

Run from LilLisa_Server/cron:
    PYTHONPATH=. python3 tests/test_nightly_pipeline_product_pass.py
"""

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# nightly_pipeline -> techsupport_qa_ingest -> src.utils, whose module-level
# LILLISA_SERVER_ENV_DICT["LOG_LEVEL"] lookup is the one hard env requirement in
# that chain. Needed here for the same reason as in
# tests/test_techsupport_correct_entry.py: under `unittest discover`,
# tests/test_github_sync.py installs a MagicMock stand-in for `dotenv`, so every
# dotenv_values() returns {} and only os.environ is left.
os.environ.setdefault("LOG_LEVEL", "INFO")

import nightly_pipeline as pipeline  # noqa: E402
import nightly_techsupport_sync as sync_mod  # noqa: E402

CHANNEL = "C_IDA"
THREAD = "100.0"
EXPERT_ID = "U_EXPERT"
BOT_ID = "U_BOT"

PARENT = {"ts": THREAD, "user": "U_USER", "text": "why is my agent timing out?"}
BOT_ANSWER = {"ts": "101.0", "user": BOT_ID, "bot_id": "B1", "text": "raise the heartbeat timeout"}
EXPERT_REPLY = {"ts": "102.0", "user": EXPERT_ID, "text": "that's wrong -- restart the agent service"}
USER_REPLY = {"ts": "103.0", "user": "U_OTHER", "text": "same here"}


def classification(useful: bool = True, conclusive: Optional[bool] = True, text: str = "thread text") -> Dict[str, Any]:
    return {"is_useful": useful, "is_conclusive": conclusive, "conversation_thread": text}


class ProductPassTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "techsupport_sync_state.json"

        self.channels = {"IDA": CHANNEL}
        self.expert_ids = [EXPERT_ID]
        # When set, the patched resolver raises this instead of returning ids.
        self.expert_error: Optional[Exception] = None
        self.scan_enabled = True
        self.max_threads = 50
        self.messages: Dict[str, List[Dict[str, Any]]] = {THREAD: [PARENT, BOT_ANSWER, EXPERT_REPLY]}
        self.sync_result = {"new_thread_ids": [THREAD], "updated_thread_ids": []}
        self.cited_titles: Dict[str, str] = {}
        # The expert-insight gate: default yes, so every pre-existing test keeps
        # exercising the routing it was written for.
        self.expert_insight = True
        self.classifications: List[Dict[str, Any]] = [classification()]
        self.correct_side_effects: List[Any] = []
        self.add_side_effects: List[Any] = []
        self.errors: List[Dict[str, str]] = []
        self.lookback_days = 30.0
        # (channel_id, last_run_timestamp) as each patched sync() call saw it.
        self.sync_seen_last_run: List[Any] = []
        self.mocks: Dict[str, Any] = {}

    # -------------------------------------------------------------- helpers

    def seed_state(self, threads: Dict[str, Any], channel_id: str = CHANNEL) -> None:
        payload = {
            "version": 2,
            "channels": {channel_id: {"last_run_timestamp": "0", "threads": threads}},
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def saved_state(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def thread_state(self, thread_ts: str = THREAD, channel_id: str = CHANNEL) -> Dict[str, Any]:
        return self.saved_state()["channels"][channel_id]["threads"].get(thread_ts, {})

    def _paginate(self, method, _token, params):
        self.assertEqual(method, "conversations.replies")
        return list(self.messages.get(params["ts"], []))

    def _format(self, messages, **_kwargs):
        return "\n".join(f"[{m['ts']}] {m.get('user', '?')}: {m.get('text', '')}" for m in messages)

    def _classify(self, _messages, **_kwargs):
        if len(self.classifications) > 1:
            return self.classifications.pop(0)
        return self.classifications[0]

    def _expert_user_ids(self, _product):
        if self.expert_error is not None:
            raise self.expert_error
        return list(self.expert_ids)

    def _pop_side_effect(self, queue):
        if not queue:
            return {}
        effect = queue.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def _correct(self, *_args, **_kwargs):
        return self._pop_side_effect(self.correct_side_effects)

    def _add(self, *_args, **_kwargs):
        return self._pop_side_effect(self.add_side_effects)

    def _sync(self, channel_id=None):
        # Record what sync() would have read, i.e. the value the seeding step
        # left behind, before it moves the timestamp forward itself.
        state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        per_channel = (state.get("channels") or {}).get(channel_id) or {}
        self.sync_seen_last_run.append((channel_id, per_channel.get("last_run_timestamp")))
        return dict(self.sync_result)

    def run_pass(self, force: bool = False) -> Dict[str, Any]:
        specs = [
            ("state_path", patch.object(sync_mod, "STATE_PATH", self.state_path)),
            ("sync", patch.object(pipeline, "sync", side_effect=self._sync)),
            (
                "load_env",
                patch.object(
                    pipeline,
                    "load_env",
                    return_value={
                        "SLACK_BOT_TOKEN": "t",
                        "TECHSUPPORT_CHANNEL_ID": "C_TECH",
                        "LIL_LISA_SLACK_USERID": BOT_ID,
                    },
                ),
            ),
            ("channels", patch.object(pipeline, "product_channel_ids", side_effect=lambda env=None: dict(self.channels))),
            ("enabled", patch.object(pipeline, "get_scan_product_channels", side_effect=lambda: self.scan_enabled)),
            ("cap", patch.object(pipeline, "get_product_scan_max_threads_per_run", side_effect=lambda: self.max_threads)),
            (
                "lookback",
                patch.object(
                    pipeline, "get_product_scan_initial_lookback_days", side_effect=lambda: self.lookback_days
                ),
            ),
            ("experts", patch.object(pipeline.expert_group, "expert_user_ids", side_effect=self._expert_user_ids)),
            ("cited", patch.object(pipeline, "get_cited_entry_title", side_effect=lambda ts: self.cited_titles.get(ts))),
            ("paginate", patch.object(pipeline, "paginate_messages", side_effect=self._paginate)),
            ("classify", patch.object(pipeline, "classify_thread", side_effect=self._classify)),
            (
                "insight",
                patch.object(pipeline, "has_expert_insight", side_effect=lambda _text: self.expert_insight),
            ),
            ("format", patch.object(pipeline, "format_thread_messages", side_effect=self._format)),
            ("correct", patch.object(pipeline, "correct_verified_entry", side_effect=self._correct)),
            ("add", patch.object(pipeline, "add_verified_qa_pair", side_effect=self._add)),
            ("replace", patch.object(pipeline, "replace_verified_qa_pair", MagicMock(return_value={}))),
        ]
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.mocks = {name: stack.enter_context(patcher) for name, patcher in specs}
        return pipeline.run_product_channel_pass("token", errors=self.errors, force=force)


class ExpertReplyGateTests(ProductPassTestBase):
    def test_no_expert_reply_never_reaches_the_classifier(self):
        self.messages[THREAD] = [PARENT, BOT_ANSWER, USER_REPLY]
        result = self.run_pass()
        self.mocks["classify"].assert_not_called()
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_reply"], 1)
        self.assertEqual(result["totals"]["skipped_no_expert_reply"], 1)
        self.assertEqual(self.thread_state(), {})

    def test_expert_as_thread_parent_only_is_not_a_correction(self):
        self.messages[THREAD] = [
            {"ts": THREAD, "user": EXPERT_ID, "text": "has anyone seen this?"},
            {"ts": "101.0", "user": "U_OTHER", "text": "nope"},
        ]
        result = self.run_pass()
        self.mocks["classify"].assert_not_called()
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_reply"], 1)

    def test_has_expert_reply_helper(self):
        self.assertTrue(pipeline.has_expert_reply([PARENT, EXPERT_REPLY], THREAD, [EXPERT_ID]))
        self.assertFalse(pipeline.has_expert_reply([PARENT, USER_REPLY], THREAD, [EXPERT_ID]))
        self.assertFalse(pipeline.has_expert_reply([PARENT, EXPERT_REPLY], THREAD, []))
        # A duplicate of the parent inside the replies list is still the parent.
        self.assertFalse(
            pipeline.has_expert_reply(
                [{"ts": THREAD, "user": EXPERT_ID}, {"ts": THREAD, "user": EXPERT_ID}], THREAD, [EXPERT_ID]
            )
        )


class ExpertInsightGateTests(ProductPassTestBase):
    """has_expert_reply() only proves an expert spoke. The insight gate decides
    whether what the expert said was a correction/confirmation/addition or just
    another question."""

    def test_expert_reply_without_insight_is_skipped(self):
        self.expert_insight = False
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_insight"], 1)
        self.assertEqual(result["totals"]["skipped_no_expert_insight"], 1)
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_reply"], 0)
        self.mocks["classify"].assert_not_called()
        self.mocks["correct"].assert_not_called()
        self.mocks["add"].assert_not_called()
        self.mocks["replace"].assert_not_called()
        # Nothing recorded, so new activity brings the thread back next run.
        self.assertEqual(self.thread_state(), {})

    def test_insight_gate_sees_the_role_tagged_thread(self):
        self.expert_insight = False
        self.run_pass()
        self.mocks["insight"].assert_called_once()
        (text,), _kwargs = self.mocks["insight"].call_args
        self.assertIn(EXPERT_ID, text)
        format_kwargs = self.mocks["format"].call_args[1]
        self.assertEqual(format_kwargs["bot_user_id"], BOT_ID)
        self.assertEqual(list(format_kwargs["expert_user_ids"]), [EXPERT_ID])

    def test_insight_yes_routes_exactly_as_before(self):
        self.cited_titles[THREAD] = "Agent Heartbeat Timeout"
        self.classifications = [classification(useful=True, conclusive=None)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["corrected"], 1)
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_insight"], 0)
        self.mocks["correct"].assert_called_once()
        self.assertTrue(self.thread_state()["added_to_verified_db"])

    def test_expert_question_thread_is_skipped(self):
        # Expert opens the thread, the bot answers, the expert follows up with
        # another question. has_expert_reply() passes (the follow-up is not the
        # parent) but there is no insight to ingest.
        self.messages[THREAD] = [
            {"ts": THREAD, "user": EXPERT_ID, "text": "why does the agent time out?"},
            {"ts": "101.0", "user": BOT_ID, "bot_id": "B1", "text": "raise the heartbeat timeout"},
            {"ts": "102.0", "user": EXPERT_ID, "text": "and where is that configured?"},
        ]
        self.expert_insight = False
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_insight"], 1)
        self.mocks["classify"].assert_not_called()
        self.assertEqual(self.thread_state(), {})

    def test_skipped_thread_is_not_marked_added(self):
        self.expert_insight = False
        self.run_pass()
        self.assertNotIn("skipped_no_expert_insight", pipeline.PRODUCT_CHANGE_KEYS)
        self.assertEqual(self.saved_state()["channels"][CHANNEL]["threads"], {})


class CorrectionRoutingTests(ProductPassTestBase):
    def test_cited_entry_is_corrected_with_provenance_and_role_tags(self):
        self.cited_titles[THREAD] = "Agent Heartbeat Timeout"
        self.classifications = [classification(useful=True, conclusive=None)]
        result = self.run_pass()

        self.assertEqual(result["products"]["IDA"]["corrected"], 1)
        self.assertEqual(result["totals"]["corrected"], 1)
        # Lighter bar: usefulness only, exactly like the enrich path.
        _args, classify_kwargs = self.mocks["classify"].call_args
        self.assertTrue(classify_kwargs["skip_conclusive"])
        self.assertEqual(classify_kwargs["bot_user_id"], BOT_ID)
        self.assertEqual(list(classify_kwargs["expert_user_ids"]), [EXPERT_ID])

        correct_args, correct_kwargs = self.mocks["correct"].call_args
        self.assertEqual(correct_args[0], "Agent Heartbeat Timeout")
        self.assertEqual(correct_args[1], "thread text")
        self.assertEqual(correct_kwargs["source_channel_id"], CHANNEL)
        self.assertEqual(correct_kwargs["source_thread_ts"], THREAD)
        self.mocks["add"].assert_not_called()

        saved = self.thread_state()
        self.assertTrue(saved["added_to_verified_db"])
        self.assertEqual(saved["outcome"], "corrected")

    def test_cited_entry_not_useful_is_skipped(self):
        self.cited_titles[THREAD] = "Agent Heartbeat Timeout"
        self.classifications = [classification(useful=False, conclusive=None)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_not_useful"], 1)
        self.mocks["correct"].assert_not_called()

    def test_missing_entry_falls_back_to_add_against_the_full_bar(self):
        self.cited_titles[THREAD] = "Gone Entry"
        self.correct_side_effects = [LookupError("no entry titled 'Gone Entry'")]
        self.classifications = [
            classification(useful=True, conclusive=None),  # light bar for the correction attempt
            classification(useful=True, conclusive=True),  # full bar for the add fallback
        ]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["added"], 1)
        self.assertEqual(self.mocks["classify"].call_count, 2)
        self.assertFalse(self.mocks["classify"].call_args_list[1].kwargs["skip_conclusive"])
        add_args, add_kwargs = self.mocks["add"].call_args
        self.assertEqual(add_args[0], "thread text")
        self.assertEqual(add_kwargs["thread_ts"], THREAD)
        self.assertEqual(self.thread_state()["outcome"], "added")

    def test_missing_entry_fallback_respects_conclusiveness(self):
        self.cited_titles[THREAD] = "Gone Entry"
        self.correct_side_effects = [LookupError("gone")]
        self.classifications = [
            classification(useful=True, conclusive=None),
            classification(useful=True, conclusive=False),
        ]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_not_conclusive"], 1)
        self.mocks["add"].assert_not_called()


class NoCitedEntryTests(ProductPassTestBase):
    def test_useful_and_conclusive_is_added(self):
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["added"], 1)
        _args, classify_kwargs = self.mocks["classify"].call_args
        self.assertFalse(classify_kwargs["skip_conclusive"])
        self.mocks["correct"].assert_not_called()
        self.assertEqual(self.thread_state()["outcome"], "added")

    def test_not_conclusive_is_skipped(self):
        self.classifications = [classification(useful=True, conclusive=False)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_not_conclusive"], 1)
        self.mocks["add"].assert_not_called()
        self.assertEqual(self.thread_state(), {})

    def test_not_useful_is_skipped(self):
        self.classifications = [classification(useful=False, conclusive=None)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_not_useful"], 1)
        self.mocks["add"].assert_not_called()


class AlreadyAddedTests(ProductPassTestBase):
    def test_previously_corrected_thread_is_corrected_again(self):
        self.seed_state({THREAD: {"added_to_verified_db": True, "outcome": "corrected"}})
        self.cited_titles[THREAD] = "Agent Heartbeat Timeout"
        self.sync_result = {"new_thread_ids": [], "updated_thread_ids": [THREAD]}
        self.classifications = [classification(useful=True, conclusive=None)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["corrected"], 1)
        self.mocks["replace"].assert_not_called()
        self.mocks["correct"].assert_called_once()
        self.assertEqual(self.thread_state()["outcome"], "corrected")

    def test_previously_added_thread_is_replaced(self):
        self.seed_state({THREAD: {"added_to_verified_db": True, "outcome": "added"}})
        self.sync_result = {"new_thread_ids": [], "updated_thread_ids": [THREAD]}
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["replaced"], 1)
        self.mocks["correct"].assert_not_called()
        replace_args, _kwargs = self.mocks["replace"].call_args
        self.assertEqual(replace_args[0], THREAD)
        self.assertEqual(replace_args[1], "thread text")
        # A replaced entry is still the "added" kind for next time.
        self.assertEqual(self.thread_state()["outcome"], "added")

    def test_already_added_but_no_longer_conclusive_is_left_alone(self):
        self.seed_state({THREAD: {"added_to_verified_db": True, "outcome": "added"}})
        self.sync_result = {"new_thread_ids": [], "updated_thread_ids": [THREAD]}
        self.classifications = [classification(useful=True, conclusive=False)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["left_as_is_not_conclusive"], 1)
        self.mocks["replace"].assert_not_called()

    def test_already_added_but_no_longer_useful_is_left_alone(self):
        self.seed_state({THREAD: {"added_to_verified_db": True, "outcome": "added"}})
        self.sync_result = {"new_thread_ids": [], "updated_thread_ids": [THREAD]}
        self.classifications = [classification(useful=False, conclusive=None)]
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["left_as_is_not_useful"], 1)
        self.mocks["replace"].assert_not_called()


class CapAndSkipTests(ProductPassTestBase):
    def test_cap_processes_hottest_first_and_defers_the_rest(self):
        other = "200.0"
        self.messages[other] = [
            {"ts": other, "user": "U_USER", "text": "q"},
            {"ts": "201.0", "user": EXPERT_ID, "text": "correction"},
        ]
        self.sync_result = {"new_thread_ids": [THREAD, other], "updated_thread_ids": []}
        self.max_threads = 1
        result = self.run_pass()

        self.assertEqual(result["products"]["IDA"]["checked"], 1)
        self.assertEqual(result["products"]["IDA"]["added"], 1)
        # 200.0 is the more recent thread, so it goes first and 100.0 waits.
        self.assertEqual(self.thread_state(other)["outcome"], "added")
        self.assertEqual(
            self.saved_state()["channels"][CHANNEL]["pending_thread_ids"],
            [THREAD],
        )

    def test_deferred_threads_are_picked_up_next_run(self):
        self.seed_state({})
        state = self.saved_state()
        state["channels"][CHANNEL]["pending_thread_ids"] = [THREAD]
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.sync_result = {"new_thread_ids": [], "updated_thread_ids": []}
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["added"], 1)
        self.assertEqual(self.saved_state()["channels"][CHANNEL]["pending_thread_ids"], [])

    def test_unconfigured_expert_group_is_an_error_not_a_skip(self):
        # A required EXPERT_GROUP_ID_* is missing: the resolver raises ValueError.
        self.expert_error = ValueError("Missing required Slack expert user group id(s): EXPERT_GROUP_ID_IDA")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["errored"], 1)
        self.assertEqual(result["totals"]["errored"], 1)
        self.assertNotIn("skipped_reason", result["products"]["IDA"])
        self.assertEqual(len(self.errors), 1)
        self.assertIn("EXPERT_GROUP_ID_IDA", self.errors[0]["error"])
        self.mocks["sync"].assert_not_called()
        self.mocks["paginate"].assert_not_called()

    def test_unresolvable_expert_group_is_an_error_not_a_skip(self):
        # The group is configured but Slack cannot be read and nothing is cached.
        self.expert_error = pipeline.expert_group.ExpertLookupError("IDA", "S_IDA", RuntimeError("missing_scope"))
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["errored"], 1)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("[IDA]", self.errors[0]["error"])
        self.assertIn("missing_scope", self.errors[0]["error"])
        self.mocks["sync"].assert_not_called()

    def test_empty_expert_group_scans_but_finds_no_expert_reply(self):
        # An empty (but readable) group is an honest answer, not a failure: the
        # channel is still scanned and every thread falls out at the cheap gate.
        self.expert_ids = []
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["errored"], 0)
        self.assertEqual(result["products"]["IDA"]["checked"], 1)
        self.assertEqual(result["products"]["IDA"]["skipped_no_expert_reply"], 1)
        self.mocks["classify"].assert_not_called()

    def test_interval_not_elapsed_skips_the_channel(self):
        import time as _time

        self.seed_state({})
        state = self.saved_state()
        state["channels"][CHANNEL]["last_run_timestamp"] = f"{_time.time():.6f}"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_reason"], "interval_not_elapsed")
        self.mocks["sync"].assert_not_called()

    def test_unconfigured_channel_is_not_scanned(self):
        self.channels = {}
        result = self.run_pass()
        self.assertEqual(result["products"], {})
        self.assertEqual(result["reason"], "no_product_channels")
        self.mocks["sync"].assert_not_called()

    def test_scan_disabled_skips_the_whole_pass(self):
        self.scan_enabled = False
        result = self.run_pass()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["products"], {})
        self.mocks["sync"].assert_not_called()
        self.mocks["paginate"].assert_not_called()

    def test_scan_disabled_by_env(self):
        with patch.dict(os.environ, {"TECHSUPPORT_SCAN_PRODUCT_CHANNELS": "false"}):
            self.assertFalse(pipeline.get_scan_product_channels())
            with (
                patch.object(pipeline, "sync") as mock_sync,
                patch.object(pipeline, "paginate_messages") as mock_paginate,
            ):
                result = pipeline.run_product_channel_pass("token")
        self.assertFalse(result["enabled"])
        mock_sync.assert_not_called()
        mock_paginate.assert_not_called()

    def test_scan_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TECHSUPPORT_SCAN_PRODUCT_CHANNELS", None)
            self.assertTrue(pipeline.get_scan_product_channels())


class ErrorIsolationAndSummaryTests(ProductPassTestBase):
    def test_one_bad_thread_does_not_stop_the_others(self):
        other = "200.0"
        self.messages[other] = [
            {"ts": other, "user": "U_USER", "text": "q"},
            {"ts": "201.0", "user": EXPERT_ID, "text": "correction"},
        ]
        self.sync_result = {"new_thread_ids": [THREAD, other], "updated_thread_ids": []}
        # 200.0 runs first (hottest) and blows up; 100.0 still gets added.
        self.add_side_effects = [RuntimeError("lancedb down"), {}]
        result = self.run_pass()

        counts = result["products"]["IDA"]
        self.assertEqual(counts["checked"], 2)
        self.assertEqual(counts["errored"], 1)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(self.errors, [{"thread_ts": other, "error": "[IDA] lancedb down"}])
        self.assertEqual(self.thread_state(THREAD)["outcome"], "added")
        self.assertEqual(self.thread_state(other), {})

    def test_summary_shape(self):
        self.channels = {"IDA": CHANNEL, "IDDM": "C_IDDM"}
        result = self.run_pass()
        self.assertTrue(result["enabled"])
        self.assertEqual(sorted(result["products"]), ["IDA", "IDDM"])
        for product, counts in result["products"].items():
            for key in pipeline.PRODUCT_COUNT_KEYS:
                self.assertIn(key, counts, f"{product} missing {key}")
        self.assertEqual(result["products"]["IDA"]["channel_id"], CHANNEL)
        for key in pipeline.PRODUCT_COUNT_KEYS:
            self.assertIn(key, result["totals"])
        # Both products saw the same single thread, so totals add up.
        self.assertEqual(result["totals"]["checked"], 2)
        self.assertEqual(result["totals"]["added"], 2)


class InitialLookbackTests(ProductPassTestBase):
    """pr42-w1x.7: a product channel nobody has synced yet must not be read
    from timestamp 0, which would hand its entire backlog to the classifier."""

    def channel_last_run(self, channel_id: str = CHANNEL) -> float:
        return float(self.saved_state()["channels"][channel_id]["last_run_timestamp"])

    def test_first_scan_seeds_the_window_before_sync_runs(self):
        now = time.time()
        self.run_pass()

        seeded = self.channel_last_run()
        expected = now - self.lookback_days * 86400
        self.assertAlmostEqual(seeded, expected, delta=60)
        # The seed has to be on disk before sync() reads it, or find_new_threads
        # still walks the whole channel history.
        self.assertEqual(len(self.sync_seen_last_run), 1)
        seen_channel, seen_ts = self.sync_seen_last_run[0]
        self.assertEqual(seen_channel, CHANNEL)
        self.assertAlmostEqual(float(seen_ts), expected, delta=60)

    def test_lookback_is_honoured_per_channel(self):
        self.channels = {"IDA": CHANNEL, "IDDM": "C_IDDM"}
        self.lookback_days = 7.0
        now = time.time()
        self.run_pass()
        for channel_id in (CHANNEL, "C_IDDM"):
            self.assertAlmostEqual(self.channel_last_run(channel_id), now - 7 * 86400, delta=60)

    def test_channel_with_existing_state_is_not_reseeded(self):
        previous = time.time() - 40 * 86400
        self.seed_state({})
        state = self.saved_state()
        state["channels"][CHANNEL]["last_run_timestamp"] = f"{previous:.6f}"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        self.run_pass()

        _seen_channel, seen_ts = self.sync_seen_last_run[0]
        self.assertAlmostEqual(float(seen_ts), previous, delta=1)
        self.assertAlmostEqual(self.channel_last_run(), previous, delta=1)

    def test_seed_helper_reports_whether_it_wrote(self):
        state = sync_mod.new_state()
        per_channel = sync_mod.channel_state(state, CHANNEL)
        with patch.object(sync_mod, "STATE_PATH", self.state_path):
            self.assertTrue(pipeline.seed_initial_scan_window(state, per_channel, CHANNEL, "IDA"))
            # Second call: the channel now has a real timestamp, so it is left alone.
            self.assertFalse(pipeline.seed_initial_scan_window(state, per_channel, CHANNEL, "IDA"))

    def test_knob_default_and_env_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS", None)
            self.assertEqual(pipeline.get_product_scan_initial_lookback_days(), 30.0)
        with patch.dict(os.environ, {"PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS": "7"}):
            self.assertEqual(pipeline.get_product_scan_initial_lookback_days(), 7.0)
        with patch.dict(os.environ, {"PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS": "not-a-number"}):
            self.assertEqual(pipeline.get_product_scan_initial_lookback_days(), 30.0)


class ForceTests(ProductPassTestBase):
    """pr42-w1x.8: an operator forcing a scan means "now", interval or not."""

    def _freeze_interval(self) -> None:
        self.seed_state({})
        state = self.saved_state()
        state["channels"][CHANNEL]["last_run_timestamp"] = f"{time.time():.6f}"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_force_bypasses_the_interval_gate(self):
        self._freeze_interval()
        result = self.run_pass(force=True)
        self.assertNotIn("skipped_reason", result["products"]["IDA"])
        self.assertEqual(result["products"]["IDA"]["added"], 1)
        self.mocks["sync"].assert_called_once()

    def test_without_force_the_gate_still_applies(self):
        self._freeze_interval()
        result = self.run_pass()
        self.assertEqual(result["products"]["IDA"]["skipped_reason"], "interval_not_elapsed")
        self.mocks["sync"].assert_not_called()

    def test_force_does_not_bypass_the_disabled_switch(self):
        self.scan_enabled = False
        result = self.run_pass(force=True)
        self.assertFalse(result["enabled"])
        self.mocks["sync"].assert_not_called()


class RunProductChannelScanTests(ProductPassTestBase):
    """pr42-w1x.8: the standalone entry point behind POST /run_product_channel_scan/."""

    def run_scan(self, force: bool = False, changed: bool = True) -> Dict[str, Any]:
        if not changed:
            # Nothing an expert said, so nothing reaches the markdown file.
            self.messages[THREAD] = [PARENT, BOT_ANSWER, USER_REPLY]
        specs = [
            ("state_path", patch.object(sync_mod, "STATE_PATH", self.state_path)),
            ("sync", patch.object(pipeline, "sync", side_effect=self._sync)),
            (
                "load_env",
                patch.object(
                    pipeline,
                    "load_env",
                    return_value={
                        "SLACK_BOT_TOKEN": "t",
                        "TECHSUPPORT_CHANNEL_ID": "C_TECH",
                        "ADMIN_CHANNEL_ID": "C_ADMIN",
                        "LIL_LISA_SLACK_USERID": BOT_ID,
                    },
                ),
            ),
            ("channels", patch.object(pipeline, "product_channel_ids", side_effect=lambda env=None: dict(self.channels))),
            ("enabled", patch.object(pipeline, "get_scan_product_channels", side_effect=lambda: self.scan_enabled)),
            ("cap", patch.object(pipeline, "get_product_scan_max_threads_per_run", side_effect=lambda: self.max_threads)),
            (
                "lookback",
                patch.object(
                    pipeline, "get_product_scan_initial_lookback_days", side_effect=lambda: self.lookback_days
                ),
            ),
            ("experts", patch.object(pipeline.expert_group, "expert_user_ids", side_effect=self._expert_user_ids)),
            ("cited", patch.object(pipeline, "get_cited_entry_title", side_effect=lambda ts: self.cited_titles.get(ts))),
            ("paginate", patch.object(pipeline, "paginate_messages", side_effect=self._paginate)),
            ("classify", patch.object(pipeline, "classify_thread", side_effect=self._classify)),
            (
                "insight",
                patch.object(pipeline, "has_expert_insight", side_effect=lambda _text: self.expert_insight),
            ),
            ("format", patch.object(pipeline, "format_thread_messages", side_effect=self._format)),
            ("correct", patch.object(pipeline, "correct_verified_entry", side_effect=self._correct)),
            ("add", patch.object(pipeline, "add_verified_qa_pair", side_effect=self._add)),
            ("replace", patch.object(pipeline, "replace_verified_qa_pair", MagicMock(return_value={}))),
            # Everything the scan does after the pass.
            ("github", patch.object(pipeline, "push_verified_qa_pairs", MagicMock(return_value={"pushed": True}))),
            ("reload", patch.object(pipeline, "reload_techsupport_index", MagicMock(return_value={"reloaded": True}))),
            ("alert", patch.object(pipeline, "post_admin_alert", MagicMock())),
            # Steps the nightly run does that this one must not.
            ("techsupport_thread", patch.object(pipeline, "process_thread", MagicMock())),
            ("review_sync", patch.object(pipeline, "sync_edited_entries", MagicMock())),
            ("reembed", patch.object(pipeline, "run_reembed_if_due", MagicMock())),
        ]
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.mocks = {name: stack.enter_context(patcher) for name, patcher in specs}
        return pipeline.run_product_channel_scan(force=force)

    def test_runs_the_pass_and_publishes_what_changed(self):
        summary = self.run_scan()
        self.assertEqual(summary["product_channels"]["totals"]["added"], 1)
        self.assertEqual(summary["github_push"], {"pushed": True})
        self.assertEqual(summary["techsupport_index_reload_after_ingest"], {"reloaded": True})
        self.assertEqual(summary["errors"], [])
        self.mocks["github"].assert_called_once()
        self.mocks["reload"].assert_called_once()

    def test_does_not_run_the_techsupport_loop_review_sync_or_reembed(self):
        self.run_scan()
        self.mocks["techsupport_thread"].assert_not_called()
        self.mocks["review_sync"].assert_not_called()
        self.mocks["reembed"].assert_not_called()
        # Only the product channel was synced; the techsupport channel was not.
        self.assertEqual([channel for channel, _ts in self.sync_seen_last_run], [CHANNEL])

    def test_nothing_changed_means_no_push_and_no_reload(self):
        summary = self.run_scan(changed=False)
        self.assertEqual(summary["product_channels"]["totals"]["skipped_no_expert_reply"], 1)
        self.mocks["github"].assert_not_called()
        self.mocks["reload"].assert_not_called()
        self.assertEqual(summary["github_push"]["reason"], "no_changes_this_run")
        self.assertEqual(summary["techsupport_index_reload_after_ingest"]["reason"], "no_changes_this_run")

    def test_force_reaches_the_pass(self):
        self.seed_state({})
        state = self.saved_state()
        state["channels"][CHANNEL]["last_run_timestamp"] = f"{time.time():.6f}"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        summary = self.run_scan(force=True)
        self.assertEqual(summary["product_channels"]["totals"]["added"], 1)
        self.mocks["sync"].assert_called_once()

    def test_per_thread_errors_are_alerted(self):
        self.add_side_effects = [RuntimeError("lancedb down")]
        summary = self.run_scan()
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("lancedb down", summary["errors"][0]["error"])
        alert_text = self.mocks["alert"].call_args[0][2]
        self.assertIn("product-channel scan", alert_text)
        self.assertIn("lancedb down", alert_text)


if __name__ == "__main__":
    unittest.main()
