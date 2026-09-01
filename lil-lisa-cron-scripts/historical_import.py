"""
historical_import.py
====================================
One-time bulk bootstrap: converts last year's pre-filtered techsupport
conversation summaries (data/historical_import/summaries/ -- 36 text files,
808 "=== Conversation N Summary ===" entries, already filtered for "useful
and conclusive" by a prior year's process) into the same Question/Answer
markdown format techsupport_qa_ingest.py produces, and appends them to the
SAME shared techsupport_qa_pairs.md file the nightly pipeline already uses.

Differs from techsupport_qa_ingest.py's add_verified_qa_pair() in two ways:

  1. Input is a prose SUMMARY of a conversation, not the raw thread -- so the
     extraction signature (ExtractQAFromSummary below) has to reverse-engineer
     a plausible question from the summary's content rather than restate one
     already asked verbatim, and has to decide whether the summary actually
     contains a resolution at all. Some summaries describe only a question
     that was asked with no reply/resolution ever recorded (e.g. "a user
     asked if anyone has a RHEL cluster..." with nothing else) -- those are
     skipped rather than fed through to produce a fabricated answer.

  2. This is a large one-time bulk load (808 entries), not a single
     incremental thread, so it deliberately does NOT use
     techsupport_qa_ingest.insert_qa_pair_into_lancedb's one-row LanceDB
     insert pattern. Instead, once all entries for this run are appended to
     the markdown file, it triggers
     techsupport_contextual_reembed.run_reembed() (an unconditional full
     re-embed, not the interval-gated run_if_due()) exactly once, so old
     entries and all new historical entries get embedded together with full
     document-level context -- what contextual/late-chunking embedding is
     for, and wasteful to do 808 times instead of once.

Resumable / idempotent: every parsed entry gets a stable id
"<source_file>::<conversation_number>", and its outcome (converted / skipped
/ error) is recorded in historical_import_state.json (this directory).
Re-running only (re-)processes entries not already in a *terminal* state --
"converted" and "skipped" are terminal; "error" is not, so a transient
failure gets retried on the next run. This means a crash partway through the
808 entries never reprocesses or duplicates earlier successful conversions.

There is a small, unavoidable crash window per entry: the markdown append and
the state-file save that marks the entry "converted" are two separate writes,
not one atomic transaction. If the process is killed in between, re-running
will reprocess that one entry and append it a second time. This matches the
rigor level of techsupport_qa_ingest.add_verified_qa_pair() (its
markdown-append / lancedb-insert / state-save sequence has the same kind of
gap) -- acceptable for a one-time bulk job where the window is milliseconds
and the file can be hand-deduped if it's ever actually hit.

Required env vars: same as techsupport_qa_ingest.py / techsupport_classifier.py
(read from ../env/lillisa_server.env) -- LLM_MODEL, LLM_API_KEY_FILEPATH,
LANCEDB_FOLDERPATH, VOYAGE_API_KEY_FILEPATH.

Usage:
    # Dry run: parse + extract only, print results, touch nothing on disk.
    python historical_import.py --dry-run --limit 10 --files summary_part_1.txt

    # Real run against everything not yet processed, resumable:
    python historical_import.py

    # Real run without the final reembed (e.g. to split "convert" and
    # "reembed" into separate invocations):
    python historical_import.py --skip-reembed
    python historical_import.py --reembed-only
"""

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import dspy

from paths import LILLISA_SERVER_ROOT, PACKAGE_ROOT, ensure_import_paths

ensure_import_paths()
SCRIPT_DIR = PACKAGE_ROOT
PROJECT_ROOT = LILLISA_SERVER_ROOT
SUMMARIES_DIR = PROJECT_ROOT / "data" / "historical_import" / "summaries"
STATE_PATH = SCRIPT_DIR / "historical_import_state.json"

from techsupport_classifier import configure_dspy_lm  # noqa: E402
from techsupport_qa_ingest import append_summary_to_markdown  # noqa: E402
import techsupport_contextual_reembed  # noqa: E402

# *** RETIRED / NEEDS REWORK BEFORE REUSE ***
# This run already completed (all 808 entries are in a terminal state in
# historical_import_state.json) and its Q&A-format output has since been
# reverted to prose by historical_revert_to_prose.py -- see
# techsupport_qa_ingest.py's module docstring for the current title+summary
# format. append_qa_pair_to_markdown() no longer exists (renamed to
# append_summary_to_markdown(), which takes (title, summary) not
# (question, answer)); the call in run() below now passes the extracted
# question/answer through as a rough (title, summary) stand-in purely so this
# script doesn't crash on import -- it has NOT been redesigned to generate a
# proper short title the way techsupport_qa_ingest.generate_title() /
# historical_revert_to_prose.py do. Before reusing this script for the
# deferred 1-year production data import, rework ExtractQAFromSummary below
# into a plain prose-summary signature (no question/answer split, matching
# techsupport_qa_ingest.SummarizeConversationThread) plus a real title-generation
# call, following historical_revert_to_prose.py's pattern.


# --- Parsing: 36 files -> flat, ordered list of individual summary entries ---

_HEADER_PATTERN = re.compile(
    r"=== Conversation (\d+) Summary ===\n(.*?)(?=\n=== Conversation \d+ Summary ===|\Z)",
    re.DOTALL,
)
_PART_NUMBER_PATTERN = re.compile(r"(\d+)")


def _part_number(filename: str) -> int:
    """Natural sort key so summary_part_2.txt sorts before summary_part_10.txt."""
    match = _PART_NUMBER_PATTERN.search(filename)
    return int(match[1]) if match else 0


def parse_summary_file(path: Path) -> List[Dict[str, Any]]:
    """One file -> list of {"conversation_number", "text"} dicts, in file order."""
    text = path.read_text(encoding="utf-8")
    entries = []
    for match in _HEADER_PATTERN.finditer(text):
        conversation_number = int(match[1])
        summary_text = html.unescape(match[2].strip())
        if summary_text:
            entries.append({"conversation_number": conversation_number, "text": summary_text})
    return entries


def parse_all_summaries(summaries_dir: Path, only_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """All 36 (or a --files-restricted subset) files' entries, flattened into
    one ordered list, each tagged with its source_file and a stable
    "<source_file>::<conversation_number>" id used for state tracking."""
    filenames = sorted((f.name for f in summaries_dir.glob("*.txt")), key=_part_number)
    if only_files:
        wanted = set(only_files)
        filenames = [f for f in filenames if f in wanted]

    all_entries = []
    for filename in filenames:
        for entry in parse_summary_file(summaries_dir / filename):
            entry["source_file"] = filename
            entry["id"] = f"{filename}::{entry['conversation_number']}"
            all_entries.append(entry)
    return all_entries


# --- DSPy signature: prose summary -> self-contained Q&A pair, or "skip" ---


class ExtractQAFromSummary(dspy.Signature):
    """Given a prose summary of a past technical support conversation,
    reverse-engineer the question a user likely asked and extract the
    resolution reached, if any. Some summaries describe only a question or
    request with no reply, follow-up, or resolution ever recorded -- for
    those, there is no real answer to extract."""

    conversation_summary: str = dspy.InputField()
    has_resolution: str = dspy.OutputField(
        desc=(
            "'no' ONLY if the summary contains no real technical/informational content at all -- just a bare "
            "question or request with nothing else recorded (e.g. 'a user asked if anyone has X' with no "
            "elaboration, or the summary explicitly states no solution or outcome was discussed). 'yes' if the "
            "summary contains ANY substantive technical content usable as an answer -- this includes an "
            "explicit fix/resolution, but ALSO a step-by-step process or mechanism description (e.g. 'when "
            "command X is run, the system does A, then B, then C' -- even with no explicit user problem stated, "
            "this counts as 'yes' because it answers the reverse-engineered question 'how does X work / what "
            "happens when X runs?'), a recommendation, a clarification, or factual technical detail. Do NOT "
            "require the summary to be framed as 'problem then fix' -- pure explanations of how something works "
            "are still substantive technical answers. When in doubt between 'yes' and 'no', prefer 'yes' if "
            "there is real technical substance to extract. Answer with exactly 'yes' or 'no'."
        )
    )
    question: str = dspy.OutputField(
        desc=(
            "A single, self-contained question capturing what the summary's content answers or explains -- "
            "either the technical problem/request described, or (if the summary is more of an explanation than "
            "a resolved problem) a natural question whose answer is the technical content in the summary, e.g. "
            "'How does X work?' or 'What happens when Y is run?'. Phrase it generally enough to match similar "
            "future questions. Do not reference 'the conversation', 'the summary', usernames, or ticket "
            "numbers. Leave empty if has_resolution is 'no'."
        )
    )
    answer: str = dspy.OutputField(
        desc=(
            "A clear, self-contained answer summarizing the technical content in the summary -- the resolution, "
            "process, or explanation, including specific steps, commands, or details given. Write it as a "
            "standalone technical answer -- do not reference 'the conversation', 'the summary', usernames, or "
            "ticket numbers. Leave empty if has_resolution is 'no'."
        )
    )


extract_qa_from_summary = dspy.Predict(ExtractQAFromSummary)


def convert_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Run one summary through the extraction signature. Returns
    {"outcome": "converted", "question", "answer"} or
    {"outcome": "skipped", "reason"}. Raises on LLM/parsing failure -- the
    caller records that as an "error" outcome (retryable on the next run)."""
    configure_dspy_lm()
    extracted = extract_qa_from_summary(conversation_summary=entry["text"])
    has_resolution = extracted.has_resolution.strip().lower()
    question = extracted.question.strip()
    answer = extracted.answer.strip()

    if has_resolution != "yes" or not question or not answer:
        return {"outcome": "skipped", "reason": f"has_resolution={has_resolution!r}, question/answer present={bool(question and answer)}"}

    return {"outcome": "converted", "question": question, "answer": answer}


# --- State tracking (mirrors techsupport_contextual_reembed.py's pattern) ---


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"entries": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


_TERMINAL_OUTCOMES = {"converted", "skipped"}


def is_done(state: Dict[str, Any], entry_id: str) -> bool:
    record = state["entries"].get(entry_id)
    return bool(record) and record.get("outcome") in _TERMINAL_OUTCOMES


# --- Main run ---


def run(
    files: Optional[List[str]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    skip_reembed: bool = False,
) -> Dict[str, Any]:
    all_entries = parse_all_summaries(SUMMARIES_DIR, only_files=files)
    state = load_state()

    pending = [e for e in all_entries if not is_done(state, e["id"])]
    already_done = len(all_entries) - len(pending)
    if limit is not None:
        pending = pending[:limit]

    counts = {"converted": 0, "skipped": 0, "error": 0}
    examples = {"converted": [], "skipped": []}
    errors: List[Dict[str, str]] = []

    for i, entry in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {entry['id']} ...", end=" ", flush=True)
        try:
            result = convert_entry(entry)
        except Exception as exc:  # noqa: BLE001 -- one bad entry must not abort the batch
            print(f"ERROR: {exc}")
            counts["error"] += 1
            errors.append({"id": entry["id"], "error": str(exc)})
            if not dry_run:
                state["entries"][entry["id"]] = {"outcome": "error", "error": str(exc)}
                save_state(state)
            continue

        if result["outcome"] == "skipped":
            print(f"skipped ({result['reason']})")
            counts["skipped"] += 1
            if len(examples["skipped"]) < 3:
                examples["skipped"].append({"id": entry["id"], "summary": entry["text"], **result})
            if not dry_run:
                state["entries"][entry["id"]] = {"outcome": "skipped", "reason": result["reason"]}
                save_state(state)
            continue

        print("converted")
        counts["converted"] += 1
        if len(examples["converted"]) < 3:
            examples["converted"].append({"id": entry["id"], "summary": entry["text"], **result})

        if not dry_run:
            # See the RETIRED / NEEDS REWORK note near the top of this file --
            # (question, answer) is a rough stand-in for (title, summary) here.
            append_summary_to_markdown(result["question"], result["answer"])
            state["entries"][entry["id"]] = {"outcome": "converted", "question": result["question"], "answer": result["answer"]}
            save_state(state)

    reembed_result = None
    if not dry_run and not skip_reembed and counts["converted"] > 0:
        print("\nTriggering full contextual re-embed of TECHSUPPORT_QA_PAIRS ...")
        reembed_result = techsupport_contextual_reembed.run_reembed()
        reembed_state = techsupport_contextual_reembed.load_state()
        reembed_state["last_reembed_timestamp"] = f"{time.time():.6f}"
        techsupport_contextual_reembed.save_state(reembed_state)
        print(f"Reembed done: {reembed_result}")

    return {
        "total_parsed": len(all_entries),
        "already_done_before_this_run": already_done,
        "processed_this_run": len(pending),
        "counts": counts,
        "errors": errors,
        "examples": examples,
        "reembed_result": reembed_result,
        "dry_run": dry_run,
    }


def print_summary(result: Dict[str, Any]) -> None:
    print("\n=== Historical Import Summary ===")
    print(f"Total summaries parsed: {result['total_parsed']}")
    print(f"Already done (from previous runs, not reprocessed): {result['already_done_before_this_run']}")
    print(f"Processed this run: {result['processed_this_run']}")
    print(f"  Converted: {result['counts']['converted']}")
    print(f"  Skipped (no real resolution): {result['counts']['skipped']}")
    print(f"  Errors: {result['counts']['error']}")
    if result["errors"]:
        print("  Error detail:")
        for err in result["errors"]:
            print(f"    {err['id']}: {err['error']}")
    if result["dry_run"]:
        print("(dry run -- nothing was written to markdown, state, or LanceDB)")
    elif result["reembed_result"] is not None:
        print(f"Reembed: {result['reembed_result']}")
    elif result["counts"]["converted"] == 0:
        print("Reembed: skipped (no new conversions this run)")
    else:
        print("Reembed: skipped (--skip-reembed)")

    for label in ("converted", "skipped"):
        if result["examples"][label]:
            print(f"\n--- Example {label} entries ---")
            for ex in result["examples"][label]:
                print(f"\n[{ex['id']}]")
                print(f"Summary: {ex['summary'][:500]}")
                if label == "converted":
                    print(f"Question: {ex['question']}")
                    print(f"Answer: {ex['answer']}")
                else:
                    print(f"Reason: {ex['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", nargs="*", default=None, help="Restrict to these source filenames (e.g. summary_part_1.txt)")
    parser.add_argument("--limit", type=int, default=None, help="Max number of not-yet-done entries to process this run")
    parser.add_argument("--dry-run", action="store_true", help="Extract and print only; write nothing to disk")
    parser.add_argument("--skip-reembed", action="store_true", help="Skip the final full contextual re-embed step")
    parser.add_argument("--reembed-only", action="store_true", help="Skip parsing/conversion; just force a full re-embed now")
    args = parser.parse_args()

    if args.reembed_only:
        print("Triggering full contextual re-embed of TECHSUPPORT_QA_PAIRS ...")
        reembed_result = techsupport_contextual_reembed.run_reembed()
        reembed_state = techsupport_contextual_reembed.load_state()
        reembed_state["last_reembed_timestamp"] = f"{time.time():.6f}"
        techsupport_contextual_reembed.save_state(reembed_state)
        print(f"Reembed done: {reembed_result}")
        return

    result = run(files=args.files, limit=args.limit, dry_run=args.dry_run, skip_reembed=args.skip_reembed)
    print_summary(result)


if __name__ == "__main__":
    main()
