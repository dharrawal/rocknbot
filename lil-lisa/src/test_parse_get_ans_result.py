"""
Unit tests for parse_get_ans_result (timeout string must not crash Slack posting).

Run from lil-lisa/src:
    python3 test_parse_get_ans_result.py
"""

import json
import unittest

from utils import parse_get_ans_result


class ParseGetAnsResultTests(unittest.TestCase):
    def test_success_json_passthrough(self):
        payload = {
            "response": "Restart VDS.",
            "links_text": "<http://example|doc>",
            "reranked_nodes": [{"text": "chunk"}],
            "needs_escalation": False,
        }
        self.assertEqual(parse_get_ans_result(json.dumps(payload)), payload)

    def test_timeout_plain_string_becomes_response(self):
        raw = "The agent failed to generate an answer. Please try again in a new message thread."
        parsed = parse_get_ans_result(raw)
        self.assertEqual(parsed["response"], raw)
        self.assertEqual(parsed["links_text"], "")
        self.assertEqual(parsed["reranked_nodes"], [])
        self.assertFalse(parsed["needs_escalation"])

    def test_json_array_is_treated_as_plain_text(self):
        raw = json.dumps(["not", "an", "object"])
        parsed = parse_get_ans_result(raw)
        self.assertEqual(parsed["response"], raw)
        self.assertFalse(parsed["needs_escalation"])


if __name__ == "__main__":
    unittest.main()
