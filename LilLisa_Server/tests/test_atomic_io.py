"""
Unit tests for atomic JSON/text writes (temp + os.replace).

Run from LilLisa_Server:
    PYTHONPATH=. python3 tests/test_atomic_io.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from atomic_io import atomic_write_json, atomic_write_text  # noqa: E402
import nightly_techsupport_sync as sync_mod  # noqa: E402


class AtomicWriteTests(unittest.TestCase):
    def test_json_round_trip_and_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"b": 2, "a": 1}, indent=2, sort_keys=True)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertEqual(json.loads(text), {"a": 1, "b": 2})

    def test_replace_keeps_previous_file_if_write_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"ok": True})

            def boom(_self):
                raise OSError("disk full")

            with patch("atomic_io.os.fdopen", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"ok": False})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
            leftovers = list(Path(tmp).glob(".state.json.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_text_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "file.md"
            atomic_write_text(path, "## Title\n\nbody\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "## Title\n\nbody\n")

    def test_sync_save_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "techsupport_sync_state.json"
            with patch.object(sync_mod, "STATE_PATH", path):
                sync_mod.save_state({"last_run_timestamp": "1", "threads": {"t": {"added_to_verified_db": True}}})
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(loaded["threads"]["t"]["added_to_verified_db"])
                sync_mod.save_state({"last_run_timestamp": "2", "threads": {}})
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["last_run_timestamp"], "2")


if __name__ == "__main__":
    unittest.main()
