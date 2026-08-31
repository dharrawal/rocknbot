"""
backfill_github_urls.py
====================================
One-time backfill: adds node.metadata["github_url"] to every existing
TECHSUPPORT_QA_PAIRS row that doesn't already have one -- i.e. every entry
added before github_anchor.py/the GitHub anchor-link feature existed (see
techsupport_qa_ingest.py's FLAGGED OPEN ITEM). New entries already get this
at insert time (techsupport_qa_ingest.insert_summary_into_lancedb) and every
periodic full re-embed already recomputes it for every row from scratch
(techsupport_contextual_reembed.build_contextual_nodes) -- this script exists
purely to cover the gap for rows that predate both of those and won't see
another full re-embed for up to TECHSUPPORT_REEMBED_INTERVAL_DAYS.

Pure metadata patch, NOT a content change: titles/summaries are not
regenerated and embeddings are not recomputed. Each row's `vector` and `text`
columns are read and written back byte-for-byte identical; only the
"metadata" struct column's _node_content string gets a new "github_url" key
added to its embedded metadata dict. See module docstring in
techsupport_qa_ingest.insert_summary_into_lancedb() for why _node_content
(not a flat top-level "github_url" struct field) is the only way to add a
genuinely new metadata key to an existing LanceDB table without a schema
migration -- flat fields not already in the table's fixed struct schema are
silently dropped by LanceDB on write, but _node_content is a plain string
column, schema-invisible to whatever JSON text it holds.

Mechanically: LanceDB's table.update() cannot set struct-typed columns
(raises "SQL conversion is not implemented for this type" -- confirmed
empirically), so each patched row is done as delete-by-id + re-add of the
same row data (same pattern replace_verified_qa_pair() already uses for
content updates) rather than a real UPDATE. Two separate operations, not
atomic -- acceptable for a one-time script; a crash between them would drop
that one row, so ALWAYS take a filesystem-level backup of the ./lancedb
folder before running for real (not scripted here since a copy of a
potentially-large directory tree is itself just as easily done in a shell).

Matching rows to titles: title -> github_url is a plain dict built from
techsupport_qa_pairs.md's current heading order run through
github_anchor.compute_github_urls_for_titles(). This requires every title in
the file to be unique -- verified explicitly below (raises if not, rather
than silently mismatching a row to the wrong URL) -- true for all 672
original entries as of this writing. If a row's title has no match in the
current markdown file at all (e.g. the file changed since that row was
written and the title text drifted), it's skipped and reported, not guessed.

Idempotent / resumable: any row that already has a "github_url" key in its
_node_content metadata is left untouched, so re-running only touches rows
still missing it.

Usage:
    # Dry run: report what WOULD change, write nothing.
    python backfill_github_urls.py --dry-run

    # Real run.
    python backfill_github_urls.py
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

import lancedb

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from github_anchor import compute_github_urls_for_titles  # noqa: E402

# Reused from techsupport_contextual_reembed.py rather than
# techsupport_qa_ingest.py -- same reasoning that module documents for
# itself: avoids triggering techsupport_qa_ingest's module-level
# configure_dspy_lm() (an LLM dependency this pure-metadata script has no
# need for).
from techsupport_contextual_reembed import (  # noqa: E402
    TECHSUPPORT_QA_MARKDOWN_FILENAME,
    TECHSUPPORT_QA_TABLE_NAME,
    VERIFIED_TECHSUPPORT_QA_FOLDERPATH,
    _resolve_path,
    load_pipeline_env,
    parse_qa_pairs,
)


def build_title_to_url_map() -> Dict[str, str]:
    """Parses the current techsupport_qa_pairs.md and returns {title:
    github_url}, computed via the real GitHub duplicate-heading slug
    algorithm. Raises if any title appears more than once -- title-only
    matching (no positional info) is only safe when titles are unique."""
    markdown_path = VERIFIED_TECHSUPPORT_QA_FOLDERPATH / TECHSUPPORT_QA_MARKDOWN_FILENAME
    if not markdown_path.exists():
        raise FileNotFoundError(f"Techsupport QA markdown file not found: {markdown_path}")

    qa_pairs = parse_qa_pairs(markdown_path.read_text(encoding="utf-8"))
    titles_in_order = [title for title, _summary in qa_pairs]

    duplicate_titles = {title for title in titles_in_order if titles_in_order.count(title) > 1}
    if duplicate_titles:
        raise RuntimeError(
            f"Refusing to backfill: {len(duplicate_titles)} duplicate title(s) found in "
            f"{markdown_path}, which title-only row matching cannot safely disambiguate: "
            f"{sorted(duplicate_titles)}"
        )

    github_urls = compute_github_urls_for_titles(titles_in_order)
    return dict(zip(titles_in_order, github_urls))


def run(dry_run: bool = False) -> Dict[str, Any]:
    title_to_url = build_title_to_url_map()

    env = load_pipeline_env()
    lancedb_folderpath = str(_resolve_path(env["LANCEDB_FOLDERPATH"]))
    db = lancedb.connect(lancedb_folderpath)
    table = db.open_table(TECHSUPPORT_QA_TABLE_NAME)

    df = table.to_pandas()
    counts = {"total_rows": len(df), "already_had_url": 0, "updated": 0, "title_not_found": 0}
    title_not_found_examples = []

    for _, row in df.iterrows():
        flat_metadata = dict(row["metadata"])
        node_content = json.loads(flat_metadata["_node_content"])
        node_metadata = node_content["metadata"]

        if node_metadata.get("github_url"):
            counts["already_had_url"] += 1
            continue

        title = node_metadata.get("title")
        github_url = title_to_url.get(title)
        if not github_url:
            counts["title_not_found"] += 1
            if len(title_not_found_examples) < 10:
                title_not_found_examples.append({"id": row["id"], "title": title})
            continue

        counts["updated"] += 1
        if dry_run:
            continue

        node_metadata["github_url"] = github_url
        flat_metadata["_node_content"] = json.dumps(node_content)
        new_row = {
            "id": row["id"],
            "doc_id": row["doc_id"],
            "vector": row["vector"],
            "text": row["text"],
            "metadata": flat_metadata,
        }
        table.delete(f"id = '{row['id']}'")
        table.add([new_row])

    counts["title_not_found_examples"] = title_not_found_examples
    counts["dry_run"] = dry_run
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)

    print("\n=== GitHub URL Backfill Summary ===")
    print(f"Total rows in TECHSUPPORT_QA_PAIRS: {result['total_rows']}")
    print(f"Already had github_url (skipped): {result['already_had_url']}")
    print(f"{'Would update' if result['dry_run'] else 'Updated'}: {result['updated']}")
    print(f"Title not found in current markdown (skipped): {result['title_not_found']}")
    if result["title_not_found_examples"]:
        print("  Examples:")
        for ex in result["title_not_found_examples"]:
            print(f"    id={ex['id']} title={ex['title']!r}")
    if result["dry_run"]:
        print("(dry run -- nothing was written to LanceDB)")


if __name__ == "__main__":
    main()
