"""Slack-side techsupport & product-channel harvester for lil-lisa.

Owns every Slack interaction of the nightly techsupport pipeline so
LilLisa_Server can stay 100% ignorant of Slack:
  1. Discovers new and updated threads in TECHSUPPORT_CHANNEL_ID and
     CHANNEL_ID_IDA / _IDDM / _IDO via Slack `conversations.history`, gated and
     checkpointed against LilLisa_Server's `GET/POST /techsupport_channel_state/`.
  2. Fetches thread replies (`conversations.replies`), filters out product
     threads with no expert reply locally (`has_expert_reply`) using lil-lisa's
     existing expert group resolver, formats speaker display names (`users.info`)
     and role tags (`(bot)`, `(expert)`), and sends each candidate thread to
     LilLisa_Server's `POST /process_techsupport_thread/`.
  3. Triggers post-run publishing and maintenance on LilLisa_Server via
     `POST /finalize_techsupport_pipeline_run/` and posts any step or thread
     errors to the admin Slack channel.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import jwt
import requests

from expert_group import ExpertLookupError
from utils import logger

SLACK_API_BASE = "https://slack.com/api"
IGNORED_SUBTYPES = {"channel_join", "channel_leave"}
PROCESSING_PLACEHOLDER = "Processing..."
BOT_ROLE_TAG = "(bot)"
ESCALATION_RELAY_PREFIX = "Question posted in "
ESCALATION_RELAY_ROLE_TAG = "(bot, relaying a user's question)"
EXPERT_ROLE_TAG = "(expert)"
DEFAULT_BOT_DISPLAY_NAME = "Lil Lisa"

CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS = 3.0
CHANNEL_NOT_FOUND_MAX_RETRIES = 2
PER_THREAD_THROTTLE_SECONDS = 0.3
DEFAULT_TICK_MINUTES = 60

PRODUCTS: Sequence[str] = ("IDA", "IDDM", "IDO")
CHANNEL_ERROR_PREFIX = "channel "

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

PRODUCT_COUNT_KEYS = (
    "checked",
    "corrected",
    "added",
    "replaced",
    "left_as_is_not_useful",
    "left_as_is_not_conclusive",
    "skipped_no_expert_reply",
    "skipped_no_expert_insight",
    "skipped_not_useful",
    "skipped_not_conclusive",
    "errored",
)

PRODUCT_CHANGE_KEYS = ("corrected", "added", "replaced")

_user_name_cache: Dict[str, str] = {}
_run_lock = threading.Lock()


def is_running() -> bool:
    """True while a scheduled or manual techsupport pipeline run is in flight."""
    return _run_lock.locked()


def new_techsupport_counts() -> Dict[str, int]:
    return {
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


def new_product_counts() -> Dict[str, Any]:
    return {key: 0 for key in PRODUCT_COUNT_KEYS}


def format_pipeline_counts(
    counts: Mapping[str, Any],
    *,
    omit: Optional[Iterable[str]] = None,
) -> str:
    """Render ingest outcome counts as `key=value` pairs for logs and Slack alerts."""
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
    """Sum numeric outcome counts for one admin alert."""
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
    """Format an admin alert body for thread and product-channel failures."""
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


def resolve_admin_channel_id(env: Mapping[str, Any]) -> Optional[str]:
    """Return ADMIN_CHANNEL_ID from lil-lisa.env for pipeline error alerts."""
    val = str(env.get("ADMIN_CHANNEL_ID") or "").strip()
    return val or None


def product_channel_ids(env: Mapping[str, Any]) -> Dict[str, str]:
    """Return {product: channel_id} for configured CHANNEL_ID_<PRODUCT> keys."""
    configured: Dict[str, str] = {}
    for product in PRODUCTS:
        channel_id = str(env.get(f"CHANNEL_ID_{product}") or "").strip()
        if channel_id:
            configured[product] = channel_id
    return configured


def slack_api_call(method: str, token: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Call a Slack Web API GET method with rate-limit and channel_not_found retries."""
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
                method,
                params.get("channel"),
                channel_not_found_attempts,
                CHANNEL_NOT_FOUND_MAX_RETRIES,
                CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS,
            )
            time.sleep(CHANNEL_NOT_FOUND_RETRY_DELAY_SECONDS)
            continue
        if data.get("error") == "channel_not_found":
            logger.error(
                "[RETRY %s] Exhausted retries -- channel_not_found persisted for channel=%r",
                method,
                params.get("channel"),
            )
        raise RuntimeError(f"Slack API error calling {method}: {data.get('error')}")


def paginate_messages(method: str, token: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect the 'messages' list across all pages of a Slack conversations.* method."""
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


def find_new_threads(token: str, channel_id: str, oldest: str, known_thread_ids: Iterable[str]) -> Dict[str, str]:
    """Return {thread_ts: latest_reply_or_ts} for top-level messages posted since `oldest`."""
    known = set(known_thread_ids)
    history = paginate_messages(
        "conversations.history",
        token,
        {"channel": channel_id, "oldest": oldest, "limit": 200},
    )
    discovered: Dict[str, str] = {}
    for msg in history:
        if msg.get("subtype") in IGNORED_SUBTYPES:
            continue
        thread_ts = str(msg["ts"])
        if thread_ts in known:
            continue
        discovered[thread_ts] = str(msg.get("latest_reply") or thread_ts)
    return discovered


def fetch_parent_latest_reply_ts(token: str, channel_id: str, thread_ts: str) -> str:
    """Return Slack's latest_reply on the parent message, or the parent ts."""
    data = slack_api_call(
        "conversations.history",
        token,
        {
            "channel": channel_id,
            "oldest": thread_ts,
            "inclusive": "true",
            "limit": 1,
        },
    )
    messages = data.get("messages") or []
    if not messages:
        raise RuntimeError(f"No parent message found for thread {thread_ts}")
    parent = messages[0]
    return str(parent.get("latest_reply") or parent.get("ts") or thread_ts)


def refresh_known_threads(
    token: str,
    channel_id: str,
    lookup_ids: Sequence[str],
) -> Dict[str, str]:
    """Fetch current latest_reply timestamps for `lookup_ids` on `channel_id`."""
    refreshed: Dict[str, str] = {}
    for index, thread_ts in enumerate(lookup_ids):
        try:
            refreshed[str(thread_ts)] = fetch_parent_latest_reply_ts(token, channel_id, thread_ts)
        except Exception as exc:  # noqa: BLE001 -- one missing parent must not abort the run
            logger.warning("Skipping parent lookup for thread %s: %s", thread_ts, exc)
            continue
        if index < len(lookup_ids) - 1:
            time.sleep(PER_THREAD_THROTTLE_SECONDS)
    return refreshed


def has_expert_reply(messages: Sequence[Dict[str, Any]], thread_ts: str, expert_ids: Iterable[str]) -> bool:
    """True if some message OTHER than the thread parent was posted by an expert."""
    experts = {expert for expert in (expert_ids or ()) if expert}
    if not experts:
        return False
    for index, message in enumerate(messages):
        if index == 0 or message.get("ts") == thread_ts:
            continue
        if message.get("user") in experts:
            return True
    return False


def _resolve_user_name(user_id: str, slack_token: str) -> str:
    """Best-effort lookup of a Slack display name via users.info."""
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        resp = requests.get(
            f"{SLACK_API_BASE}/users.info",
            headers={"Authorization": f"Bearer {slack_token}"},
            params={"user": user_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            profile = data["user"].get("profile", {})
            name = profile.get("display_name") or profile.get("real_name") or data["user"].get("name") or user_id
        else:
            name = user_id
    except (requests.RequestException, KeyError, ValueError):
        name = user_id

    _user_name_cache[user_id] = name
    return name


def _is_bot_message(msg: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
    if msg.get("bot_id"):
        return True
    if bot_user_id and msg.get("user") == bot_user_id:
        return True
    return msg.get("subtype") == "bot_message"


def _bot_display_name(msg: Dict[str, Any], default_name: str) -> str:
    bot_profile = msg.get("bot_profile")
    profile_name = bot_profile.get("name") if isinstance(bot_profile, dict) else None
    return msg.get("username") or profile_name or default_name


def format_thread_messages(
    messages: List[Dict[str, Any]],
    slack_token: Optional[str] = None,
    *,
    bot_user_id: Optional[str] = None,
    expert_user_ids: Optional[Iterable[str]] = None,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
) -> str:
    """Format Slack conversations.replies messages into role-tagged flat text."""
    expert_ids = set(expert_user_ids or ())
    lines = []
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        if msg.get("subtype") in IGNORED_SUBTYPES:
            continue

        ts = msg["ts"]
        text = msg.get("text", "")
        if text.strip() == PROCESSING_PLACEHOLDER:
            continue

        user_id = msg.get("user") or msg.get("bot_id") or "unknown"

        if _is_bot_message(msg, bot_user_id):
            role_tag = ESCALATION_RELAY_ROLE_TAG if text.startswith(ESCALATION_RELAY_PREFIX) else BOT_ROLE_TAG
            display_name = f"{_bot_display_name(msg, bot_display_name)} {role_tag}"
        else:
            if slack_token and msg.get("user"):
                display_name = _resolve_user_name(user_id, slack_token)
            else:
                display_name = user_id
            if msg.get("user") and msg["user"] in expert_ids:
                display_name = f"{display_name} {EXPERT_ROLE_TAG}"

        lines.append(f"[{ts}] {display_name}: {text}")

    return "\n".join(lines)


def post_admin_alert(token: str, admin_channel_id: Optional[str], text: str) -> None:
    """Best-effort post to the admin Slack channel."""
    if not admin_channel_id:
        logger.warning("No admin channel configured -- skipping admin alert:\n%s", text)
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


class BackendPipelineClient:
    """HTTP client for LilLisa_Server's stateless techsupport pipeline endpoints."""

    def __init__(self, base_url: str, encrypted_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.encrypted_key = encrypted_key

    def get_channel_state(
        self,
        channel_id: str,
        *,
        is_product_channel: bool = False,
        product: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "channel_id": channel_id,
            "encrypted_key": self.encrypted_key,
            "is_product_channel": is_product_channel,
            "force": force,
        }
        if product:
            params["product"] = product
        resp = requests.get(f"{self.base_url}/techsupport_channel_state/", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def checkpoint_channel_discovery(
        self,
        channel_id: str,
        *,
        is_product_channel: bool = False,
        scan_timestamp: Optional[str] = None,
        catchup_due: bool = False,
        new_threads: Optional[Mapping[str, str]] = None,
        refreshed_threads: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        resp = requests.post(
            f"{self.base_url}/techsupport_channel_state/",
            params={"encrypted_key": self.encrypted_key},
            json={
                "channel_id": channel_id,
                "is_product_channel": is_product_channel,
                "scan_timestamp": scan_timestamp,
                "catchup_due": catchup_due,
                "new_threads": dict(new_threads or {}),
                "refreshed_threads": dict(refreshed_threads or {}),
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def process_thread(
        self,
        channel_id: str,
        thread_ts: str,
        conversation_thread: str,
        *,
        product: Optional[str] = None,
    ) -> Dict[str, Any]:
        resp = requests.post(
            f"{self.base_url}/process_techsupport_thread/",
            params={"encrypted_key": self.encrypted_key},
            json={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "conversation_thread": conversation_thread,
                "product": product,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    def finalize_run(
        self,
        *,
        changed_this_run: bool,
        run_maintenance: bool = True,
    ) -> Dict[str, Any]:
        resp = requests.post(
            f"{self.base_url}/finalize_techsupport_pipeline_run/",
            params={"encrypted_key": self.encrypted_key},
            json={
                "changed_this_run": changed_this_run,
                "run_maintenance": run_maintenance,
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()


def build_backend_client(env: Mapping[str, Any]) -> BackendPipelineClient:
    base_url = str(env.get("LIL_LISA_SERVER_URL") or "http://127.0.0.1:8000").strip().rstrip("/")
    auth_key = str(env["AUTHENTICATION_KEY"])
    encrypted_key = jwt.encode({"some": "payload"}, auth_key, algorithm="HS256")
    if isinstance(encrypted_key, bytes):
        encrypted_key = encrypted_key.decode("utf-8")
    return BackendPipelineClient(base_url=base_url, encrypted_key=encrypted_key)


def run_techsupport_pipeline(
    env: Mapping[str, Any],
    *,
    resolve_experts: Callable[[str], Sequence[str]],
    backend: Optional[BackendPipelineClient] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the full techsupport + product-channel harvest and backend ingest pass."""
    token = str(env["SLACK_BOT_TOKEN"])
    techsupport_channel_id = str(env.get("TECHSUPPORT_CHANNEL_ID") or "").strip() or None
    admin_channel_id = resolve_admin_channel_id(env)
    bot_user_id = str(env.get("LIL_LISA_SLACK_USERID") or "").strip() or None
    client = backend or build_backend_client(env)

    counts = new_techsupport_counts()
    errors: List[Dict[str, str]] = []
    new_thread_ids: List[str] = []
    updated_thread_ids: List[str] = []

    if techsupport_channel_id:
        plan = client.get_channel_state(
            techsupport_channel_id,
            is_product_channel=False,
            force=force,
        )
        if plan.get("due"):
            discovered_new = find_new_threads(
                token,
                techsupport_channel_id,
                str(plan.get("last_run_timestamp", "0")),
                plan.get("known_thread_ids") or [],
            )
            refreshed = refresh_known_threads(
                token,
                techsupport_channel_id,
                plan.get("lookup_ids") or [],
            )
            checkpoint = client.checkpoint_channel_discovery(
                techsupport_channel_id,
                is_product_channel=False,
                scan_timestamp=plan.get("scan_timestamp"),
                catchup_due=bool(plan.get("catchup_due")),
                new_threads=discovered_new,
                refreshed_threads=refreshed,
            )
            new_thread_ids = list(checkpoint.get("new_thread_ids") or [])
            updated_thread_ids = list(checkpoint.get("updated_thread_ids") or [])
            for thread_ts in checkpoint.get("candidate_thread_ids") or []:
                counts["checked"] += 1
                try:
                    messages = paginate_messages(
                        "conversations.replies",
                        token,
                        {"channel": techsupport_channel_id, "ts": thread_ts, "limit": 200},
                    )
                    if not messages:
                        raise RuntimeError(f"No messages found for thread {thread_ts}")
                    formatted = format_thread_messages(
                        messages,
                        slack_token=token,
                        bot_user_id=bot_user_id,
                    )
                    res = client.process_thread(
                        techsupport_channel_id,
                        thread_ts,
                        formatted,
                    )
                    outcome = str(res["outcome"])
                    counts[outcome] = counts.get(outcome, 0) + 1
                    logger.info("Techsupport thread %s: %s", thread_ts, outcome)
                except Exception as exc:  # noqa: BLE001
                    counts["errored"] += 1
                    errors.append({"thread_ts": str(thread_ts), "error": str(exc)})
                    logger.exception("Techsupport thread %s: errored", thread_ts)
        else:
            logger.info("Techsupport channel %s check skipped (%s)", techsupport_channel_id, plan.get("reason"))
    else:
        logger.warning("TECHSUPPORT_CHANNEL_ID not configured -- skipping techsupport channel pass")

    # Product-channel expert-correction pass
    product_totals = new_product_counts()
    products_summary: Dict[str, Any] = {}
    configured_products = product_channel_ids(env)
    product_scan_enabled = True

    for product, channel_id in configured_products.items():
        p_counts = new_product_counts()
        p_counts["channel_id"] = channel_id
        products_summary[product] = p_counts

        try:
            expert_ids = list(resolve_experts(product))
        except (ValueError, ExpertLookupError) as exc:
            p_counts["errored"] += 1
            errors.append({"thread_ts": f"{CHANNEL_ERROR_PREFIX}{channel_id}", "error": f"[{product}] {exc}"})
            logger.exception("Could not resolve experts for %s (channel %s)", product, channel_id)
            continue

        plan = client.get_channel_state(
            channel_id,
            is_product_channel=True,
            product=product,
            force=force,
        )
        if not plan.get("due"):
            reason = plan.get("reason")
            if reason == "product_scan_disabled":
                product_scan_enabled = False
                products_summary.pop(product, None)
                break
            p_counts["skipped_reason"] = reason
            logger.info("Product channel %s (%s) check skipped (%s)", channel_id, product, reason)
            continue

        discovered_new = find_new_threads(
            token,
            channel_id,
            str(plan.get("last_run_timestamp", "0")),
            plan.get("known_thread_ids") or [],
        )
        refreshed = refresh_known_threads(
            token,
            channel_id,
            plan.get("lookup_ids") or [],
        )
        checkpoint = client.checkpoint_channel_discovery(
            channel_id,
            is_product_channel=True,
            scan_timestamp=plan.get("scan_timestamp"),
            catchup_due=bool(plan.get("catchup_due")),
            new_threads=discovered_new,
            refreshed_threads=refreshed,
        )
        for thread_ts in checkpoint.get("candidate_thread_ids") or []:
            p_counts["checked"] += 1
            try:
                messages = paginate_messages(
                    "conversations.replies",
                    token,
                    {"channel": channel_id, "ts": thread_ts, "limit": 200},
                )
                if not messages:
                    raise RuntimeError(f"No messages found for thread {thread_ts}")
                if not has_expert_reply(messages, thread_ts, expert_ids):
                    p_counts["skipped_no_expert_reply"] += 1
                    continue
                formatted = format_thread_messages(
                    messages,
                    slack_token=token,
                    bot_user_id=bot_user_id,
                    expert_user_ids=expert_ids,
                )
                res = client.process_thread(
                    channel_id,
                    thread_ts,
                    formatted,
                    product=product,
                )
                outcome = str(res["outcome"])
                p_counts[outcome] = p_counts.get(outcome, 0) + 1
                logger.info("Product %s thread %s: %s", product, thread_ts, outcome)
            except Exception as exc:  # noqa: BLE001
                p_counts["errored"] += 1
                errors.append({"thread_ts": str(thread_ts), "error": f"[{product}] {exc}"})
                logger.exception("Product %s thread %s: errored", product, thread_ts)

    for key in PRODUCT_COUNT_KEYS:
        product_totals[key] = sum(int(c.get(key, 0)) for c in products_summary.values())

    product_result = {
        "enabled": product_scan_enabled,
        "products": products_summary,
        "totals": product_totals,
    }

    changed_this_run = bool(
        counts["added"]
        or counts["enriched"]
        or counts["replaced"]
        or any(product_totals.get(key) for key in PRODUCT_CHANGE_KEYS)
    )

    try:
        finalize_result = client.finalize_run(changed_this_run=changed_this_run, run_maintenance=True)
    except Exception as exc:  # noqa: BLE001
        finalize_result = {"step_errors": [f"nightly_pipeline.py: finalize_techsupport_pipeline_run failed: {exc}"]}
        logger.exception("Failed to finalize techsupport pipeline run")

    for step_error in finalize_result.get("step_errors") or []:
        post_admin_alert(token, admin_channel_id, str(step_error))

    logger.info("Pipeline summary: %s", format_pipeline_counts(counts))
    if product_result.get("enabled"):
        logger.info("Product-channel summary: %s", format_pipeline_counts(product_totals))

    if errors:
        alert_text = format_pipeline_error_alert(
            "nightly_pipeline.py",
            errors,
            counts,
            product_totals,
        )
        post_admin_alert(token, admin_channel_id, alert_text)

    return {
        "new_threads": len(new_thread_ids),
        "updated_threads": len(updated_thread_ids),
        **counts,
        "product_channels": product_result,
        "errors": errors,
        **finalize_result,
    }


def tick_minutes_from_env(env: Mapping[str, Any]) -> int:
    raw = str(env.get("TECHSUPPORT_PIPELINE_TICK_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_TICK_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        logger.warning(
            "TECHSUPPORT_PIPELINE_TICK_MINUTES=%r is not an integer -- using %s", raw, DEFAULT_TICK_MINUTES
        )
        return DEFAULT_TICK_MINUTES
    if minutes <= 0:
        logger.warning(
            "TECHSUPPORT_PIPELINE_TICK_MINUTES=%s must be positive -- using %s", minutes, DEFAULT_TICK_MINUTES
        )
        return DEFAULT_TICK_MINUTES
    return minutes


def run_once(
    env: Mapping[str, Any],
    *,
    resolve_experts: Callable[[str], Sequence[str]],
    backend: Optional[BackendPipelineClient] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the pipeline unless one is already in flight in this process."""
    if not _run_lock.acquire(blocking=False):
        logger.info("Nightly techsupport pipeline already running -- ignoring this trigger")
        return {"ran": False, "reason": "already_running"}

    try:
        logger.info("Nightly techsupport pipeline starting (force=%s)", force)
        summary = run_techsupport_pipeline(
            env,
            resolve_experts=resolve_experts,
            backend=backend,
            force=force,
        )
        logger.info("Nightly techsupport pipeline finished: %s", summary)
        return {"ran": True, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        logger.critical("Nightly techsupport pipeline failed: %s", exc, exc_info=True)
        return {"ran": False, "error": str(exc)}
    finally:
        _run_lock.release()


async def start_scheduler_loop(
    env: Mapping[str, Any],
    *,
    resolve_experts: Callable[[str], Sequence[str]],
) -> None:
    """Periodic asyncio loop that runs the harvester off the event loop."""
    interval_seconds = tick_minutes_from_env(env) * 60
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(
            run_once,
            env,
            resolve_experts=resolve_experts,
            force=False,
        )
