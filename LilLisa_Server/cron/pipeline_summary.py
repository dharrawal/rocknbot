"""Format nightly_pipeline ingest counts for logs and admin Slack alerts.

Serves both the techsupport loop's counts and the product-channel pass's
(which adds "corrected" and "skipped_no_expert_reply", and carries no
"enriched"); every key is optional, and anything unknown still shows up.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# Product-channel failures that happen before any thread is scanned (an expert
# user group that cannot be resolved) are stored with this thread_ts prefix.
# They increment `errored` and must not be described as threads.
CHANNEL_ERROR_PREFIX = "channel "

# Human-readable order for the pipeline summary log and admin-alert parenthetical.
# Keys not listed here still appear (sorted) so a new counts entry cannot go silent.
PIPELINE_COUNT_KEYS = (
    "checked",
    "added",
    "enriched",
    "corrected",
    "replaced",
    "left_as_is_not_useful",
    "left_as_is_not_conclusive",
    "skipped_no_expert_reply",
    "skipped_no_expert_insight",
    "skipped_not_useful",
    "skipped_not_conclusive",
    "errored",
)


def format_pipeline_counts(
    counts: Mapping[str, Any],
    *,
    omit: Optional[Iterable[str]] = None,
) -> str:
    """Render ingest outcome counts as `key=value` pairs for logs and Slack alerts.

    Known keys keep a stable order (enriched sits with added/replaced). Any extra
    keys in `counts` are appended sorted so a new outcome cannot vanish from the
    summary the way `enriched` did.
    """
    skip = set(omit or ())
    parts: List[str] = []
    seen = set()
    for key in PIPELINE_COUNT_KEYS:
        if key in skip or key not in counts:
            continue
        parts.append(f"{key}={counts[key]}")
        seen.add(key)
    extras = sorted(k for k in counts if k not in seen and k not in skip)
    for key in extras:
        parts.append(f"{key}={counts[key]}")
    return " ".join(parts)


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def counts_for_error_alert(*sources: Mapping[str, Any]) -> Dict[str, int]:
    """Sum numeric outcome counts for one admin alert.

    The first source decides which zero keys stay visible, so a techsupport-only
    alert keeps its existing parenthetical. Later sources add into keys that are
    already present, and contribute a key of their own only when it is non-zero.
    A product pass that did nothing therefore does not pad the alert with
    corrected=0 and skipped_no_expert_reply=0, while a real product outcome
    still shows up.
    """
    merged: Dict[str, int] = {}
    for index, source in enumerate(sources):
        for key, raw in source.items():
            value = _as_count(raw)
            if value is None:
                continue
            if key in merged:
                merged[key] += value
            elif index == 0 or value != 0:
                merged[key] = value
    return merged


def format_pipeline_error_alert(
    prefix: str,
    errors: Sequence[Mapping[str, str]],
    *count_sources: Mapping[str, Any],
) -> str:
    """One admin-alert body whose headline and parenthetical count the same failures.

    Thread failures stay "N thread(s) errored out of M checked". Entries whose
    thread_ts starts with CHANNEL_ERROR_PREFIX are product channels that failed
    before any thread was checked (expert group missing or unreadable); they are
    called out separately so they are not "threads errored out of 0 checked".
    `errored` in the parenthetical is the sum across every count source, which
    is the thread failures plus those channel failures.
    """
    combined = counts_for_error_alert(*count_sources)
    checked = combined.get("checked", 0)
    errored = combined.get("errored", 0)
    channel_failed = sum(1 for error in errors if str(error.get("thread_ts", "")).startswith(CHANNEL_ERROR_PREFIX))
    thread_errored = max(0, errored - channel_failed)
    clauses: List[str] = []
    if thread_errored or not channel_failed:
        clauses.append(f"{thread_errored} thread(s) errored out of {checked} checked")
    if channel_failed:
        clauses.append(f"{channel_failed} product channel(s) failed")
    error_lines = "\n".join(f"- {error['thread_ts']}: {error['error']}" for error in errors)
    parenthetical = format_pipeline_counts(combined, omit=("checked",))
    return f"{prefix}: {'; '.join(clauses)} ({parenthetical}).\n{error_lines}"
