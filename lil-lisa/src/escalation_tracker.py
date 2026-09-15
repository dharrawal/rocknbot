"""Persist successful Slack escalations across bot restarts.

Thumbs-up / thumbs-down stay in process memory (ENDORSEMENT_TRACKER). Only
`escalated` is durable, and only for ESCALATION_MAX_AGE_DAYS (default 90).
After that the original thread may get a bot reply again.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_ESCALATION_MAX_AGE_DAYS = 90
DEFAULT_TRACKER_PATH = Path(__file__).resolve().parent.parent / "escalation_tracker.json"

_FILE_LOCK = threading.Lock()


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        text = json.dumps(obj, indent=2)
        if not text.endswith("\n"):
            text += "\n"
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def tracker_path() -> Path:
    override = os.environ.get("ESCALATION_TRACKER_PATH")
    if override:
        return Path(override)
    return DEFAULT_TRACKER_PATH


def max_age_days() -> float:
    raw = os.environ.get("ESCALATION_MAX_AGE_DAYS")
    if raw:
        return float(raw)
    return float(DEFAULT_ESCALATION_MAX_AGE_DAYS)


def _parse_escalated_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_fresh(escalated_at: datetime, now: datetime, age_days: float) -> bool:
    return now - escalated_at <= timedelta(days=age_days)


def load_escalations(
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    age_days: Optional[float] = None,
) -> Dict[str, datetime]:
    """Return {conv_id: escalated_at} for entries still within the max age."""
    store_path = path if path is not None else tracker_path()
    current = now or datetime.now(timezone.utc)
    limit = max_age_days() if age_days is None else age_days
    if not store_path.exists():
        return {}
    try:
        loaded = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    fresh: Dict[str, datetime] = {}
    for conv_id, record in loaded.items():
        if not isinstance(record, dict):
            continue
        escalated_at = _parse_escalated_at(record.get("escalated_at"))
        if escalated_at is None:
            continue
        if _is_fresh(escalated_at, current, limit):
            fresh[str(conv_id)] = escalated_at
    return fresh


def save_escalations(entries: Dict[str, datetime], path: Optional[Path] = None) -> None:
    store_path = path if path is not None else tracker_path()
    payload = {
        conv_id: {"escalated_at": escalated_at.isoformat()}
        for conv_id, escalated_at in sorted(entries.items())
    }
    _atomic_write_json(store_path, payload)


def is_escalation_active(
    conv_id: str,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    age_days: Optional[float] = None,
) -> bool:
    with _FILE_LOCK:
        return conv_id in load_escalations(path=path, now=now, age_days=age_days)


def claim_escalation(
    conv_id: str,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    age_days: Optional[float] = None,
) -> bool:
    """Record this thread as escalated. False if it was already active (not expired)."""
    with _FILE_LOCK:
        current = now or datetime.now(timezone.utc)
        entries = load_escalations(path=path, now=current, age_days=age_days)
        if conv_id in entries:
            return False
        entries[conv_id] = current
        save_escalations(entries, path=path)
        return True


def clear_escalation(
    conv_id: str,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    age_days: Optional[float] = None,
) -> None:
    with _FILE_LOCK:
        current = now or datetime.now(timezone.utc)
        entries = load_escalations(path=path, now=current, age_days=age_days)
        if conv_id not in entries:
            return
        del entries[conv_id]
        save_escalations(entries, path=path)


def hydrate_endorsement_tracker(
    tracker: Dict[str, Dict[str, Any]],
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    age_days: Optional[float] = None,
) -> None:
    """Set escalated=True on in-memory tracker rows that are still persisted."""
    for conv_id in load_escalations(path=path, now=now, age_days=age_days):
        if conv_id not in tracker:
            tracker[conv_id] = {
                "message_endorsed": False,
                "reaction_endorsed": False,
                "escalated": True,
            }
        else:
            tracker[conv_id]["escalated"] = True
