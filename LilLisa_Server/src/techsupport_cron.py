"""In-process driver for the nightly techsupport pipeline.

This process is the scheduler. There is no host crontab: `main.lifespan`
starts the periodic tick below, and POST /run_nightly_pipeline/ forces a run
on demand. The pipeline modules stay in `cron/` rather than under `src/` so
they remain a self-contained package that can still be invoked directly
(`cd cron && make run-nightly`) for one-off ops work.

Runs are serialised by the lock below, so a manual trigger landing during a
tick run (or vice versa) is dropped rather than queued.
"""

import asyncio
import os
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

from src import utils

# The pipeline gates itself on TECHSUPPORT_SYNC_INTERVAL_HOURS (see
# nightly_pipeline.is_channel_check_due), so the tick only needs to fire more
# often than that interval -- not at a specific hour. Extra ticks no-op, and a
# container restart self-heals on the next one.
DEFAULT_TICK_MINUTES = 60


def _resolve_cron_root() -> Optional[Path]:
    """Locate the cron package: <server root>/cron in both the repo and the image."""
    server_root = Path(__file__).resolve().parent.parent
    candidate = Path(os.environ.get("TECHSUPPORT_CRON_ROOT") or server_root / "cron")
    return candidate.resolve() if (candidate / "nightly_pipeline.py").is_file() else None


CRON_ROOT = _resolve_cron_root()
if CRON_ROOT and str(CRON_ROOT) not in sys.path:
    sys.path.insert(0, str(CRON_ROOT))

try:
    # Needs CRON_ROOT on sys.path above; the cron package uses flat imports.
    from nightly_pipeline import run_pipeline  # noqa: E402

    IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # noqa: BLE001 -- a broken cron install must not stop the API from serving
    run_pipeline = None  # type: ignore[assignment]
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_run_lock = threading.Lock()


def is_available() -> bool:
    """False when the cron package is absent or failed to import."""
    return run_pipeline is not None


def is_running() -> bool:
    return _run_lock.locked()


def run_once() -> Dict[str, Any]:
    """Run the pipeline unless one is already in flight.

    Never raises: both callers (a FastAPI BackgroundTask and the tick) would
    otherwise drop the exception. run_pipeline() already posts per-step
    failures to the admin Slack channel; this is the last-resort net.
    """
    if run_pipeline is None:
        utils.logger.error("Nightly techsupport pipeline unavailable: %s", IMPORT_ERROR)
        return {"ran": False, "reason": "unavailable", "error": IMPORT_ERROR}

    if not _run_lock.acquire(blocking=False):
        utils.logger.info("Nightly techsupport pipeline already running -- ignoring this trigger")
        return {"ran": False, "reason": "already_running"}

    try:
        utils.logger.info("Nightly techsupport pipeline starting (cron root: %s)", CRON_ROOT)
        summary = run_pipeline()
        utils.logger.info("Nightly techsupport pipeline finished: %s", summary)
        return {"ran": True, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        utils.logger.critical("Nightly techsupport pipeline failed: %s", exc, exc_info=True)
        return {"ran": False, "error": str(exc)}
    finally:
        _run_lock.release()


def tick_minutes() -> int:
    raw = (utils.LILLISA_SERVER_ENV_DICT.get("TECHSUPPORT_PIPELINE_TICK_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_TICK_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        utils.logger.warning(
            "TECHSUPPORT_PIPELINE_TICK_MINUTES=%r is not an integer -- using %s", raw, DEFAULT_TICK_MINUTES
        )
        return DEFAULT_TICK_MINUTES
    if minutes <= 0:
        utils.logger.warning(
            "TECHSUPPORT_PIPELINE_TICK_MINUTES=%s must be positive -- using %s", minutes, DEFAULT_TICK_MINUTES
        )
        return DEFAULT_TICK_MINUTES
    return minutes


async def _scheduler_loop() -> None:
    """Sleep-then-run, so startup (which may rebuild indices) stays clean."""
    interval_seconds = tick_minutes() * 60
    while True:
        await asyncio.sleep(interval_seconds)
        # run_pipeline() is blocking (Slack + LLM calls), so keep it off the event loop.
        await asyncio.to_thread(run_once)


def start_scheduler() -> Optional[asyncio.Task]:
    """Start the periodic tick. This process is the only scheduler."""
    if not is_available():
        utils.logger.error(
            "Nightly techsupport pipeline will not run -- the cron package is unavailable: %s", IMPORT_ERROR
        )
        return None
    utils.logger.info("Nightly techsupport pipeline tick every %s minute(s)", tick_minutes())
    return asyncio.create_task(_scheduler_loop())


async def stop_scheduler(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
