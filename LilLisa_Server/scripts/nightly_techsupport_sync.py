"""
nightly_techsupport_sync.py
====================================
Standalone script (no dependency on LilLisa_Server or lil-lisa's runtime code)
that detects which threads in a Slack techsupport channel are new or have new
reply activity since the last run.

Slack's conversations.history only returns new top-level messages (filtered by
an "oldest" timestamp) -- it does NOT surface new replies posted to existing
threads. To catch updated threads we keep a local record of every thread we've
seen and its last-known-latest-reply timestamp, and on each run call
conversations.replies for each tracked thread to see if that timestamp moved.

This is step one of the nightly techsupport sync job: it only detects new /
updated threads. Classifying threads as resolved (or anything else) is a
separate, later step.

Required env vars (see ./env/techsupport_sync.env):
    SLACK_BOT_TOKEN        - Slack bot token with channels:history / groups:history
                              (and the read scope for the channel type in use)
    TECHSUPPORT_CHANNEL_ID - Channel ID to sync (e.g. the test-techsupport channel)

Usage:
    python nightly_techsupport_sync.py
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / "env" / "techsupport_sync.env"
STATE_PATH = SCRIPT_DIR / "techsupport_sync_state.json"

SLACK_API_BASE = "https://slack.com/api"
IGNORED_SUBTYPES = {"channel_join", "channel_leave"}

# Same propagation-delay quirk fixed the same way in
# lil-lisa/src/slack.py's conversations_replies_with_retry(): a channel/
# message can momentarily fail to resolve via the Web API an instant after
# it's posted, even though it's otherwise valid and a retry a few seconds
# later succeeds. Same delay/attempt counts as that fix.
CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS = 3.0
CHANNEL_NOT_FOUND_MAX_RETRIES = 2

# sync()'s per-thread conversations.replies loop below fires one call per
# tracked thread back-to-back on the same token with no throttling, unlike
# paginate_messages()'s own inter-page sleep(1). With dozens of tracked
# threads that's a burst of same-token requests in a few seconds -- observed
# to trigger transient channel_not_found (retries exhausted, all 3 attempts
# failing) that isolated single-request tests against the same channel/token
# never hit. This throttles that loop the same way pagination already is.
PER_THREAD_THROTTLE_SECONDS = 0.3

# No basicConfig() call -- library module, imported into nightly_pipeline.py
# (which configures logging itself); this just needs a named logger that
# inherits whichever configuration is already in place. Standalone runs
# (main() below) don't configure logging either, matching this module's
# existing print()-based console output -- INFO-level retry logging here
# is aimed at nightly_pipeline.py's log stream, not standalone use.
logger = logging.getLogger(__name__)


def load_env() -> Dict[str, str]:
    """Load env vars the same way lil-lisa/src/slack.py does: values from the
    env file, overridden by any matching variables already in the process
    environment."""
    env = dict(dotenv_values(str(ENV_PATH)))
    env = {**env, **os.environ}

    required = ["SLACK_BOT_TOKEN", "TECHSUPPORT_CHANNEL_ID"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s) {missing} - expected in {ENV_PATH}"
        )
    return env


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_run_timestamp": "0", "threads": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def slack_api_call(method: str, token: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Call a Slack Web API method, retrying on rate limits and, a limited
    number of times, on "channel_not_found" -- see
    CHANNEL_NOT_FOUND_MAX_RETRIES above for why. Any other error is raised
    immediately, unretried, so it isn't masked."""
    url = f"{SLACK_API_BASE}/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    channel_not_found_attempts = 0
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        data = resp.json()
        if data.get("ok"):
            return data
        if data.get("error") == "ratelimited":
            retry_after = int(resp.headers.get("Retry-After", "1"))
            time.sleep(retry_after)
            continue
        if data.get("error") == "channel_not_found" and channel_not_found_attempts < CHANNEL_NOT_FOUND_MAX_RETRIES:
            channel_not_found_attempts += 1
            logger.info(
                "[RETRY %s] channel_not_found for channel=%r (attempt %d/%d) -- retrying in %ss",
                method, params.get("channel"), channel_not_found_attempts, CHANNEL_NOT_FOUND_MAX_RETRIES,
                CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS,
            )
            time.sleep(CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS)
            continue
        if data.get("error") == "channel_not_found":
            logger.error(
                "[RETRY %s] Exhausted retries -- channel_not_found persisted for channel=%r",
                method, params.get("channel"),
            )
        raise RuntimeError(f"Slack API error calling {method}: {data.get('error')}")


def paginate_messages(
    method: str, token: str, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Collect the "messages" list across all pages of a cursor-paginated
    Slack conversations.* method."""
    messages: List[Dict[str, Any]] = []
    cursor = None
    while True:
        call_params = dict(params)
        if cursor:
            call_params["cursor"] = cursor
        data = slack_api_call(method, token, call_params)
        messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return messages
        time.sleep(1)


def find_new_threads(
    token: str, channel_id: str, oldest: str, known_thread_ids: set
) -> List[Dict[str, Any]]:
    """Find top-level messages posted since `oldest` that aren't already tracked."""
    history = paginate_messages(
        "conversations.history",
        token,
        {"channel": channel_id, "oldest": oldest, "limit": 200},
    )
    new_threads = []
    for msg in history:
        if msg.get("subtype") in IGNORED_SUBTYPES:
            continue
        thread_ts = msg["ts"]
        if thread_ts in known_thread_ids:
            continue
        new_threads.append(msg)
    return new_threads


def latest_reply_ts(token: str, channel_id: str, thread_ts: str) -> str:
    """Latest message ts in a thread (the parent itself if it has no replies)."""
    replies = paginate_messages(
        "conversations.replies",
        token,
        {"channel": channel_id, "ts": thread_ts, "limit": 200},
    )
    if not replies:
        return thread_ts
    return replies[-1]["ts"]


def sync() -> Dict[str, List[str]]:
    """Detect new/updated threads in the techsupport channel and update the
    on-disk state file accordingly. Returns
    {"new_thread_ids": [...], "updated_thread_ids": [...]} so callers (e.g.
    nightly_pipeline.py) can process exactly the threads that changed since
    the last run, without re-implementing the state-tracking logic here."""
    env = load_env()
    token = env["SLACK_BOT_TOKEN"]
    channel_id = env["TECHSUPPORT_CHANNEL_ID"]

    state = load_state()
    threads = state.setdefault("threads", {})
    last_run_timestamp = state.get("last_run_timestamp", "0")

    # Snapshot of threads we already knew about *before* this run, so we can
    # tell "brand new" apart from "existing, checked for new replies".
    previously_known_thread_ids = list(threads.keys())

    new_threads = find_new_threads(
        token, channel_id, last_run_timestamp, set(previously_known_thread_ids)
    )
    for msg in new_threads:
        thread_ts = msg["ts"]
        # conversations.history already tells us the latest reply ts for
        # threads with replies, so no extra API call is needed here.
        threads[thread_ts] = {"last_seen_reply_ts": msg.get("latest_reply", thread_ts)}

    updated_thread_ids = []
    for thread_ts in previously_known_thread_ids:
        newest_ts = latest_reply_ts(token, channel_id, thread_ts)
        if float(newest_ts) > float(threads[thread_ts]["last_seen_reply_ts"]):
            updated_thread_ids.append(thread_ts)
        threads[thread_ts]["last_seen_reply_ts"] = newest_ts
        time.sleep(PER_THREAD_THROTTLE_SECONDS)

    state["last_run_timestamp"] = f"{time.time():.6f}"
    save_state(state)

    new_thread_ids = [msg["ts"] for msg in new_threads]
    return {"new_thread_ids": new_thread_ids, "updated_thread_ids": updated_thread_ids}


def main() -> None:
    result = sync()
    print(f"Brand new threads: {len(result['new_thread_ids'])}")
    for thread_ts in result["new_thread_ids"]:
        print(f"  {thread_ts}")
    print(f"Existing threads with new activity: {len(result['updated_thread_ids'])}")
    for thread_ts in result["updated_thread_ids"]:
        print(f"  {thread_ts}")


if __name__ == "__main__":
    main()
