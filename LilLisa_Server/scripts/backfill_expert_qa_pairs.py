#!/usr/bin/env python3
"""
backfill_expert_qa_pairs.py
====================================
One-off recovery for expert thumbs-up QA pairs added *before* the endpoint
started pushing to the golden QA pairs repo (pr42-w1x.5).

Until that fix, POST /add_expert_qa_pair/ only wrote to the {PRODUCT}_QA_PAIRS
LanceDB table, and every /update_golden_qa_pairs/ rebuild dropped that table
and rebuilt it from the repo -- so those pairs are gone from LanceDB. They do
survive in the server log, which records each one as:

    ... Expert QA Verification: {"timestamp": ..., "product": "IDDM", ...}

This script mines those lines, drops duplicates, skips pairs already in the
repo, and appends the rest through
src.golden_qa_sync.append_expert_qa_pair_to_repo().

Usage (from LilLisa_Server):
    PYTHONPATH=. .venv/bin/python scripts/backfill_expert_qa_pairs.py \\
        --log-file /var/log/lillisa/server.log --dry-run
    PYTHONPATH=. .venv/bin/python scripts/backfill_expert_qa_pairs.py \\
        --log-file /var/log/lillisa/server.log

Each appended pair is one commit+push, so re-running after a partial failure
is safe: pairs already in the repo are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SERVER_ROOT = Path(__file__).resolve().parent.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from src import golden_qa_sync  # noqa: E402

LOG_MARKER = "Expert QA Verification:"


def parse_log_lines(lines: Iterable[str]) -> List[Tuple[str, str, str]]:
    """Extract (product, question, answer) triples from server log lines.

    Keeps first-seen order and drops exact duplicates -- the same pair can be
    verified more than once (an expert re-clicking, a retried request)."""
    seen = set()
    pairs: List[Tuple[str, str, str]] = []
    for line in lines:
        index = line.find(LOG_MARKER)
        if index < 0:
            continue
        payload = line[index + len(LOG_MARKER):].strip()
        try:
            entry = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        product = (entry.get("product") or "").strip()
        question = (entry.get("question") or "").strip()
        answer = (entry.get("answer") or "").strip()
        if not product or not question or not answer:
            continue
        key = (product, question, answer)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def parse_log_file(log_path: str) -> List[Tuple[str, str, str]]:
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_log_lines(handle)


def backfill(
    log_path: str,
    dry_run: bool = False,
    products: Optional[Sequence[str]] = None,
    repo_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Append every log-mined pair that is not already in the repo.

    Returns a summary dict; also the return value the tests assert on."""
    pairs = parse_log_file(log_path)
    if products:
        wanted = {p.upper() for p in products}
        pairs = [p for p in pairs if p[0].upper() in wanted]

    summary: Dict[str, Any] = {
        "found": len(pairs),
        "skipped_existing": 0,
        "pushed": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "would_push": [],
        "errors": [],
    }

    # One clone per product to learn what is already there; each subsequent
    # append is checked against this set plus whatever we just added.
    existing: Dict[str, set] = {}
    for product, question, answer in pairs:
        if product not in existing:
            try:
                existing[product] = set(
                    golden_qa_sync.read_repo_qa_pairs(product, repo_url=repo_url, token=token)
                )
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"{product}: could not read repo: {exc}")
                existing[product] = set()
                continue

        if (question, answer) in existing[product]:
            summary["skipped_existing"] += 1
            continue

        if dry_run:
            summary["would_push"].append({"product": product, "question": question, "answer": answer})
            # Treat it as added so a repeated pair is only reported once.
            existing[product].add((question, answer))
            continue

        result = golden_qa_sync.append_expert_qa_pair_to_repo(
            product, question, answer, repo_url=repo_url, token=token
        )
        if result.get("pushed"):
            summary["pushed"] += 1
            existing[product].add((question, answer))
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{product}: {result.get('error')}")

    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-file", required=True, help="Server log file to mine for 'Expert QA Verification' lines")
    parser.add_argument("--product", action="append", dest="products", help="Only this product (repeatable), e.g. IDDM")
    parser.add_argument("--repo-url", default=None, help="Override QA_PAIRS_GITHUB_REPO_URL (mainly for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be pushed; change nothing")
    args = parser.parse_args(argv)

    summary = backfill(args.log_file, dry_run=args.dry_run, products=args.products, repo_url=args.repo_url)
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
