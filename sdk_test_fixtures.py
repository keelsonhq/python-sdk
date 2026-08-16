"""Locate shared SDK parity fixtures in standalone and monorepo checkouts."""

from __future__ import annotations

import os
from pathlib import Path


def parity_fixtures_dir(test_file: str | Path) -> Path:
    """Return the parity fixture directory for the current checkout layout.

    ``KEELSON_SDK_FIXTURES_DIR`` takes precedence. Otherwise, the resolver
    checks the standalone repository layout first and the Keelson monorepo
    layout second.
    """
    configured = os.environ.get("KEELSON_SDK_FIXTURES_DIR", "").strip()
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        source = Path(test_file).resolve()
        candidates = [
            source.parents[2] / "fixtures" / "parity",
            source.parents[3] / "fixtures" / "parity",
        ]

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Could not find SDK parity fixtures; searched: {searched}")
