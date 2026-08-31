"""
techsupport_review_sync.py
====================================
Optional-edit sync for the shared verified-techsupport content store: an
expert can edit an existing entry's title/summary text directly in
techsupport_qa_pairs.md whenever they like -- there's no dedicated UI for this
(a private GitHub repo now exists as a push-only backup/visibility mirror,
see github_sync.py and techsupport_qa_ingest.py's FLAGGED OPEN ITEM, but
edits are still made against the local markdown file, not the repo). This
script (run as part of the nightly pipeline) is what makes such an edit take
effect in LanceDB:

    For each markdown entry whose current text differs from what's currently
    stored in LanceDB, delete the corresponding row(s) and re-insert with the
    markdown's *current* title/summary text.

Entries are retrievable as soon as they're added (techsupport_qa_ingest.py) --
there is no review/approval gate, so this script has nothing to do with
gating. It's purely "did the text change, if so update the DB."

Matching markdown entries to LanceDB rows: since an expert might edit the
title text itself, matching by current row content isn't reliable. Instead
techsupport_qa_ingest.py's add_verified_qa_pair() records a
techsupport_review_state.json (git-ignored, alongside techsupport_sync_state.json
/ techsupport_reembed_state.json) mapping each markdown entry's 0-indexed
position to the LanceDB node_id(s) it was inserted as. That mapping is the only
way this script can find entry N's row(s) regardless of text edits. Entries
with no state mapping (e.g. added before this feature existed) are skipped
with a warning -- there's no way to locate their row(s) safely.

Safe to run repeatedly: an entry whose LanceDB text already matches the
markdown's current text is left untouched, so only entries that were actually
edited are ever re-embedded and re-inserted.

Usage (as a library, e.g. from nightly_pipeline.py):
    from techsupport_review_sync import sync_edited_entries
    result = sync_edited_entries()

Usage (standalone):
    python techsupport_review_sync.py
"""

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import lancedb

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from techsupport_qa_ingest import (  # noqa: E402
    TECHSUPPORT_QA_MARKDOWN_FILENAME,
    TECHSUPPORT_QA_TABLE_NAME,
    VERIFIED_TECHSUPPORT_QA_FOLDERPATH,
    _compute_github_url_for_entry,
    _resolve_path,
    configure_embedding_model,
    insert_summary_into_lancedb,
    load_pipeline_env,
    load_review_state,
    parse_summary_markdown,
    save_review_state,
)

from src.llama_index_lancedb_vector_store import LanceDBVectorStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("techsupport_review_sync")


def _sql_string_literal(value: Any) -> str:
    """SQL string literal for LanceDB where predicates.

    LanceDB's Python API takes a SQL predicate string; it does not expose
    bound parameters for search().where(). Single quotes are doubled per SQL.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _row_matches_current_text(table: Any, node_ids: List[str], title: str, summary: str) -> bool:
    """True if every node_id already reflects the markdown's current
    title/summary text -- i.e. nothing left to do for this entry."""
    if not node_ids:
        return False
    id_list = ", ".join(_sql_string_literal(node_id) for node_id in node_ids)
    matches = table.search().where(f"id in ({id_list})").to_pandas()
    if len(matches) != len(node_ids):
        return False  # a row went missing somehow -- treat as needing re-sync
    expected_text = f"## {title}\n\n{summary}"
    for _, row in matches.iterrows():
        if row["text"] != expected_text or row["metadata"].get("title") != title:
            return False
    return True


def sync_edited_entries() -> Dict[str, Any]:
    """Reads techsupport_qa_pairs.md; for every tracked entry whose text no
    longer matches LanceDB, deletes and re-inserts its row(s) with the
    markdown's current title/summary text."""
    markdown_path = VERIFIED_TECHSUPPORT_QA_FOLDERPATH / TECHSUPPORT_QA_MARKDOWN_FILENAME
    if not markdown_path.exists():
        logger.info("No techsupport_qa_pairs.md found at %s -- nothing to sync", markdown_path)
        return {"checked": 0, "synced": 0, "skipped_untracked": 0, "already_synced": 0}

    file_content = markdown_path.read_text(encoding="utf-8")
    entries = parse_summary_markdown(file_content)

    review_state = load_review_state()
    state_entries = review_state["entries"]  # keyed by str(0-indexed markdown position) -- see load_review_state

    configure_embedding_model()
    env = load_pipeline_env()
    lancedb_folderpath = str(_resolve_path(env["LANCEDB_FOLDERPATH"]))
    db = lancedb.connect(lancedb_folderpath)

    counts = {"checked": 0, "synced": 0, "skipped_untracked": 0, "already_synced": 0}

    if TECHSUPPORT_QA_TABLE_NAME not in db.table_names():
        logger.warning("%s table does not exist yet -- nothing to sync", TECHSUPPORT_QA_TABLE_NAME)
        return counts
    table = db.open_table(TECHSUPPORT_QA_TABLE_NAME)

    for index, entry in enumerate(entries):
        counts["checked"] += 1

        state_entry = state_entries.get(str(index))
        if not state_entry or not state_entry.get("node_ids"):
            # DEBUG, not WARNING: for the two bulk historical imports (638 of
            # 672 entries as of this writing), this is permanent, expected
            # state, not a problem -- see the summary line logged below
            # instead, which is what a normal run should surface.
            logger.debug(
                "Entry %d ('%s...') has no tracked LanceDB node_id(s) -- skipping "
                "(likely added before review-sync tracking existed)",
                index, entry["title"][:60],
            )
            counts["skipped_untracked"] += 1
            continue

        node_ids = state_entry["node_ids"]

        if _row_matches_current_text(table, node_ids, entry["title"], entry["summary"]):
            counts["already_synced"] += 1
            continue

        vector_store = LanceDBVectorStore(
            connection=db, uri=lancedb_folderpath, table_name=TECHSUPPORT_QA_TABLE_NAME, query_type="hybrid"
        )
        vector_store.delete_nodes(node_ids)

        # Recompute github_url from the (unchanged since it was read above)
        # current markdown file -- insert_summary_into_lancedb() has no
        # memory of the old row's github_url, so without this an edited
        # entry would silently lose its GitHub link on every resync.
        github_url = _compute_github_url_for_entry(index, markdown_path)
        insert_result = insert_summary_into_lancedb(entry["title"], entry["summary"], github_url=github_url)

        state_entries[str(index)] = {"node_ids": insert_result["node_ids"]}
        save_review_state(review_state)  # persist immediately, same as nightly_pipeline.py's per-thread saves

        counts["synced"] += 1
        logger.info("Entry %d synced: node_ids=%s", index, insert_result["node_ids"])

    if counts["skipped_untracked"]:
        # The one line a normal run should actually show for these -- see
        # module docstring for why they're untrackable (added before
        # review-sync's node_id tracking existed) and why that's permanent
        # short of a dedicated backfill.
        logger.info(
            "%d entries skipped as untracked bulk-imported content -- see module docstring for backfill info",
            counts["skipped_untracked"],
        )

    logger.info(
        "Review sync summary: checked=%d synced=%d already_synced=%d skipped_untracked=%d",
        counts["checked"], counts["synced"], counts["already_synced"], counts["skipped_untracked"],
    )
    return counts


if __name__ == "__main__":
    result = sync_edited_entries()
    print(result)
