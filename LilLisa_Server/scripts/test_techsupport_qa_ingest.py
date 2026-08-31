"""
test_techsupport_qa_ingest.py
====================================
Ad-hoc end-to-end check of the step-6 "add" pipeline (techsupport_qa_ingest.py):
takes one real Slack thread that techsupport_classifier.classify_thread()
scores as useful + conclusive, runs it through add_verified_qa_pair(), and
prints everything needed to eyeball correctness:
    - the generated title + summary
    - the markdown file's full contents after the append
    - a direct LanceDB query proving the new row landed in the shared
      TECHSUPPORT_QA_PAIRS table
    - row counts for the existing golden IDA_QA_PAIRS / IDDM_QA_PAIRS tables,
      before and after, to confirm this pipeline never touches them

The pipeline is product-agnostic (one shared techsupport channel, one shared
verified-QA store used by both IDA and IDDM) -- there is no product argument.

Usage:
    python test_techsupport_qa_ingest.py [thread_ts]

With no args, defaults to the real LDAP/firewall thread already confirmed to
classify as useful+conclusive (thread_ts 1785094655.495349).
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import lancedb  # noqa: E402

from nightly_techsupport_sync import load_env, paginate_messages  # noqa: E402
from techsupport_classifier import classify_thread  # noqa: E402
from techsupport_qa_ingest import (  # noqa: E402
    PROJECT_ROOT,
    add_verified_qa_pair,
    load_pipeline_env,
    _resolve_path,
)

DEFAULT_THREAD_TS = "1785094655.495349"


def existing_golden_table_row_counts(lancedb_folderpath: str) -> dict:
    db = lancedb.connect(lancedb_folderpath)
    counts = {}
    for table_name in ("IDA_QA_PAIRS", "IDDM_QA_PAIRS", "IDO_QA_PAIRS"):
        if table_name in db.table_names():
            counts[table_name] = db.open_table(table_name).count_rows()
    return counts


def main() -> None:
    thread_ts = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_THREAD_TS

    slack_env = load_env()
    token = slack_env["SLACK_BOT_TOKEN"]
    channel_id = slack_env["TECHSUPPORT_CHANNEL_ID"]

    messages = paginate_messages(
        "conversations.replies", token, {"channel": channel_id, "ts": thread_ts, "limit": 200}
    )
    if not messages:
        print(f"No messages found for thread {thread_ts}")
        return

    result = classify_thread(messages, slack_token=token)
    print("=== Source thread ===")
    print(result["conversation_thread"])
    print(f"--- is_useful: {result['is_useful']} | is_conclusive: {result['is_conclusive']} ---\n")

    if not (result["is_useful"] and result["is_conclusive"]):
        print("Thread did not classify as useful+conclusive -- not running the add pipeline.")
        return

    pipeline_env = load_pipeline_env()
    lancedb_folderpath = str(_resolve_path(pipeline_env["LANCEDB_FOLDERPATH"]))

    print("=== Golden QA pairs table row counts BEFORE ingest ===")
    before_counts = existing_golden_table_row_counts(lancedb_folderpath)
    print(before_counts)

    add_result = add_verified_qa_pair(result["conversation_thread"])

    print("\n=== Generated title + summary ===")
    print(f"Title: {add_result['title']}")
    print(f"Summary: {add_result['summary']}")

    print(f"\n=== Markdown file contents ({add_result['markdown_path']}) ===")
    print(Path(add_result["markdown_path"]).read_text(encoding="utf-8"))

    print(f"=== Direct LanceDB query against {add_result['table_name']} ===")
    db = lancedb.connect(lancedb_folderpath)
    table = db.open_table(add_result["table_name"])
    df = table.to_pandas()
    print(f"Row count: {table.count_rows()}")
    print(df[["id", "text"]].to_string())
    for node_id in add_result["node_ids"]:
        match = df[df["id"] == node_id]
        print(f"\nNew row (id={node_id}):")
        if not match.empty:
            row = match.iloc[0]
            print(f"  text: {row['text']}")
            print(f"  metadata.title: {row['metadata']['title']}")
            print(f"  metadata.source: {row['metadata']['source']}")
        else:
            print("  NOT FOUND -- something went wrong")

    print("\n=== Golden QA pairs table row counts AFTER ingest (should be unchanged) ===")
    after_counts = existing_golden_table_row_counts(lancedb_folderpath)
    print(after_counts)
    print("MATCH -- golden tables untouched" if before_counts == after_counts else "MISMATCH -- golden tables were affected!")


if __name__ == "__main__":
    main()
