"""Suite-wide fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_settings(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep every test off the host's data directory and off its real network interfaces.

    `Settings` defaults to `DATA_DIR=/data` (the container's volume) and to
    `NTRIP_BIND=tailscale` on the fixed port 2101, `WEB_BIND=tailscale` on the fixed port 8080.
    Anything that builds a `Settings` without saying otherwise - the daemon, the CLI - would then
    open its database and write its logs into the host's real data directory (or fail outright
    where `/data` is absent or read-only), and bind the machine's actual Tailscale address on
    ports a live base station may already own. `WEB_ALLOW_INSECURE=1` has to come with the
    loopback `WEB_BIND`: `Settings` refuses any non-`tailscale` web bind that has neither a
    password nor that flag, so without it every `Settings()` in the suite would fail validation.
    A test that cares still wins: an explicit `data_dir=` / `ntrip_bind=` / `web_bind=` kwarg
    beats the environment, as does a `monkeypatch.setenv` in the test body, and `test_config.py`
    clears these keys so it can still assert the real defaults - including the guard itself.
    """
    root = tmp_path_factory.mktemp("data_dir")
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("NTRIP_BIND", "127.0.0.1")
    monkeypatch.setenv("NTRIP_PORT", "0")
    monkeypatch.setenv("WEB_BIND", "127.0.0.1")
    monkeypatch.setenv("WEB_PORT", "0")
    monkeypatch.setenv("WEB_ALLOW_INSECURE", "1")
    return root
