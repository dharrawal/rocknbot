"""
smoke/techsupport_classifier.py
====================================
LIVE smoke check (not a unit test): hits real Slack. Do not collect with pytest.

Ad-hoc sanity-check script: fetches a handful of real threads from the
test-techsupport channel (reusing nightly_techsupport_sync.py's Slack
fetching logic -- same token/channel env, same paginate_messages/load_env
helpers) and runs each through techsupport_classifier.classify_thread(),
printing the formatted thread and classification so you can manually compare
against what actually happened in each thread.

*** See techsupport_classifier.py's module docstring: is_useful/is_conclusive
are unoptimized zero-shot outputs with no validation data or tuning behind
them yet. This script is for manual eyeballing, not a benchmark. ***

Usage (from LilLisa_Server, with cron scripts on PYTHONPATH):
    PYTHONPATH=cron python3 smoke/techsupport_classifier.py [thread_ts ...]

With no args, picks up to 4 thread_ts already tracked in
techsupport_sync_state.json, preferring ones that have actual reply activity
(last_seen_reply_ts != thread_ts) over single-message threads.
"""

import sys
from pathlib import Path
from typing import List

SMOKE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SMOKE_DIR.parent / "cron"
sys.path.insert(0, str(SCRIPTS_DIR))

from nightly_techsupport_sync import load_env, load_state, paginate_messages  # noqa: E402
from techsupport_classifier import classify_thread  # noqa: E402

DEFAULT_THREAD_COUNT = 4


def pick_default_thread_ids(limit: int = DEFAULT_THREAD_COUNT) -> List[str]:
    state = load_state()
    threads = state.get("threads", {})
    with_replies = [ts for ts, info in threads.items() if info.get("last_seen_reply_ts") != ts]
    candidates = with_replies if len(with_replies) >= limit else list(threads.keys())
    return candidates[:limit]


def fetch_thread(token: str, channel_id: str, thread_ts: str):
    return paginate_messages(
        "conversations.replies",
        token,
        {"channel": channel_id, "ts": thread_ts, "limit": 200},
    )


def main() -> None:
    env = load_env()
    token = env["SLACK_BOT_TOKEN"]
    channel_id = env["TECHSUPPORT_CHANNEL_ID"]

    thread_ids = sys.argv[1:] or pick_default_thread_ids()
    if not thread_ids:
        print("No thread_ts available (techsupport_sync_state.json is empty and none were passed as args).")
        return

    for thread_ts in thread_ids:
        messages = fetch_thread(token, channel_id, thread_ts)
        if not messages:
            print(f"\n=== Thread {thread_ts}: no messages found, skipping ===")
            continue

        result = classify_thread(messages, slack_token=token)

        print(f"\n=== Thread {thread_ts} ({len(messages)} messages) ===")
        print(result["conversation_thread"])
        print(f"--- is_useful: {result['is_useful']} | is_conclusive: {result['is_conclusive']} ---")


if __name__ == "__main__":
    main()
