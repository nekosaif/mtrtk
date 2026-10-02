"""Keep `mtrtk doctor` tests off the host: no systemctl, timedatectl, docker, psutil or /etc/udev.

`run_checks` asks the host about ModemManager, the clock, docker and listening ports. Each of
those helpers is a module-level function in `mtrtk.doctor`; `patch_host_probes` replaces them
with a quiet host, and `REAL` keeps the originals for the tests that exercise them directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mtrtk import doctor

PROBED = (
    "_command_output",
    "_service_active",
    "_ntp_synchronized",
    "_udev_rule_present",
    "_port_owner",
    "_probe_firmware",
    "_public_addresses",
    "_daemon_process",
    "_in_container",
)
REAL: dict[str, Any] = {name: getattr(doctor, name) for name in PROBED}


def patch_host_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_probe(port: str, baud: int) -> str | None:
        raise AssertionError("doctor opened the receiver without --probe")

    monkeypatch.setattr(doctor, "_service_active", lambda name: False)
    monkeypatch.setattr(doctor, "_ntp_synchronized", lambda: True)
    monkeypatch.setattr(doctor, "_port_owner", lambda port, hosts=None: None)
    monkeypatch.setattr(doctor, "_udev_rule_present", lambda: False)
    monkeypatch.setattr(doctor, "_command_output", lambda args: "Docker version 29.6.2")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=100e9))
    monkeypatch.setattr(doctor, "_probe_firmware", no_probe)
    monkeypatch.setattr(doctor, "_public_addresses", list)
    monkeypatch.setattr(doctor, "_daemon_process", lambda: None)
    monkeypatch.setattr(doctor, "_in_container", lambda: False)
