"""Suite-wide fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_data_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point DATA_DIR at a scratch directory for every test.

    `Settings.data_dir` defaults to `/data`, the container's volume. Anything that builds a
    `Settings` without naming a directory - the daemon, the CLI - would otherwise open its
    database and write its logs into the host's real data directory, or fail outright where
    `/data` is absent or read-only. A test that cares about the location still sets its own
    `DATA_DIR` or passes `data_dir=`, which wins over this default.
    """
    root = tmp_path_factory.mktemp("data_dir")
    monkeypatch.setenv("DATA_DIR", str(root))
    return root
