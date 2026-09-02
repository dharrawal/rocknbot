"""Parse and sanitize techsupport_qa_pairs.md heading blocks.

Kept free of DSPy/LanceDB so unit tests can run without the pipeline venv.
"""

import re
from typing import Any, Dict, List

# Splits on any line starting with "## " (the title heading marker), so the
# entry text itself must never start a line with "## ".
_TITLE_BLOCK_SPLIT_PATTERN = re.compile(r"(?m)^## ")
_TITLE_SUMMARY_PATTERN = re.compile(r"(.+?)\n\n(.*)", re.DOTALL)
_LEADING_HEADING_MARKERS = re.compile(r"^#+\s*")


def sanitize_techsupport_title(title: str) -> str:
    """Strip markdown heading markers so titles cannot re-split the file."""
    cleaned = _LEADING_HEADING_MARKERS.sub("", (title or "").strip())
    cleaned = cleaned.replace("#", "").strip()
    return cleaned or "Untitled"


def sanitize_techsupport_summary(summary: str) -> str:
    """Indent lines that start with ## so parse_summary_markdown will not split."""
    lines = []
    for line in (summary or "").splitlines():
        if line.startswith("##"):
            lines.append("  " + line.lstrip())
        else:
            lines.append(line)
    return "\n".join(lines).strip()


def uniquify_techsupport_title(title: str, existing_titles: List[str]) -> str:
    """If title already exists, append ' - 2', ' - 3', ... (pr42-mp.1.10)."""
    taken = set(existing_titles)
    if title not in taken:
        return title
    n = 2
    while f"{title} - {n}" in taken:
        n += 1
    return f"{title} - {n}"


def prepare_title_and_summary_for_markdown(title: str, summary: str, existing_titles: List[str]) -> tuple[str, str]:
    """Sanitize heading markers then make the title unique among existing_titles."""
    title = uniquify_techsupport_title(sanitize_techsupport_title(title), existing_titles)
    summary = sanitize_techsupport_summary(summary)
    return title, summary


def parse_summary_markdown(file_content: str) -> List[Dict[str, Any]]:
    """Parse techsupport_qa_pairs.md into an ordered list of
    {"title", "summary"} dicts, one per "## {title}" heading block, in file
    order."""
    entries = []
    for raw_block in _TITLE_BLOCK_SPLIT_PATTERN.split(file_content):
        block = raw_block.strip()
        if not block:
            continue
        match = _TITLE_SUMMARY_PATTERN.match(block)
        if not match:
            continue
        title, summary = match[1].strip(), match[2].strip()
        if title and summary:
            entries.append({"title": title, "summary": summary})
    return entries
