"""
nightly_pipeline.py
====================================
End-to-end nightly techsupport pipeline, tying together the three pieces
built in earlier steps:
    1. nightly_techsupport_sync.sync() -- detect new/updated threads in the
       one shared techsupport Slack channel (TECHSUPPORT_CHANNEL_ID in
       cron/env/techsupport_sync.env; the same channel is used for IDA,
       IDDM, and IDO -- see lil-lisa/app_envfiles/lil-lisa.env's
       TECHSUPPORT_CHANNEL_ID_IDA/IDDM/IDO, which are all equal).
    2. techsupport_classifier.classify_thread() -- classify each changed
       thread as useful / conclusive.
    3. techsupport_qa_ingest.add_verified_qa_pair() -- for threads that are
       both useful and conclusive, extract a Q&A pair and add it to the
       single shared verified-techsupport store (product-agnostic; there is
       no per-product classification step). Entries are retrievable
       immediately -- there is no expert-review gate.

Each thread is processed independently (its own try/except) so one bad
thread can't take down the whole run. A per-run summary is logged, and if
any thread errored, the error details are posted to the shared admin Slack
channel (ADMIN_CHANNEL_ID in cron/env/techsupport_sync.env -- reuses the
same channel lil-lisa's ADMIN_CHANNEL_ID_IDA/ADMIN_CHANNEL_ID_IDDM already
point at).

How often step 1 actually checks the Slack channel is itself interval-gated
by TECHSUPPORT_SYNC_INTERVAL_HOURS (env/lillisa_server.env, default 24, i.e.
once a day) -- is_channel_check_due() below checks elapsed time since the
last check (tracked in techsupport_sync_state.json's last_run_timestamp,
which nightly_techsupport_sync.sync() already maintains) and skips calling
sync() entirely if the interval hasn't elapsed, the same
"check-elapsed-time-skip-if-not-due" pattern
techsupport_contextual_reembed.is_reembed_due() uses. This is independent of
TECHSUPPORT_REEMBED_INTERVAL_DAYS below, which controls how often the
contextual re-embed runs, not how often the channel gets checked.

Dedup: whether a thread has already been added to the verified DB is tracked
in techsupport_sync_state.json (the same per-thread state file
nightly_techsupport_sync.py already maintains for last_seen_reply_ts), via an
"added_to_verified_db": true field. A thread is only marked this way AFTER
add_verified_qa_pair() returns successfully, and the flag is saved to disk
immediately (not batched until the end of the run) so a crash partway through
a run doesn't lose track of threads that were already added -- a retry will
correctly skip those and only reconsider threads that weren't marked.

An already-added thread is only ever reconsidered when sync() itself detects
new reply activity on it (i.e. it shows up in updated_thread_ids); a thread
with no new activity is never touched, added_to_verified_db=true or not. When
one IS reconsidered, it is re-classified against the complete, current
conversation. If still useful+conclusive, the existing verified entry is
REPLACED in place (full regeneration from the whole updated conversation,
old entry fully discarded) -- see process_thread()'s already_added branch and
techsupport_qa_ingest.replace_verified_qa_pair(). If no longer useful or
conclusive, the existing entry is deliberately left as-is rather than
removed, since a changed classification isn't evidence the old answer became
wrong. Replacement relies on the entry's source thread_ts having been
recorded when it was added (techsupport_review_state.json); entries added
before that tracking existed can't be located and are left untouched, logged
as an error for that thread rather than silently skipped.

A fourth, less-frequent piece also runs at the end of every invocation:
techsupport_contextual_reembed.run_if_due() -- a periodic full re-embed of
the TECHSUPPORT_QA_PAIRS table using Voyage's contextual embedding (whole
techsupport_qa_pairs.md as one document, so every entry benefits from
full-document context, not just what existed when it was individually
added). It only actually re-embeds once every TECHSUPPORT_REEMBED_INTERVAL_DAYS
(env/lillisa_server.env, default 7); otherwise it no-ops. Keeping this call
here instead of a separate cron entry keeps all techsupport operations
consolidated into the one nightly script.

A fifth piece, techsupport_review_sync.sync_edited_entries(), runs after every
invocation too: it picks up any markdown entries an expert has since edited
directly (see techsupport_review_sync.py) and syncs the current text into
LanceDB. This is purely optional/best-effort -- there is no gating tied to it.

A sixth piece, github_sync.push_verified_qa_pairs() (see FLAGGED OPEN ITEM in
techsupport_qa_ingest.py), pushes techsupport_qa_pairs.md to a dedicated
private GitHub repo (GITHUB_TOKEN / GITHUB_REPO_URL in
cron/env/github_push.env) -- but only when this run actually added,
enriched, or replaced an entry (counts["added"] or counts["enriched"] or
counts["replaced"]), since that's the only way the markdown file's content
changes. push_verified_qa_pairs() itself
also no-ops if the file turns out to be byte-identical to what's already in
the repo, so this is a double-safe skip against pushing empty commits.

Scheduling: the API process runs this on its own tick, via
LilLisa_Server/src/techsupport_cron.py -- there is no crontab. POST
/run_nightly_pipeline/ forces a run against a live server.

Usage (manual/debugging only; don't run this while the API is up, since the
in-process lock cannot see a second OS process):
    python nightly_pipeline.py
"""

import logging
import os
import time
from typing import Any, Dict, List

import jwt
import requests
from dotenv import dotenv_values
from paths import (
    LILLISA_SERVER_ENV_PATH,
    LILLISA_SERVER_ROOT,
    PACKAGE_ROOT,
    ensure_import_paths,
)

ensure_import_paths()
SCRIPT_DIR = PACKAGE_ROOT
PROJECT_ROOT = LILLISA_SERVER_ROOT

from github_sync import push_verified_qa_pairs  # noqa: E402
from nightly_techsupport_sync import (  # noqa: E402
    load_env,
    load_state,
    paginate_messages,
    save_state,
    sync,
)
from pipeline_summary import format_pipeline_counts  # noqa: E402
from techsupport_classifier import classify_thread  # noqa: E402
from techsupport_contextual_reembed import (  # noqa: E402
    run_if_due as run_reembed_if_due,
)
from techsupport_qa_ingest import (  # noqa: E402
    add_verified_qa_pair,
    enrich_verified_entry,
    get_related_entry_title,
    replace_verified_qa_pair,
)
from techsupport_review_sync import sync_edited_entries  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nightly_pipeline")

SLACK_API_BASE = "https://slack.com/api"
DEFAULT_SYNC_INTERVAL_HOURS = 24
# Same default lil-lisa/app_envfiles/lil-lisa.env's LIL_LISA_SERVER_URL uses --
# this script and the Slack bot normally run alongside the same LilLisa_Server
# instance on the same host. Override via LIL_LISA_SERVER_URL if not.
DEFAULT_LIL_LISA_SERVER_URL = "http://127.0.0.1:8000"


def get_sync_interval_hours() -> float:
    """Expert-adjustable knob: how many hours must elapse between checks of
    the techsupport Slack channel. Overridable per-process via env var
    without editing the env file -- same pattern as
    techsupport_contextual_reembed.get_reembed_interval_days()."""
    env = dict(dotenv_values(str(LILLISA_SERVER_ENV_PATH)))
    env = {**env, **os.environ}
    raw = env.get("TECHSUPPORT_SYNC_INTERVAL_HOURS", str(DEFAULT_SYNC_INTERVAL_HOURS))
    return float(raw)


def reload_techsupport_index() -> Dict[str, Any]:
    """Tell the running LilLisa_Server to rebuild its in-memory QA pairs
    retrievers/indices (including TECHSUPPORT_QA_PAIRS) from the current
    on-disk LanceDB tables, via the /reload_techsupport_qa_pairs/ endpoint.

    add_verified_qa_pair()/replace_verified_qa_pair() above write straight to
    LanceDB, entirely out-of-process from LilLisa_Server -- confirmed live
    (see investigation notes) that a long-running server process's
    TECHSUPPORT_QA_PAIRS_RETRIEVER stays frozen at whatever it was at last
    startup, so newly-added content is invisible to real queries until
    something rebuilds it. Without this call, only a manual server restart
    would pick up tonight's changes.

    Best-effort: mirrors the other post-run steps below (github push, review
    sync, reembed) -- a failure here must not fail the whole pipeline run,
    since the LanceDB write itself already succeeded and is durable; it just
    means the running server won't see it until its next restart or reload."""
    env = dict(dotenv_values(str(LILLISA_SERVER_ENV_PATH)))
    env = {**env, **os.environ}
    authentication_key = env.get("AUTHENTICATION_KEY")
    if not authentication_key:
        return {"reloaded": False, "reason": "AUTHENTICATION_KEY not found in lillisa_server.env"}

    base_url = env.get("LIL_LISA_SERVER_URL", DEFAULT_LIL_LISA_SERVER_URL).rstrip("/")
    encrypted_key = jwt.encode({"some": "payload"}, authentication_key, algorithm="HS256")

    resp = requests.post(
        f"{base_url}/reload_techsupport_qa_pairs/",
        params={"encrypted_key": encrypted_key},
        timeout=30,
    )
    resp.raise_for_status()
    return {"reloaded": True, "response": resp.json()}


def is_channel_check_due(sync_state: Dict[str, Any]) -> bool:
    """True if TECHSUPPORT_SYNC_INTERVAL_HOURS have elapsed since the last
    time the techsupport channel was checked (sync_state's
    last_run_timestamp, maintained by nightly_techsupport_sync.sync())."""
    last = sync_state.get("last_run_timestamp", "0")
    interval_seconds = get_sync_interval_hours() * 3600
    return (time.time() - float(last)) >= interval_seconds


def post_admin_alert(token: str, admin_channel_id: str, text: str) -> None:
    """Best-effort post to the shared admin channel. Alerting failures are
    logged but never allowed to fail the pipeline run itself."""
    if not admin_channel_id:
        logger.warning("ADMIN_CHANNEL_ID not configured -- skipping admin alert:\n%s", text)
        return
    try:
        resp = requests.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": admin_channel_id, "text": text},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("Failed to post admin alert: %s", data.get("error"))
    except requests.RequestException as exc:
        logger.error("Failed to post admin alert: %s", exc)


def process_thread(thread_ts: str, token: str, channel_id: str, already_added: bool) -> str:
    """Fetch, classify, and (if it clears the bar) ingest, enrich, or replace one thread.

    `already_added` is True when this thread previously had
    added_to_verified_db=true and is being reprocessed because sync()
    detected new reply activity since then. In that case a still
    useful+conclusive thread REPLACES its existing verified entry -- full
    regeneration from the complete, current conversation (original question
    plus every reply, including the new one), not a patch of just the new
    detail -- instead of being added as a new (duplicate) entry. If it's no
    longer useful/conclusive, the existing entry is deliberately left as-is:
    there's no reliable signal that the old resolution stopped being correct,
    so nothing is deleted.

    A brand-new (not already_added) thread that was tagged (via
    /tag_techsupport_thread/) as related to an existing verified entry --
    i.e. Lil Lisa answered by citing that entry and the user escalated
    anyway -- gets a LIGHTER bar: only IsUsefulConversation is required, not
    IsConclusiveConversation, since enrichment is adding supplementary
    insight to an already-resolved topic rather than needing to
    independently resolve something from scratch. Untagged threads (the
    normal add path) are unaffected -- both useful AND conclusive are still
    required, exactly as before.

    Returns one of: "added", "enriched", "replaced", "skipped_not_useful",
    "skipped_not_conclusive", "left_as_is_not_useful", "left_as_is_not_conclusive".
    Raises on any failure -- the caller is responsible for catching per-thread
    errors so one bad thread doesn't abort the whole run.
    """
    messages = paginate_messages(
        "conversations.replies", token, {"channel": channel_id, "ts": thread_ts, "limit": 200}
    )
    if not messages:
        raise RuntimeError(f"No messages found for thread {thread_ts}")

    # Enrichment only ever applies to a thread's first pass (already_added is only
    # True for a replace-path reprocessing, which keeps the full bar) -- see
    # docstring above.
    related_entry_title = None if already_added else get_related_entry_title(thread_ts)

    result = classify_thread(messages, slack_token=token, skip_conclusive=bool(related_entry_title))

    if not result["is_useful"]:
        return "left_as_is_not_useful" if already_added else "skipped_not_useful"

    if related_entry_title:
        try:
            enrich_verified_entry(related_entry_title, result["conversation_thread"])
            return "enriched"
        except LookupError as exc:
            logger.warning(
                "Thread %s tagged related_entry_title=%r but enrich failed (%s) -- " "falling back to normal add",
                thread_ts,
                related_entry_title,
                exc,
            )
            # The enrich attempt's lighter bar doesn't carry over to the add fallback --
            # it still needs the full useful+conclusive bar, which wasn't evaluated
            # above (conclusiveness was deliberately skipped). Re-classify with the
            # default (skip_conclusive=False) rather than duplicating the
            # IsConclusiveConversation "yes"/"no" comparison here.
            full_result = classify_thread(messages, slack_token=token)
            if not full_result["is_conclusive"]:
                return "skipped_not_conclusive"
            add_verified_qa_pair(full_result["conversation_thread"], thread_ts=thread_ts)
            return "added"

    if not result["is_conclusive"]:
        return "left_as_is_not_conclusive" if already_added else "skipped_not_conclusive"

    if already_added:
        replace_verified_qa_pair(thread_ts, result["conversation_thread"])
        return "replaced"

    add_verified_qa_pair(result["conversation_thread"], thread_ts=thread_ts)
    return "added"


def run_pipeline() -> Dict[str, Any]:
    slack_env = load_env()
    token = slack_env["SLACK_BOT_TOKEN"]
    channel_id = slack_env["TECHSUPPORT_CHANNEL_ID"]
    admin_channel_id = slack_env.get("ADMIN_CHANNEL_ID")

    sync_state_before = load_state()
    if is_channel_check_due(sync_state_before):
        sync_result = sync()
    else:
        logger.info(
            "Techsupport channel check skipped: TECHSUPPORT_SYNC_INTERVAL_HOURS (%s) not yet elapsed since last check",
            get_sync_interval_hours(),
        )
        sync_result = {"new_thread_ids": [], "updated_thread_ids": []}

    thread_ids = sync_result["new_thread_ids"] + sync_result["updated_thread_ids"]

    # sync() (when it ran) already wrote its own updates (last_seen_reply_ts
    # etc.) to techsupport_sync_state.json, so re-load it fresh here to get
    # those plus any added_to_verified_db flags from previous pipeline runs.
    state = load_state()
    threads_state = state.setdefault("threads", {})

    counts = {
        "checked": 0,
        "added": 0,
        "enriched": 0,
        "replaced": 0,
        "left_as_is_not_useful": 0,
        "left_as_is_not_conclusive": 0,
        "skipped_not_useful": 0,
        "skipped_not_conclusive": 0,
        "errored": 0,
    }
    errors: List[Dict[str, str]] = []

    for thread_ts in thread_ids:
        counts["checked"] += 1

        # A thread only reaches here (i.e. is in thread_ids at all) if it's brand new
        # OR sync() detected new reply activity on it -- an already-added thread with no
        # new activity was never included in new_thread_ids/updated_thread_ids to begin
        # with, so it's implicitly left alone without any check needed here.
        already_added = bool(threads_state.get(thread_ts, {}).get("added_to_verified_db"))

        try:
            outcome = process_thread(thread_ts, token, channel_id, already_added)
            counts[outcome] += 1
            logger.info("Thread %s: %s", thread_ts, outcome)

            if outcome in ("added", "enriched"):
                # Mark and persist immediately (not batched) so a crash on a
                # later thread doesn't lose track of this one having already
                # been added -- a retry must not re-add/re-enrich it. "enriched"
                # is marked the same way as "added" since this thread_ts was never
                # previously added -- without this, an enriched thread with no new
                # activity would be reconsidered as a fresh add candidate forever.
                threads_state.setdefault(thread_ts, {})["added_to_verified_db"] = True
                save_state(state)
        except Exception as exc:  # noqa: BLE001 -- one bad thread must not abort the run
            counts["errored"] += 1
            errors.append({"thread_ts": thread_ts, "error": str(exc)})
            logger.exception("Thread %s: errored", thread_ts)

    try:
        if counts["added"] or counts["enriched"] or counts["replaced"]:
            github_push_result = push_verified_qa_pairs()
            logger.info("GitHub push of verified techsupport QA pairs: %s", github_push_result)
        else:
            github_push_result = {"pushed": False, "reason": "no_changes_this_run"}
    except Exception as exc:  # noqa: BLE001 -- a push failure must not fail the whole pipeline run
        github_push_result = {"pushed": False, "error": str(exc)}
        logger.exception("GitHub push of verified techsupport QA pairs errored")
        post_admin_alert(
            token, admin_channel_id, f"nightly_pipeline.py: GitHub push of verified techsupport QA pairs failed: {exc}"
        )

    try:
        if counts["added"] or counts["enriched"] or counts["replaced"]:
            reload_after_ingest_result = reload_techsupport_index()
            logger.info("LilLisa_Server techsupport index reload (after ingest): %s", reload_after_ingest_result)
            if not reload_after_ingest_result.get("reloaded"):
                post_admin_alert(
                    token,
                    admin_channel_id,
                    f"nightly_pipeline.py: LilLisa_Server techsupport index reload (after ingest) did not run: {reload_after_ingest_result}",
                )
        else:
            reload_after_ingest_result = {"reloaded": False, "reason": "no_changes_this_run"}
    except Exception as exc:  # noqa: BLE001 -- a reload failure must not fail the whole pipeline run
        reload_after_ingest_result = {"reloaded": False, "error": str(exc)}
        logger.exception("LilLisa_Server techsupport index reload (after ingest) errored")
        post_admin_alert(
            token,
            admin_channel_id,
            f"nightly_pipeline.py: LilLisa_Server techsupport index reload (after ingest) failed: {exc}",
        )

    try:
        review_sync_result = sync_edited_entries()
        logger.info("Techsupport review sync: %s", review_sync_result)
    except Exception as exc:  # noqa: BLE001 -- a review-sync failure must not fail the whole pipeline run
        review_sync_result = {"error": str(exc)}
        logger.exception("Techsupport review sync errored")
        post_admin_alert(token, admin_channel_id, f"nightly_pipeline.py: techsupport review sync failed: {exc}")

    try:
        reembed_result = run_reembed_if_due()
        logger.info("Contextual re-embed: %s", reembed_result)
    except Exception as exc:  # noqa: BLE001 -- a reembed failure must not fail the whole pipeline run
        reembed_result = {"ran": False, "error": str(exc)}
        logger.exception("Contextual re-embed errored")
        post_admin_alert(
            token, admin_channel_id, f"nightly_pipeline.py: contextual re-embed of TECHSUPPORT_QA_PAIRS failed: {exc}"
        )

    try:
        if reembed_result.get("ran"):
            # A full re-embed rewrites every row's vector (rebuild_table), same
            # staleness root cause as add/replace above -- the running server's
            # in-memory TECHSUPPORT_QA_PAIRS_RETRIEVER would otherwise keep
            # serving similarity scores computed against the OLD vectors until
            # a manual restart.
            reload_after_reembed_result = reload_techsupport_index()
            logger.info("LilLisa_Server techsupport index reload (after reembed): %s", reload_after_reembed_result)
            if not reload_after_reembed_result.get("reloaded"):
                post_admin_alert(
                    token,
                    admin_channel_id,
                    f"nightly_pipeline.py: LilLisa_Server techsupport index reload (after reembed) did not run: {reload_after_reembed_result}",
                )
        else:
            reload_after_reembed_result = {"reloaded": False, "reason": "reembed_did_not_run"}
    except Exception as exc:  # noqa: BLE001 -- a reload failure must not fail the whole pipeline run
        reload_after_reembed_result = {"reloaded": False, "error": str(exc)}
        logger.exception("LilLisa_Server techsupport index reload (after reembed) errored")
        post_admin_alert(
            token,
            admin_channel_id,
            f"nightly_pipeline.py: LilLisa_Server techsupport index reload (after reembed) failed: {exc}",
        )

    summary = {
        "new_threads": len(sync_result["new_thread_ids"]),
        "updated_threads": len(sync_result["updated_thread_ids"]),
        **counts,
        "errors": errors,
        "github_push": github_push_result,
        "techsupport_index_reload_after_ingest": reload_after_ingest_result,
        "review_sync": review_sync_result,
        "reembed": reembed_result,
        "techsupport_index_reload_after_reembed": reload_after_reembed_result,
    }

    logger.info("Pipeline summary: %s", format_pipeline_counts(counts))

    if errors:
        error_lines = "\n".join(f"- {e['thread_ts']}: {e['error']}" for e in errors)
        # `checked` is already in the sentence; omit it from the parenthetical.
        alert_text = (
            f"nightly_pipeline.py: {counts['errored']} thread(s) errored out of {counts['checked']} checked "
            f"({format_pipeline_counts(counts, omit=('checked',))}).\n{error_lines}"
        )
        post_admin_alert(token, admin_channel_id, alert_text)

    return summary


if __name__ == "__main__":
    result = run_pipeline()
    print(result)
