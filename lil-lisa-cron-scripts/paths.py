"""
Filesystem layout for lil-lisa-cron-scripts.

These jobs used to live in LilLisa_Server/scripts/. The API image still
does not run them; they are a sibling package so DSPy stays out of the
server's default dependencies.

LilLisa_Server remains the source of:
  - env/lillisa_server.env
  - data/ (verified markdown, historical import dumps)
  - lancedb / passwords (paths inside that env file, relative to the server root)
  - src/ (VoyageEmbedding, LanceDBVectorStore)
  - scripts/techsupport_thread_tags.json (written by POST /tag_techsupport_thread/)

Override LILLISA_SERVER_ROOT if the server checkout is not ../LilLisa_Server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
LILLISA_SERVER_ROOT = Path(
    os.environ.get("LILLISA_SERVER_ROOT", str(REPO_ROOT / "LilLisa_Server"))
).resolve()
LILLISA_SERVER_ENV_PATH = LILLISA_SERVER_ROOT / "env" / "lillisa_server.env"
# The FastAPI process writes this file; cron only reads it. Keep it on the
# server tree even after the Python jobs moved.
THREAD_TAGS_PATH = LILLISA_SERVER_ROOT / "scripts" / "techsupport_thread_tags.json"


def ensure_import_paths() -> None:
    """Allow `import nightly_pipeline` and `from src.embedding_config import ...`."""
    for path in (PACKAGE_ROOT, LILLISA_SERVER_ROOT):
        rendered = str(path)
        if rendered not in sys.path:
            sys.path.insert(0, rendered)
