"""
Filesystem layout for the nightly techsupport cron jobs.

These jobs live in LilLisa_Server/cron/ and run two ways: standalone under
host cron (`make run-nightly`), or in the API process via
LilLisa_Server/src/techsupport_cron.py. Either way the server tree is simply
this package's parent directory, which also makes the layout inside the
container (/app/cron -> /app) fall out for free.

LilLisa_Server remains the source of:
  - env/lillisa_server.env
  - data/ (verified markdown, historical import dumps)
  - lancedb / passwords (paths inside that env file, relative to the server root)
  - src/ (VoyageEmbedding, LanceDBVectorStore)
  - scripts/techsupport_thread_tags.json (written by POST /tag_techsupport_thread/)
  - scripts/techsupport_answer_tags.json (written by invoke() at answer time)

Override LILLISA_SERVER_ROOT only for unusual layouts; the default is correct
for both the repo checkout and the image.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
LILLISA_SERVER_ROOT = Path(os.environ.get("LILLISA_SERVER_ROOT", str(PACKAGE_ROOT.parent))).resolve()
LILLISA_SERVER_ENV_PATH = LILLISA_SERVER_ROOT / "env" / "lillisa_server.env"
# The FastAPI process writes these files; cron only reads them. Keep them on
# the server tree even after the Python jobs moved.
THREAD_TAGS_PATH = LILLISA_SERVER_ROOT / "scripts" / "techsupport_thread_tags.json"
# {session_id (Slack thread ts): title of the verified techsupport entry the
# bot's answer cited}, written by src/main.py's invoke().
ANSWER_TAGS_PATH = LILLISA_SERVER_ROOT / "scripts" / "techsupport_answer_tags.json"


def ensure_import_paths() -> None:
    """Allow `import nightly_pipeline` and `from src.embedding_config import ...`."""
    for path in (PACKAGE_ROOT, LILLISA_SERVER_ROOT):
        rendered = str(path)
        if rendered not in sys.path:
            sys.path.insert(0, rendered)
