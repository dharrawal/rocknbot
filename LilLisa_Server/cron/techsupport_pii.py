"""
Obvious-format PII/secret redaction for generated techsupport title/summary text.

Tuned for well-known patterns only. Customer/tenant names, bare hostnames, and
org-specific Jira keys need a real techsupport_qa_pairs.md before we can add
them without false-positives (beads pr42-blockers.2.3).
"""

import re

PII_REDACTED_PLACEHOLDER = "[REDACTED]"

_PII_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PII_SLACK_USER = re.compile(r"<@[UW][A-Z0-9]+>")
_PII_SLACK_CHANNEL = re.compile(r"<#[C][A-Z0-9]+(?:\|[^>]+)?>")
_PII_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PII_PEM_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_PII_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*\S+")
_PII_TICKET = re.compile(r"\b(?:INC|SR|HD|TICKET)[-_]?\d+\b", re.IGNORECASE)
_PII_INTERNAL_FQDN = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+(?:local|internal|lan|corp|intranet)\b",
    re.IGNORECASE,
)
# Private/link-local IPv4 only -- a generic dotted-quad matcher eats product versions like 7.3.1.0.
_PII_PRIVATE_IPV4 = re.compile(
    r"\b(?:"
    r"10(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){3}"
    r"|192\.168(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){2}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){2}"
    r"|127(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){3}"
    r"|169\.254(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d{1,2})){2}"
    r")\b"
)


def redact_obvious_pii(text: str) -> str:
    """Replace well-known PII/secret patterns in generated title/summary text."""
    if not text:
        return text
    redacted = _PII_PEM_PRIVATE_KEY.sub(PII_REDACTED_PLACEHOLDER, text)
    redacted = _PII_AWS_ACCESS_KEY.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_SECRET_ASSIGNMENT.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_EMAIL.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_SLACK_USER.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_SLACK_CHANNEL.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_TICKET.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_INTERNAL_FQDN.sub(PII_REDACTED_PLACEHOLDER, redacted)
    redacted = _PII_PRIVATE_IPV4.sub(PII_REDACTED_PLACEHOLDER, redacted)
    return redacted
