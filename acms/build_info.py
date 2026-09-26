"""Build identity: the deployed code identity of a running ACMS instance.

Written by the Docker build (deploy/Dockerfile) at build time so that the
running process can always report exactly what it is (feature-delivery plan §4:
a Git SHA is the authoritative deployed code identity).

Development (non-Docker) builds fall back to package constants plus an import
timestamp; unknown values render as ``unknown`` so the UI never fabricates.
"""
from __future__ import annotations

import os
import time

from . import __version__

__all__ = ["VERSION", "GIT_SHA", "BUILD_TIME", "as_dict"]

VERSION: str = __version__


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else "unknown"


GIT_SHA: str = _env("ACMS_BUILD_GIT_SHA")
BUILD_TIME: str = _env("ACMS_BUILD_TIME")


def as_dict() -> dict[str, str]:
    """Build identity dict for /version and the System UI page."""
    return {
        "version": VERSION,
        "git_sha": GIT_SHA,
        "git_sha_short": GIT_SHA[:7],
        "build_time": BUILD_TIME,
        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }