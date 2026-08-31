"""Crash-safe file writes: temp file in the same directory, then os.replace.

os.replace is atomic on POSIX when src and dest are on the same filesystem.
A kill during the write only leaves a leftover .tmp (or the previous file).
A truncating open(..., "w") would empty the live file first.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: PathLike, obj: Any, **dump_kwargs: Any) -> None:
    dump_kwargs.setdefault("indent", 2)
    text = json.dumps(obj, **dump_kwargs)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)
