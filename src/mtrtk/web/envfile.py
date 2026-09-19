"""Minimal .env reader/writer that preserves comments and unrelated lines."""

from __future__ import annotations

import contextlib
import os
import re
import stat
from enum import Enum
from pathlib import Path
from typing import Any

_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<comment>\s+#.*)?$")

# `.env` carries the NTRIP and web passwords, so a file this module creates is owner-only. A file
# that already exists keeps whatever mode the operator gave it - the atomic replace must not
# silently widen a 0600 secrets file to the umask default.
_NEW_FILE_MODE = 0o600


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not Path(path).exists():
        return values
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"') or "=" not in line:
            continue
        key, rest = line.split("=", 1)
        rest = rest.strip()
        if rest.startswith(("'", '"')):
            # A quoted value may legitimately contain a `#`, so the comment regex must not see it.
            values[key.strip()] = _unquote(
                rest.split(rest[0], 2)[1] if rest.count(rest[0]) >= 2 else rest
            )
            continue
        m = _LINE_RE.match(line)
        values[key.strip()] = _unquote(m["value"]) if m else _unquote(rest)
    return values


def to_env_value(value: Any) -> str:
    if value is None:
        return ""
    # Before the scalar branch: `Role` and `BaseMode` are `StrEnum`, so `str(member)` of a
    # member is fine but `str()` of an `IntEnum` member would write "DynModel.PORTABLE".
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Set every key in *updates*, leaving the rest of the file byte-for-byte alone.

    Keys already present are rewritten in place (keeping their trailing `# comment`); the rest are
    appended. The whole file is then swapped in with a single `os.replace`, so a reader either
    sees the old file or the new one, never a half-written one.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out: list[str] = []
    for raw in lines:
        m = _LINE_RE.match(raw.strip())
        if m is None or m["key"] not in pending:
            out.append(raw)
            continue
        value = pending.pop(m["key"])
        comment = m["comment"] or ""
        if value == "" and comment:
            # `.env.example`'s own rule: a key with an empty value must not carry a trailing
            # comment, because a parser that does not strip it reads "# ..." as the value. Keep
            # the note - one line up, which is where the template puts it.
            out.append(comment.strip())
            out.append(f"{m['key']}=")
        else:
            out.append(f"{m['key']}={value}{comment}")
    out.extend(f"{key}={value}" for key, value in pending.items())
    _atomic_write(path, "\n".join(out) + "\n" if out else "")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _NEW_FILE_MODE
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _NEW_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())  # the replace is only atomic over data that reached the disk
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Flush the rename itself: without this a power cut can lose the new directory entry."""
    with contextlib.suppress(OSError):  # not every filesystem allows it; the data is already safe
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
