"""
github_sync.py
====================================
Pushes the verified-techsupport markdown file (techsupport_qa_pairs.md) to a
dedicated private GitHub repo whenever nightly_pipeline.py's run has actually
changed it -- resolves techsupport_qa_ingest.py's FLAGGED OPEN ITEM (there is
now a real repo, unlike the golden QA pairs situation that comment
describes).

Clones the repo fresh into a temp directory on every call rather than keeping
a persistent local clone -- same clone-fresh-every-time pattern src/main.py
already uses for the golden QA pairs repo (QA_PAIRS_GITHUB_REPO_URL) -- so
there's no local git state that can drift or get corrupted between runs, and
the temp dir (which briefly holds the authenticated URL in its
.git/config) is deleted immediately after use.

Required env vars (read from scripts/env/github_push.env):
    GITHUB_TOKEN     - a GitHub personal access token with push access to
                        GITHUB_REPO_URL's repo
    GITHUB_REPO_URL  - HTTPS URL of the destination repo, e.g.
                        https://github.com/<org>/<repo>.git (no embedded
                        credentials -- the token is injected into the clone
                        URL in-memory for this call only)

Usage (as a library, e.g. from nightly_pipeline.py):
    from github_sync import push_verified_qa_pairs
    result = push_verified_qa_pairs()

Usage (standalone):
    python github_sync.py
"""

import filecmp
import logging
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

import git
from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_PATH = SCRIPT_DIR / "env" / "github_push.env"

# Deliberately duplicated from techsupport_qa_ingest.py rather than imported
# from it -- that module's top-level imports pull in the full LanceDB /
# llama_index / voyageai runtime stack (and the server env setup that
# requires), which this lightweight, standalone-runnable script has no need
# for. Same env-var-override-with-default pattern as the original.
VERIFIED_TECHSUPPORT_QA_FOLDERPATH = Path(
    os.environ.get(
        "VERIFIED_TECHSUPPORT_QA_FOLDERPATH",
        str(PROJECT_ROOT / "data" / "verified_techsupport"),
    )
)
TECHSUPPORT_QA_MARKDOWN_FILENAME = "techsupport_qa_pairs.md"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("github_sync")

REQUIRED_ENV_VARS = ("GITHUB_TOKEN", "GITHUB_REPO_URL")

# Explicit commit identity so this doesn't depend on whatever git config
# happens to be set on the machine running the nightly pipeline.
COMMIT_AUTHOR = git.Actor("LilLisa Techsupport Pipeline", "noreply@radiantlogic.com")


def load_env() -> Dict[str, str]:
    """Reads GITHUB_TOKEN / GITHUB_REPO_URL from scripts/env/github_push.env."""
    env = dict(dotenv_values(str(ENV_PATH)))
    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing required env var(s) {missing} - expected in {ENV_PATH}")
    return env


def _authenticated_url(repo_url: str, token: str) -> str:
    """Injects the token into the repo URL for this clone only. Must be
    https:// -- a token can't be used as a password over the git:// or ssh
    schemes GitHub also serves."""
    parts = urlsplit(repo_url)
    if parts.scheme != "https":
        raise ValueError(f"GITHUB_REPO_URL must be an https:// URL, got: {repo_url}")
    netloc = f"{token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def push_verified_qa_pairs() -> Dict[str, Any]:
    """Clones GITHUB_REPO_URL fresh into a temp dir, copies the current
    techsupport_qa_pairs.md over the repo's copy, and commits+pushes only if
    the content actually changed. Safe to call even when nothing changed
    (returns pushed=False rather than erroring)."""
    env = load_env()
    token = env["GITHUB_TOKEN"]
    repo_url = env["GITHUB_REPO_URL"]

    source_path = VERIFIED_TECHSUPPORT_QA_FOLDERPATH / TECHSUPPORT_QA_MARKDOWN_FILENAME
    if not source_path.exists():
        logger.info("No %s found at %s -- nothing to push", TECHSUPPORT_QA_MARKDOWN_FILENAME, source_path)
        return {"pushed": False, "reason": "source_missing"}

    temp_dir = tempfile.mkdtemp(prefix="techsupport_github_sync_")
    try:
        authenticated_url = _authenticated_url(repo_url, token)
        logger.info("Cloning %s into %s", repo_url, temp_dir)
        repo = git.Repo.clone_from(authenticated_url, temp_dir)

        dest_path = Path(temp_dir) / TECHSUPPORT_QA_MARKDOWN_FILENAME
        if dest_path.exists() and filecmp.cmp(source_path, dest_path, shallow=False):
            logger.info("%s unchanged -- skipping commit/push", TECHSUPPORT_QA_MARKDOWN_FILENAME)
            return {"pushed": False, "reason": "unchanged"}

        shutil.copyfile(source_path, dest_path)
        repo.index.add([TECHSUPPORT_QA_MARKDOWN_FILENAME])

        commit_message = f"Update verified techsupport QA pairs - {date.today().isoformat()}"
        repo.index.commit(commit_message, author=COMMIT_AUTHOR, committer=COMMIT_AUTHOR)
        repo.remote(name="origin").push()

        logger.info("Pushed commit: %s", commit_message)
        return {"pushed": True, "commit_message": commit_message}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    result = push_verified_qa_pairs()
    print(result)
