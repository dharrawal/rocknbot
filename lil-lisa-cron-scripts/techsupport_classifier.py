"""
techsupport_classifier.py
====================================
Standalone script (no dependency on LilLisa_Server's runtime code, same
philosophy as nightly_techsupport_sync.py) that classifies a techsupport
Slack thread as "useful" (contains technical content) and, if useful,
whether it reached a conclusive resolution.

Ports the IsUsefulConversation / IsConclusiveConversation DSPy signatures
from last summer's extracting-conversations project (the final, most-evolved
versions in extracting_conversations.py, not the weaker duplicate in that
project's dspy_optimizer.py), adapted to run against real Slack thread JSON
(conversations.replies output) instead of a hand-parsed raw chat log export.

*** IMPORTANT CAVEAT ***
is_useful and is_conclusive currently come from an unoptimized, zero-shot
DSPy Predict call -- there is no few-shot tuning, no BootstrapFewShot/MIPRO
optimization, and no labeled validation set behind these prompts yet. Unlike
last year's version (which was checked against ~5 years of real labeled
conversation data), these classifications have not been validated against
any ground truth. Treat outputs as a rough first pass for manual review,
not a reliable signal, until they've been checked against real threads.

Required env vars (read from ../env/lillisa_server.env, same file the main
server reads):
    LLM_MODEL            - litellm-style model string (e.g. mistral/mistral-small-latest)
    LLM_API_KEY_FILEPATH - path (relative to the LilLisa_Server project root) to a
                            file containing the API key for LLM_MODEL's provider

Usage (as a library):
    from techsupport_classifier import classify_thread
    result = classify_thread(messages)  # messages = conversations.replies() ["messages"]
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import dspy
import requests
from dotenv import dotenv_values

from paths import LILLISA_SERVER_ENV_PATH, LILLISA_SERVER_ROOT, PACKAGE_ROOT, ensure_import_paths

ensure_import_paths()
SCRIPT_DIR = PACKAGE_ROOT
PROJECT_ROOT = LILLISA_SERVER_ROOT

SLACK_API_BASE = "https://slack.com/api"
IGNORED_SUBTYPES = {"channel_join", "channel_leave"}


def load_llm_env() -> Dict[str, str]:
    """Load LLM_MODEL / LLM_API_KEY_FILEPATH the same way the rest of
    LilLisa_Server does, without importing src.utils (which pulls in torch,
    llama-index, lancedb, etc. that this standalone script doesn't need)."""
    env = dict(dotenv_values(str(LILLISA_SERVER_ENV_PATH)))
    env = {**env, **os.environ}

    if not env.get("LLM_MODEL"):
        raise RuntimeError(f"LLM_MODEL not found in {LILLISA_SERVER_ENV_PATH}")
    if not env.get("LLM_API_KEY_FILEPATH"):
        raise RuntimeError(f"LLM_API_KEY_FILEPATH not found in {LILLISA_SERVER_ENV_PATH}")
    return env


_dspy_configured = False


def configure_dspy_lm() -> None:
    """Point dspy.LM at the same litellm-backed model the rest of the project
    already uses -- no new LLM provider needed, since dspy.LM uses litellm
    internally and litellm is already a pinned dependency.

    Lazy: only runs on the first classify/summarize call so importing this
    module (or nightly_pipeline) does not require LLM key files.
    """
    global _dspy_configured
    if _dspy_configured:
        return
    env = load_llm_env()
    llm_model = env["LLM_MODEL"]

    api_key_filepath = Path(env["LLM_API_KEY_FILEPATH"])
    if not api_key_filepath.is_absolute():
        api_key_filepath = PROJECT_ROOT / api_key_filepath
    if not api_key_filepath.exists():
        raise FileNotFoundError(f"LLM API key file not found: {api_key_filepath}")
    api_key = api_key_filepath.read_text(encoding="utf-8").strip()

    lm = dspy.LM(model=llm_model, api_key=api_key)
    dspy.configure(lm=lm)
    _dspy_configured = True


# --- DSPy signatures, ported verbatim from the final version in
# extracting-conversations/extracting_conversations.py (not the weaker
# duplicate IsUsefulConversation in that project's dspy_optimizer.py) ---


class IsUsefulConversation(dspy.Signature):
    conversation_thread: str = dspy.InputField()
    is_useful: Literal["yes", "no"] = dspy.OutputField(
        desc=(
            "Respond 'yes' if the conversation contains any technical content such as problem descriptions, "
            "debugging attempts, clarifications, architectural discussions, configurations, tools, commands, "
            "solutions, or reasoning around implementation decisions. "
            "Respond 'yes' even if only part of the conversation is useful. "
            "Respond 'no' only if the conversation contains no technical discussion or is entirely social, "
            "off-topic, or noise."
        )
    )


class IsConclusiveConversation(dspy.Signature):
    conversation_thread: str = dspy.InputField()
    is_conclusive: Literal["yes", "no"] = dspy.OutputField(
        desc=(
            "Respond 'yes' if the conversation reaches any technical solution, answer, or resolved state. "
            "Respond 'no' if the conversation is left open-ended, unresolved, or inconclusive even if it is "
            "technical."
        )
    )


check_useful = dspy.Predict(IsUsefulConversation)
check_conclusive = dspy.Predict(IsConclusiveConversation)


# --- Slack JSON -> flat text formatting ---

_user_name_cache: Dict[str, str] = {}


def _resolve_user_name(user_id: str, slack_token: str) -> str:
    """Best-effort lookup of a Slack display name via users.info. Falls back
    to the raw user ID on any failure (missing token, rate limit, revoked
    user, etc.) -- resolution is a nice-to-have for readability, not required
    for classification to work."""
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


def format_thread_messages(messages: List[Dict[str, Any]], slack_token: Optional[str] = None) -> str:
    """Turn a list of Slack message objects (as returned by
    conversations.replies, and consumed by nightly_techsupport_sync.py) into
    a flat text block, one line per message: "[{ts}] {user}: {text}".

    If slack_token is given, user IDs are resolved to display names via
    users.info (best-effort, cached). Otherwise raw user/bot IDs are used --
    resolving without a token is a follow-up, not required here.
    """
    lines = []
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        if msg.get("subtype") in IGNORED_SUBTYPES:
            continue

        ts = msg["ts"]
        text = msg.get("text", "")
        user_id = msg.get("user") or msg.get("bot_id") or "unknown"

        if slack_token and msg.get("user"):
            display_name = _resolve_user_name(user_id, slack_token)
        else:
            display_name = user_id

        lines.append(f"[{ts}] {display_name}: {text}")

    return "\n".join(lines)


# --- Classification ---


def classify_thread(
    messages: List[Dict[str, Any]],
    slack_token: Optional[str] = None,
    skip_conclusive: bool = False,
) -> Dict[str, Any]:
    """Classify a single techsupport thread (Slack message JSON, e.g. the
    output of conversations.replies) as useful / conclusive.

    IsConclusiveConversation is only evaluated when IsUsefulConversation says
    "yes", mirroring the gating in the original extracting-conversations
    pipeline. See module docstring for the accuracy caveat -- both classifiers
    are unoptimized zero-shot prompts with no validation data behind them yet.

    `skip_conclusive`, when True, skips the IsConclusiveConversation call
    entirely (`is_conclusive` comes back None) even if the thread is useful.
    Used by nightly_pipeline.py's enrichment path: a thread tagged as related
    to an existing verified entry only needs to clear the (lighter) usefulness
    bar to enrich that entry, since it's adding supplementary insight to an
    already-resolved topic rather than needing to independently resolve
    something from scratch.
    """
    configure_dspy_lm()
    conversation_thread = format_thread_messages(messages, slack_token=slack_token)

    useful_result = check_useful(conversation_thread=conversation_thread)
    is_useful = useful_result.is_useful == "yes"

    is_conclusive: Optional[bool] = None
    if is_useful and not skip_conclusive:
        conclusive_result = check_conclusive(conversation_thread=conversation_thread)
        is_conclusive = conclusive_result.is_conclusive == "yes"

    return {
        "is_useful": is_useful,
        "is_conclusive": is_conclusive,
        "conversation_thread": conversation_thread,
    }
