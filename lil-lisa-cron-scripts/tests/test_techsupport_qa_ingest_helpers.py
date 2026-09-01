"""
Unit tests for defensive techsupport review-state lookup, and a source-level
check that historical_import_production dry-run uses the public ingest API.

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_techsupport_qa_ingest_helpers.py
"""

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from techsupport_review_state import node_ids_from_review_entry, review_entry_state  # noqa: E402


class ReviewEntryStateTests(unittest.TestCase):
    def test_missing_entries_key(self):
        self.assertEqual(review_entry_state({}, 0), {})

    def test_missing_index(self):
        self.assertEqual(review_entry_state({"entries": {"1": {"node_ids": ["a"]}}}, 0), {})

    def test_non_dict_entry(self):
        self.assertEqual(review_entry_state({"entries": {"0": "corrupt"}}, 0), {})

    def test_non_dict_state(self):
        self.assertEqual(review_entry_state(None, 0), {})  # type: ignore[arg-type]

    def test_happy_path(self):
        state = {"entries": {"3": {"node_ids": ["n1"], "thread_ts": "1.2"}}}
        self.assertEqual(review_entry_state(state, 3)["node_ids"], ["n1"])

    def test_node_ids_defaults(self):
        self.assertEqual(node_ids_from_review_entry({}), [])
        self.assertEqual(node_ids_from_review_entry({"node_ids": None}), [])
        self.assertEqual(node_ids_from_review_entry({"node_ids": "not-a-list"}), [])
        self.assertEqual(node_ids_from_review_entry({"node_ids": ["x"]}), ["x"])


class HistoricalImportUsesPublicIngestApiTests(unittest.TestCase):
    def test_dry_run_imports_generate_verified_title_and_summary_not_predicts(self):
        source = (SCRIPTS_DIR / "historical_import_production.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_from_ingest: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "techsupport_qa_ingest":
                for alias in node.names:
                    imported_from_ingest.add(alias.name)
        self.assertIn("generate_verified_title_and_summary", imported_from_ingest)
        self.assertIn("add_verified_qa_pair", imported_from_ingest)
        self.assertNotIn("generate_title", imported_from_ingest)
        self.assertNotIn("summarize_conversation", imported_from_ingest)


if __name__ == "__main__":
    unittest.main()
