"""
Unit tests for persisted Slack escalations (90-day TTL).

Run from lil-lisa:
    PYTHONPATH=src python3 tests/test_escalation_tracker.py
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from escalation_tracker import (  # noqa: E402
    claim_escalation,
    clear_escalation,
    hydrate_endorsement_tracker,
    is_escalation_active,
    load_escalations,
)


class EscalationTrackerTests(unittest.TestCase):
    def test_claim_survives_reload_within_ttl(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escalation_tracker.json"
            self.assertTrue(claim_escalation("111.1", path=path, now=now, age_days=90))
            self.assertFalse(claim_escalation("111.1", path=path, now=now, age_days=90))
            self.assertTrue(is_escalation_active("111.1", path=path, now=now, age_days=90))
            loaded = load_escalations(path=path, now=now, age_days=90)
            self.assertIn("111.1", loaded)

    def test_entries_older_than_90_days_are_dropped(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escalation_tracker.json"
            path.write_text(
                json.dumps(
                    {
                        "old.1": {"escalated_at": (now - timedelta(days=91)).isoformat()},
                        "fresh.1": {"escalated_at": (now - timedelta(days=89)).isoformat()},
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_escalations(path=path, now=now, age_days=90)
            self.assertNotIn("old.1", loaded)
            self.assertIn("fresh.1", loaded)
            self.assertFalse(is_escalation_active("old.1", path=path, now=now, age_days=90))
            self.assertTrue(is_escalation_active("fresh.1", path=path, now=now, age_days=90))

    def test_exactly_90_days_still_active(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escalation_tracker.json"
            path.write_text(
                json.dumps({"edge.1": {"escalated_at": (now - timedelta(days=90)).isoformat()}}),
                encoding="utf-8",
            )
            self.assertTrue(is_escalation_active("edge.1", path=path, now=now, age_days=90))

    def test_clear_removes_persisted_row(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escalation_tracker.json"
            claim_escalation("111.1", path=path, now=now, age_days=90)
            clear_escalation("111.1", path=path, now=now, age_days=90)
            self.assertFalse(is_escalation_active("111.1", path=path, now=now, age_days=90))

    def test_hydrate_only_sets_escalated_flag(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escalation_tracker.json"
            claim_escalation("111.1", path=path, now=now, age_days=90)
            tracker = {}
            hydrate_endorsement_tracker(tracker, path=path, now=now, age_days=90)
            self.assertTrue(tracker["111.1"]["escalated"])
            self.assertFalse(tracker["111.1"]["message_endorsed"])
            self.assertFalse(tracker["111.1"]["reaction_endorsed"])


if __name__ == "__main__":
    unittest.main()
