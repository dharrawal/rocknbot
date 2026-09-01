"""Format nightly_pipeline ingest counts for logs and admin Slack alerts."""

from typing import Any, Iterable, List, Mapping, Optional

# Human-readable order for the pipeline summary log and admin-alert parenthetical.
# Keys not listed here still appear (sorted) so a new counts entry cannot go silent.
PIPELINE_COUNT_KEYS = (
    "checked",
    "added",
    "enriched",
    "replaced",
    "left_as_is_not_useful",
    "left_as_is_not_conclusive",
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
