"""Application version and source revision helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
FALLBACK_VERSION = "0.0.0+unknown"
GIT_TIMEOUT_SECONDS = 1.5


def _git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output or None


def get_version() -> str:
    configured = os.getenv("BOT_VERSION", "").strip()
    if configured:
        return configured
    return _git_output("describe", "--tags", "--always", "--dirty") or FALLBACK_VERSION


def get_git_commit() -> str:
    return _git_output("rev-parse", "--short", "HEAD") or "unknown"
