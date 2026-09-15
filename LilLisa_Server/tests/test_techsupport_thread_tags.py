"""
Unit tests for techsupport_thread_tags read-modify-write (atomic + merge).

Run from LilLisa_Server:
    PYTHONPATH=. python3 tests/test_techsupport_thread_tags.py
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.techsupport_thread_tags import (  # noqa: E402
    THREAD_TAGS_LOCK,
    load_thread_tags,
    upsert_thread_tag,
)


class ThreadTagsUpsertTests(unittest.TestCase):
    def test_sequential_upserts_keep_both_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "techsupport_thread_tags.json"
            upsert_thread_tag("111.1", "LDAP bind", path=path)
            upsert_thread_tag("222.2", "SSO certs", path=path)
            tags = load_thread_tags(path)
            self.assertEqual(tags["111.1"], "LDAP bind")
            self.assertEqual(tags["222.2"], "SSO certs")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))

    def test_lock_serializes_concurrent_upserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "techsupport_thread_tags.json"
            path.write_text("{}\n", encoding="utf-8")

            async def tagged(thread_ts: str, title: str) -> None:
                async with THREAD_TAGS_LOCK:
                    await asyncio.sleep(0.05)
                    upsert_thread_tag(thread_ts, title, path=path)

            async def run() -> None:
                await asyncio.gather(
                    tagged("aaa.1", "First"),
                    tagged("bbb.2", "Second"),
                )

            asyncio.run(run())
            tags = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(tags["aaa.1"], "First")
            self.assertEqual(tags["bbb.2"], "Second")


if __name__ == "__main__":
    unittest.main()
