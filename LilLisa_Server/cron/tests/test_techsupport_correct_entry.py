"""
Unit tests for the expert-correction supersede path (pr42-w1x.3):
techsupport_qa_ingest.correct_verified_entry() and the review-state
provenance helpers it writes through.

No LLM, no LanceDB, no network -- the DSPy predictor, the embedding/LM
configuration, the LanceDB insert/connect/vector-store and the GitHub-anchor
lookup are all patched, and markdown / review state live in a temp dir.

Run from LilLisa_Server/cron:
    PYTHONPATH=. python3 tests/test_techsupport_correct_entry.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# techsupport_qa_ingest -> src.llama_index_lancedb_vector_store -> src.utils, whose
# module-level LILLISA_SERVER_ENV_DICT["LOG_LEVEL"] lookup is the one hard env
# requirement in that chain. os.environ overrides the env file there, so setting it
# keeps this import working both without env/lillisa_server.env and under
# `unittest discover`, where tests/test_github_sync.py loads first and installs a
# MagicMock stand-in for `dotenv` (making every dotenv_values() return {}).
os.environ.setdefault("LOG_LEVEL", "INFO")

import techsupport_qa_ingest as ingest  # noqa: E402
from techsupport_markdown import parse_summary_markdown  # noqa: E402
from techsupport_review_state import (  # noqa: E402
    corrections_from_review_entry,
    enrichments_from_review_entry,
    with_provenance,
)

EXISTING_MARKDOWN = (
    "## Zookeeper GC Logging Configuration\n"
    "\n"
    "Enable GC logging by editing zoo.cfg and restarting the ensemble.\n"
    "\n"
    "## Agent Heartbeat Timeout\n"
    "\n"
    "The wrong original answer: raise the heartbeat timeout to 600 seconds.\n"
    "\n"
)

CORRECTED_TEXT = "The heartbeat timeout is not configurable; restart the agent service instead."

CORRECTION_THREAD = (
    "[1.0] User: why is my agent timing out?\n"
    "[2.0] Lil Lisa (bot): raise the heartbeat timeout to 600 seconds\n"
    "[3.0] Pat (expert): that's wrong, the timeout isn't configurable -- restart the agent service"
)


class CorrectVerifiedEntryTestCase(unittest.TestCase):
    """Shared temp markdown/review-state fixture plus the patch stack that keeps
    correct_verified_entry() off the LLM, LanceDB and the network."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        folder = Path(self.tmpdir.name)
        self.markdown_path = folder / ingest.TECHSUPPORT_QA_MARKDOWN_FILENAME
        self.markdown_path.write_text(EXISTING_MARKDOWN, encoding="utf-8")
        self.review_state_path = folder / "techsupport_review_state.json"

        self._enter(patch.object(ingest, "VERIFIED_TECHSUPPORT_QA_FOLDERPATH", folder))
        self._enter(patch.object(ingest, "REVIEW_STATE_PATH", self.review_state_path))
        self._enter(patch.object(ingest, "configure_dspy_lm"))
        self._enter(patch.object(ingest, "configure_embedding_model"))
        self._enter(patch.object(ingest, "load_pipeline_env", return_value={"LANCEDB_FOLDERPATH": str(folder)}))
        self._enter(patch.object(ingest, "lancedb", MagicMock()))
        self._enter(patch.object(ingest, "LanceDBVectorStore"))
        self._enter(patch.object(ingest, "_compute_github_url_for_entry", return_value="https://example/#anchor"))

        prediction = MagicMock()
        prediction.corrected_summary = CORRECTED_TEXT
        self.correct_summary = self._enter(patch.object(ingest, "correct_summary", return_value=prediction))
        self.insert = self._enter(
            patch.object(
                ingest,
                "insert_summary_into_lancedb",
                return_value={"table_name": "T", "row_count": 2, "node_ids": ["new-1"]},
            )
        )
        self.vector_store = ingest.LanceDBVectorStore.return_value

    def _enter(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def write_review_state(self, entries):
        self.review_state_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")

    def read_review_state(self):
        return json.loads(self.review_state_path.read_text(encoding="utf-8"))["entries"]

    def markdown_entries(self):
        return parse_summary_markdown(self.markdown_path.read_text(encoding="utf-8"))

    def correct(self, title="Agent Heartbeat Timeout"):
        return ingest.correct_verified_entry(
            title,
            CORRECTION_THREAD,
            source_channel_id="C_PRODUCT",
            source_thread_ts="1700000000.000100",
        )


class CorrectVerifiedEntryTests(CorrectVerifiedEntryTestCase):
    def setUp(self):
        super().setUp()
        self.write_review_state({"1": {"node_ids": ["old-1", "old-2"], "thread_ts": "1600000000.000200"}})

    def test_title_unchanged_and_summary_replaced(self):
        result = self.correct()

        entries = self.markdown_entries()
        self.assertEqual([entry["title"] for entry in entries], ["Zookeeper GC Logging Configuration", "Agent Heartbeat Timeout"])
        self.assertEqual(entries[1]["summary"], CORRECTED_TEXT)
        self.assertNotIn("600 seconds", entries[1]["summary"])
        # Unrelated entries are untouched.
        self.assertIn("Enable GC logging", entries[0]["summary"])
        self.assertEqual(result["title"], "Agent Heartbeat Timeout")
        self.assertEqual(result["summary"], CORRECTED_TEXT)

    def test_correction_prompt_receives_existing_summary_and_thread(self):
        self.correct()
        kwargs = self.correct_summary.call_args.kwargs
        self.assertIn("600 seconds", kwargs["existing_summary"])
        self.assertEqual(kwargs["correction_thread"], CORRECTION_THREAD)

    def test_old_node_ids_deleted_and_new_ones_saved(self):
        self.correct()
        self.vector_store.delete_nodes.assert_called_once_with(["old-1", "old-2"])
        self.assertEqual(self.read_review_state()["1"]["node_ids"], ["new-1"])

    def test_reembeds_with_unchanged_title(self):
        self.correct()
        args, kwargs = self.insert.call_args
        self.assertEqual(args[0], "Agent Heartbeat Timeout")
        self.assertEqual(args[1], CORRECTED_TEXT)
        self.assertEqual(kwargs["github_url"], "https://example/#anchor")

    def test_corrections_provenance_appended(self):
        self.correct()
        entry = self.read_review_state()["1"]
        # The entry's ORIGINAL thread_ts survives -- the correction happened in a
        # different (product) channel thread.
        self.assertEqual(entry["thread_ts"], "1600000000.000200")
        corrections = entry["corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["source_channel_id"], "C_PRODUCT")
        self.assertEqual(corrections[0]["source_thread_ts"], "1700000000.000100")
        # ISO-8601 UTC.
        parsed = datetime.fromisoformat(corrections[0]["superseded_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_repeated_corrections_accumulate(self):
        self.correct()
        self.correct()
        self.assertEqual(len(self.read_review_state()["1"]["corrections"]), 2)

    def test_unknown_title_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            self.correct(title="No Such Entry")
        self.assertEqual(self.markdown_path.read_text(encoding="utf-8"), EXISTING_MARKDOWN)

    def test_missing_markdown_file_raises_lookup_error(self):
        self.markdown_path.unlink()
        with self.assertRaises(LookupError):
            self.correct()

    def test_untracked_entry_skips_delete(self):
        self.write_review_state({})
        self.correct()
        self.vector_store.delete_nodes.assert_not_called()
        self.assertEqual(self.read_review_state()["1"]["node_ids"], ["new-1"])


class ProvenancePreservationTests(CorrectVerifiedEntryTestCase):
    def test_correction_preserves_existing_enrichments(self):
        self.write_review_state(
            {
                "1": {
                    "node_ids": ["old-1"],
                    "thread_ts": "1600000000.000200",
                    "enrichments": [{"source_channel_id": "C_TS", "source_thread_ts": "1.1", "enriched_at": "z"}],
                }
            }
        )
        self.correct()
        entry = self.read_review_state()["1"]
        self.assertEqual(len(entry["enrichments"]), 1)
        self.assertEqual(entry["enrichments"][0]["source_channel_id"], "C_TS")
        self.assertEqual(len(entry["corrections"]), 1)

    def test_enrichment_preserves_existing_corrections(self):
        self.write_review_state(
            {
                "1": {
                    "node_ids": ["old-1"],
                    "thread_ts": "1600000000.000200",
                    "corrections": [{"source_channel_id": "C_PROD", "source_thread_ts": "2.2", "superseded_at": "z"}],
                }
            }
        )
        merged = MagicMock()
        merged.merged_summary = "merged text"
        summarized = MagicMock()
        summarized.summary = "new insight"
        with patch.object(ingest, "merge_techsupport_summaries", return_value=merged), patch.object(
            ingest, "summarize_conversation", return_value=summarized
        ):
            ingest.enrich_verified_entry(
                "Agent Heartbeat Timeout",
                CORRECTION_THREAD,
                source_channel_id="C_TS",
                source_thread_ts="3.3",
            )
        entry = self.read_review_state()["1"]
        self.assertEqual(len(entry["corrections"]), 1)
        self.assertEqual(entry["corrections"][0]["source_channel_id"], "C_PROD")
        self.assertEqual(len(entry["enrichments"]), 1)
        self.assertEqual(entry["enrichments"][0]["source_channel_id"], "C_TS")
        self.assertIn("enriched_at", entry["enrichments"][0])

    def test_enrichment_without_provenance_args_keeps_original_shape(self):
        """nightly_pipeline.py's existing 2-positional-arg call must not start
        writing an "enrichments" key."""
        self.write_review_state({"1": {"node_ids": ["old-1"], "thread_ts": "1600000000.000200"}})
        merged = MagicMock()
        merged.merged_summary = "merged text"
        summarized = MagicMock()
        summarized.summary = "new insight"
        with patch.object(ingest, "merge_techsupport_summaries", return_value=merged), patch.object(
            ingest, "summarize_conversation", return_value=summarized
        ):
            ingest.enrich_verified_entry("Agent Heartbeat Timeout", CORRECTION_THREAD)
        self.assertEqual(
            self.read_review_state()["1"], {"node_ids": ["new-1"], "thread_ts": "1600000000.000200"}
        )


class ProvenanceAccessorTests(unittest.TestCase):
    def test_missing_and_invalid_inputs_return_empty_list(self):
        for accessor in (corrections_from_review_entry, enrichments_from_review_entry):
            self.assertEqual(accessor({}), [])
            self.assertEqual(accessor(None), [])  # type: ignore[arg-type]
            self.assertEqual(accessor("corrupt"), [])  # type: ignore[arg-type]
            self.assertEqual(accessor({"corrections": None, "enrichments": None}), [])
            self.assertEqual(accessor({"corrections": "x", "enrichments": "x"}), [])

    def test_happy_path(self):
        self.assertEqual(corrections_from_review_entry({"corrections": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(enrichments_from_review_entry({"enrichments": [{"b": 2}]}), [{"b": 2}])

    def test_with_provenance_omits_empty_lists(self):
        self.assertEqual(with_provenance({}, ["n"], "1.0"), {"node_ids": ["n"], "thread_ts": "1.0"})

    def test_with_provenance_appends_and_preserves(self):
        entry = with_provenance(
            {"corrections": [{"old": True}]},
            ["n"],
            "1.0",
            correction={"new": True},
            enrichment={"e": True},
        )
        self.assertEqual(entry["corrections"], [{"old": True}, {"new": True}])
        self.assertEqual(entry["enrichments"], [{"e": True}])

    def test_with_provenance_does_not_mutate_input(self):
        original = {"corrections": [{"old": True}]}
        with_provenance(original, ["n"], "1.0", correction={"new": True})
        self.assertEqual(original["corrections"], [{"old": True}])


class CorrectionPromptRoleTagMentionTests(unittest.TestCase):
    def test_corrected_summary_desc_mentions_role_tags(self):
        desc = ingest.CorrectTechsupportSummary.model_fields["corrected_summary"].json_schema_extra["desc"]
        self.assertIn("(bot)", desc)
        self.assertIn("(expert)", desc)


if __name__ == "__main__":
    unittest.main()
