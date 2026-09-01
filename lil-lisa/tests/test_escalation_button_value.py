"""
Unit tests for Slack escalate button JSON (2000-char cap) and shared channel IDs.

Run from lil-lisa:
    PYTHONPATH=src python3 tests/test_escalation_button_value.py
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils import (  # noqa: E402
    SLACK_ACTION_VALUE_MAX,
    assert_shared_techsupport_channel_ids,
    build_escalation_button_value,
)


class EscalationButtonValueTests(unittest.TestCase):
    def test_short_title_round_trips(self):
        blob = build_escalation_button_value(
            "how do I bind?",
            "C111",
            "123.4",
            "sess",
            "Uasker",
            primary_techsupport_match_title="LDAP bind timeout",
        )
        payload = json.loads(blob)
        self.assertEqual(payload["primary_techsupport_match_title"], "LDAP bind timeout")
        self.assertLessEqual(len(blob), SLACK_ACTION_VALUE_MAX)

    def test_long_title_is_truncated_to_fit(self):
        title = "T" * 2000
        blob = build_escalation_button_value(
            "q" * 1500,
            "C111",
            "123.4",
            "sess",
            "Uasker",
            primary_techsupport_match_title=title,
        )
        self.assertLessEqual(len(blob), SLACK_ACTION_VALUE_MAX)
        payload = json.loads(blob)
        self.assertIn("query", payload)
        if "primary_techsupport_match_title" in payload:
            self.assertLess(len(payload["primary_techsupport_match_title"]), 2000)


class SharedTechsupportChannelTests(unittest.TestCase):
    def test_matching_ids_ok(self):
        assert_shared_techsupport_channel_ids(
            {"IDA": "Csame", "IDDM": "Csame", "IDO": "Csame"}
        )

    def test_unset_ido_ok(self):
        assert_shared_techsupport_channel_ids(
            {"IDA": "Csame", "IDDM": "Csame", "IDO": None}
        )

    def test_mismatch_raises(self):
        with self.assertRaises(ValueError):
            assert_shared_techsupport_channel_ids(
                {"IDA": "C1", "IDDM": "C2", "IDO": None}
            )


if __name__ == "__main__":
    unittest.main()
