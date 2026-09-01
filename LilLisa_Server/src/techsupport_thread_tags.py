"""Read-modify-write helper for scripts/techsupport_thread_tags.json.

Callers that serve concurrent requests (POST /tag_techsupport_thread/) must
hold THREAD_TAGS_LOCK around upsert_thread_tag so two escalations cannot
drop each other's keys.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict

from src.atomic_io import atomic_write_json

THREAD_TAGS_LOCK = asyncio.Lock()

DEFAULT_THREAD_TAGS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "techsupport_thread_tags.json"


def load_thread_tags(path: Path | None = None) -> Dict[str, str]:
    tags_path = path or DEFAULT_THREAD_TAGS_PATH
    if not tags_path.exists():
        return {}
    loaded = json.loads(tags_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items()}


def save_thread_tags(tags: Dict[str, str], path: Path | None = None) -> None:
    tags_path = path or DEFAULT_THREAD_TAGS_PATH
    atomic_write_json(tags_path, tags)


def upsert_thread_tag(thread_ts: str, related_entry_title: str, path: Path | None = None) -> Dict[str, str]:
    """Load tags, set thread_ts -> title, atomic-write. Not itself locked."""
    tags = load_thread_tags(path)
    tags[thread_ts] = related_entry_title
    save_thread_tags(tags, path)
    return tags
