"""
Defensive lookups into techsupport_review_state.json.

The review-state file is a sparse {entries: {str(index): {node_ids, thread_ts}}}
map. Partial writes or entries that predate a field must not KeyError ingest.
"""

from typing import Any, Dict, List


def review_entry_state(review_state: Dict[str, Any], entry_index: int) -> Dict[str, Any]:
    """Return review_state["entries"][str(entry_index)] or {} if missing/corrupt."""
    entries = review_state.get("entries") if isinstance(review_state, dict) else None
    if not isinstance(entries, dict):
        return {}
    entry = entries.get(str(entry_index), {})
    return entry if isinstance(entry, dict) else {}


def node_ids_from_review_entry(entry_state: Dict[str, Any]) -> List[str]:
    """Return tracked LanceDB node ids, or [] if the field is missing/invalid."""
    if not isinstance(entry_state, dict):
        return []
    node_ids = entry_state.get("node_ids") or []
    if not isinstance(node_ids, list):
        return []
    return node_ids
