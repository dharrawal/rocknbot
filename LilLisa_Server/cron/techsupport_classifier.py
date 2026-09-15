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
from paths import (
    LILLISA_SERVER_ENV_PATH,
    LILLISA_SERVER_ROOT,
    PACKAGE_ROOT,
    ensure_import_paths,
)

ensure_import_paths()
SCRIPT_DIR = PACKAGE_ROOT
PROJECT_ROOT = LILLISA_SERVER_ROOT

SLACK_API_BASE = "https://slack.com/api"
IGNORED_SUBTYPES = {"channel_join", "channel_leave"}
# Placeholder the Slack bot posts while it works on an answer; it carries no
# content, so it is dropped before the thread text reaches any prompt.
PROCESSING_PLACEHOLDER = "Processing..."
BOT_ROLE_TAG = "(bot)"
# The escalate button posts the user's question into the techsupport channel
# under the bot's identity (see lil-lisa/src/slack.py, techsupport_fallback_text:
# "Question posted in <channel>. <question>"). That post is a relayed human
# question, not an AI answer, so it gets its own tag rather than "(bot)".
ESCALATION_RELAY_PREFIX = "Question posted in "
ESCALATION_RELAY_ROLE_TAG = "(bot, relaying a user's question)"
EXPERT_ROLE_TAG = "(expert)"
DEFAULT_BOT_DISPLAY_NAME = "Lil Lisa"


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
            "off-topic, or noise. "
            "Speakers tagged '(bot)' are AI-generated and unverified, while a later message from a speaker "
            "tagged '(expert)' is authoritative and supersedes the bot's content."
        )
    )


class IsConclusiveConversation(dspy.Signature):
    conversation_thread: str = dspy.InputField()
    is_conclusive: Literal["yes", "no"] = dspy.OutputField(
        desc=(
            "Respond 'yes' if the conversation reaches any technical solution, answer, or resolved state. "
            "Respond 'no' if the conversation is left open-ended, unresolved, or inconclusive even if it is "
            "technical. "
            "Speakers tagged '(bot)' are AI-generated and unverified, while a later message from a speaker "
            "tagged '(expert)' is authoritative and supersedes the bot's content."
        )
    )


class HasExpertInsight(dspy.Signature):
    conversation_thread: str = dspy.InputField()
    has_expert_insight: Literal["yes", "no"] = dspy.OutputField(
        desc=(
            "Respond 'yes' if any message from a speaker tagged '(expert)' corrects, confirms, or adds "
            "technical insight to the answer given by the speaker tagged '(bot)' or to the topic of the "
            "thread: a correction, a confirmation that the answer worked, an additional fix, a caveat, or "
            "any other detail that makes the resolution more complete or more accurate. "
            "Respond 'no' if the expert messages only ask questions, request clarification or more "
            "information, or are social or off-topic. An expert asking a question is not insight, even "
            "when the question is technical. "
            "The first message in the thread is the thread parent and is never insight by itself, so an "
            "expert who merely opened the thread does not count."
        )
    )


check_useful = dspy.Predict(IsUsefulConversation)
check_conclusive = dspy.Predict(IsConclusiveConversation)
check_expert_insight = dspy.Predict(HasExpertInsight)


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


def _is_bot_message(msg: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
    """True if this Slack message was posted by a bot rather than a human.

    Slack marks bot posts inconsistently depending on how they were sent, so
    all three signals are accepted: a bot_id field, the configured bot's own
    user id, or the bot_message subtype.
    """
    if msg.get("bot_id"):
        return True
    if bot_user_id and msg.get("user") == bot_user_id:
        return True
    return msg.get("subtype") == "bot_message"


def _bot_display_name(msg: Dict[str, Any], default_name: str) -> str:
    """Prefer the name Slack attached to the bot post, falling back to the
    configured default (the bot's product-facing name)."""
    bot_profile = msg.get("bot_profile")
    profile_name = bot_profile.get("name") if isinstance(bot_profile, dict) else None
    return msg.get("username") or profile_name or default_name


def format_thread_messages(
    messages: List[Dict[str, Any]],
    slack_token: Optional[str] = None,
    *,
    bot_user_id: Optional[str] = None,
    expert_user_ids: Optional[Any] = None,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
) -> str:
    """Turn a list of Slack message objects (as returned by
    conversations.replies, and consumed by nightly_techsupport_sync.py) into
    a flat text block, one line per message: "[{ts}] {speaker}: {text}".

    Speakers carry a role tag so downstream prompts can tell an AI answer from
    a human correction:
      * bot turns render as "Lil Lisa (bot)" (see _is_bot_message for how a
        bot turn is detected),
      * a message whose user id is in `expert_user_ids` renders as
        "{display_name} (expert)",
      * everyone else keeps their plain display name.

    Bot turns are kept: they are the context for what an expert corrected. The
    bot's contentless "Processing..." placeholders are dropped.

    `expert_user_ids` is supplied by the caller (the Slack user-group lookup
    lives with the caller, not here) and may be any iterable of user ids.

    If slack_token is given, user IDs are resolved to display names via
    users.info (best-effort, cached). Otherwise raw user/bot IDs are used --
    resolving without a token is a follow-up, not required here.
    """
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


# --- Classification ---


def is_yes_answer(value: Any) -> bool:
    """Return True iff a classifier yes/no field is yes after light normalization.

    Strip surrounding whitespace, casefold, then strip trailing '.' / '!' so
    values like "Yes", "YES", and "yes." count as yes. Anything else
    (including "no", "unknown", empty, or unexpected punctuation) is False
    so the thread is not ingested.
    """
    if value is None:
        return False
    text = str(value).strip().casefold().rstrip(".!")
    return text == "yes"


def has_expert_insight(conversation_thread: str) -> bool:
    """True if an expert message in this thread corrects, confirms, or adds
    technical insight, rather than only asking questions or chatting.

    `conversation_thread` is the role-tagged text format_thread_messages()
    produces; the '(expert)' and '(bot)' tags are what the prompt reads. Used
    by nightly_pipeline's product-channel pass as the gate after the cheap
    has_expert_reply() check and before any routing, so an expert's own
    question never rewrites a verified entry. Zero-shot and unoptimized, same
    caveat as the classifiers above.
    """
    configure_dspy_lm()
    result = check_expert_insight(conversation_thread=conversation_thread)
    return is_yes_answer(result.has_expert_insight)


def classify_thread(
    messages: List[Dict[str, Any]],
    slack_token: Optional[str] = None,
    skip_conclusive: bool = False,
    bot_user_id: Optional[str] = None,
    expert_user_ids: Optional[Any] = None,
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

    `bot_user_id` / `expert_user_ids` are forwarded to format_thread_messages
    so bot and expert turns are role-tagged in the text the classifiers see.
    Both are optional: omitting them reproduces the previous plain formatting.
    """
    configure_dspy_lm()
    conversation_thread = format_thread_messages(
        messages,
        slack_token=slack_token,
        bot_user_id=bot_user_id,
        expert_user_ids=expert_user_ids,
    )

    useful_result = check_useful(conversation_thread=conversation_thread)
    is_useful = is_yes_answer(useful_result.is_useful)

    is_conclusive: Optional[bool] = None
    if is_useful and not skip_conclusive:
        conclusive_result = check_conclusive(conversation_thread=conversation_thread)
        is_conclusive = is_yes_answer(conclusive_result.is_conclusive)

    return {
        "is_useful": is_useful,
        "is_conclusive": is_conclusive,
        "conversation_thread": conversation_thread,
    }
