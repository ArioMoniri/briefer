"""Report which build/commit is actually running.

Lets you confirm from Telegram (/status) and the startup log that the code
on the server matches what you pushed — the single most common source of
"but I fixed that" confusion on a self-hosted deploy.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # repo root (…/briefer)


@functools.lru_cache(maxsize=1)
def build_version() -> str:
    """Short git description of the running tree, e.g. 'b0d1771 (2026-07-11)'.

    Falls back to 'unknown' if git isn't available (e.g. a tarball deploy).
    Cached — the tree doesn't change while the process runs.
    """
    try:
        sha = subprocess.run(
            ["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not sha:
            return "unknown"
        date = subprocess.run(
            ["git", "-C", str(_ROOT), "log", "-1", "--format=%cd",
             "--date=short"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        tag = f"{sha} ({date})" if date else sha
        return tag + (" +local-edits" if dirty else "")
    except Exception:  # noqa: BLE001 — never let version reporting crash startup
        return "unknown"
