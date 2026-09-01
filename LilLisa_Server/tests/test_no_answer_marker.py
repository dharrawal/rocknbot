"""
Unit tests for leading [[NO_ANSWER]] detection.

Run from LilLisa_Server:
    PYTHONPATH=. python3 tests/test_no_answer_marker.py
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils import (  # noqa: E402
    NO_ANSWER_MARKER,
    build_no_answer_retry_log_record,
    parse_leading_no_answer_marker,
)


class ParseLeadingNoAnswerMarkerTests(unittest.TestCase):
    def test_marker_at_start_is_no_answer_and_stripped(self):
        answer_found, text = parse_leading_no_answer_marker(
            f"{NO_ANSWER_MARKER} Please rephrase with more detail."
        )
        self.assertFalse(answer_found)
        self.assertEqual(text, "Please rephrase with more detail.")

    def test_leading_whitespace_before_marker_still_counts(self):
        answer_found, text = parse_leading_no_answer_marker(
            f"\n  {NO_ANSWER_MARKER}\nI cannot find that."
        )
        self.assertFalse(answer_found)
        self.assertEqual(text, "I cannot find that.")

    def test_real_answer_unchanged(self):
        body = "Set the bind DN in the data source configuration."
        answer_found, text = parse_leading_no_answer_marker(body)
        self.assertTrue(answer_found)
        self.assertEqual(text, body)

    def test_mid_response_mention_is_not_no_answer(self):
        body = (
            "I would use "
            f"{NO_ANSWER_MARKER} "
            "if the docs did not cover this. Here is the actual fix: restart VDS."
        )
        answer_found, text = parse_leading_no_answer_marker(body)
        self.assertTrue(answer_found)
        self.assertEqual(text, body)

    def test_quoted_marker_inside_fenced_code_is_not_no_answer(self):
        body = (
            "The protocol is:\n"
            "```\n"
            f"{NO_ANSWER_MARKER}\n"
            "```\n"
            "For your case, enable the connector first."
        )
        answer_found, text = parse_leading_no_answer_marker(body)
        self.assertTrue(answer_found)
        self.assertEqual(text, body)

    def test_marker_only_is_no_answer_with_empty_body(self):
        answer_found, text = parse_leading_no_answer_marker(NO_ANSWER_MARKER)
        self.assertFalse(answer_found)
        self.assertEqual(text, "")

    def test_empty_response_is_answer_found(self):
        answer_found, text = parse_leading_no_answer_marker("")
        self.assertTrue(answer_found)
        self.assertEqual(text, "")


class NoAnswerRetryLogRecordTests(unittest.TestCase):
    def _record(self, **overrides):
        base = dict(
            product="IDDM",
            original_query="how do I bind?",
            generated_query="",
            top_rerank_score=4.2,
            threshold=3.0,
            top_chunk_text="Configure the bind DN on the data source.",
            top_chunk_metadata={"title": "bind-dn", "github_url": "https://example/bind"},
            first_response=f"{NO_ANSWER_MARKER} Please rephrase.",
            retry_response="Set the bind DN in the data source configuration.",
            first_answer_found=False,
            retry_answer_found=True,
        )
        base.update(overrides)
        return build_no_answer_retry_log_record(**base)

    def test_changed_outcome_when_retry_drops_marker(self):
        record = self._record()
        self.assertEqual(record["event"], "NO_ANSWER_RETRY")
        self.assertTrue(record["changed_outcome"])
        self.assertIn("original_query", record)
        self.assertIn("top_chunk_text", record)
        self.assertIn("first_response", record)
        self.assertIn("retry_response", record)
        self.assertIn("top_rerank_score", record)

    def test_unchanged_when_retry_still_no_answer(self):
        record = self._record(
            retry_response=f"{NO_ANSWER_MARKER} Still unknown.",
            retry_answer_found=False,
        )
        self.assertFalse(record["changed_outcome"])


if __name__ == "__main__":
    unittest.main()
