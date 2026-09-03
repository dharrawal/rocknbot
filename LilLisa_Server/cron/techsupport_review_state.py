"""
Defensive lookups into techsupport_review_state.json.

The review-state file is a sparse {entries: {str(index): {node_ids, thread_ts}}}
map. Partial writes or entries that predate a field must not KeyError ingest.

An entry may additionally carry "corrections" / "enrichments" provenance lists
(see with_provenance() below); entries written before those existed have
neither, so every read goes through a defensive accessor here.
"""

from typing import Any, Dict, List, Optional


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


def _provenance_list(entry_state: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """Shared defensive read for the "corrections"/"enrichments" history lists."""
    if not isinstance(entry_state, dict):
        return []
    records = entry_state.get(key) or []
    if not isinstance(records, list):
        return []
    return records


def corrections_from_review_entry(entry_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the entry's supersede-provenance records, or [] if missing/invalid.

    Written by techsupport_qa_ingest.correct_verified_entry(); an entry that was
    never corrected (or predates the field) simply has no "corrections" key.
    """
    return _provenance_list(entry_state, "corrections")


def enrichments_from_review_entry(entry_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the entry's append-merge provenance records, or [] if missing/invalid."""
    return _provenance_list(entry_state, "enrichments")


def with_provenance(
    entry_state: Dict[str, Any],
    node_ids: List[str],
    thread_ts: Any,
    *,
    correction: Optional[Dict[str, Any]] = None,
    enrichment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the replacement review-state entry dict for one markdown entry.

    Every write path in techsupport_qa_ingest.py replaces
    review_state["entries"][str(index)] wholesale, so any accumulated
    "corrections"/"enrichments" history has to be carried across explicitly --
    that is what this helper is for. Empty histories are omitted entirely, so
    entries written by the plain add/replace paths keep their original shape.
    """
    new_entry: Dict[str, Any] = {"node_ids": node_ids, "thread_ts": thread_ts}

    corrections = list(corrections_from_review_entry(entry_state))
    if correction is not None:
        corrections.append(correction)
    if corrections:
        new_entry["corrections"] = corrections

    enrichments = list(enrichments_from_review_entry(entry_state))
    if enrichment is not None:
        enrichments.append(enrichment)
    if enrichments:
        new_entry["enrichments"] = enrichments

    return new_entry
