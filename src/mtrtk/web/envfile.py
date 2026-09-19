r"""Minimal .env reader/writer that preserves comments and unrelated lines.

The grammar here mirrors python-dotenv's, because that is the parser pydantic-settings actually
reads `.env` with, and a writer that disagrees with the reader silently corrupts settings: a bare
value ends at the first whitespace-then-`#` and is stripped of surrounding space, a leading quote
starts a quoted value, and a double-quoted value decodes `\\`, `\"`, `\'` and the usual `\n`-style
escapes. So `update_env` writes a value quoted whenever a bare line would not read back
identically - a password like `hunter2 #1` would otherwise be saved as `hunter2` and lock the
operator out - and refuses a value containing a newline outright, since that would smuggle a
second assignment into the file.

Duplicate keys follow the parser's rule too: the last occurrence is the one in force, so that is
the one an update rewrites, and the earlier copies of that key are dropped.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

# A line that binds a key, with python-dotenv's optional `export ` and horizontal space.
_ASSIGN_RE = re.compile(
    r"^[^\S\r\n]*(?:export[^\S\r\n]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[^\S\r\n]*=(?P<rest>.*)$"
)
# "A backslash always escapes the character after it", so `\\` is not the start of an escaped
# quote - these two are python-dotenv's own value patterns.
_DOUBLE_QUOTED_RE = re.compile(r'^"((?:\\.|[^"\\])*)"')
_SINGLE_QUOTED_RE = re.compile(r"^'((?:\\.|[^'\\])*)'")
_DOUBLE_ESCAPES = re.compile(r"\\[\\'\"abfnrtv]")
_SINGLE_ESCAPES = re.compile(r"\\[\\']")
_COMMENT_RE = re.compile(r"\s+#")  # python-dotenv's own `\s+#.*` comment strip, run and all
_NEWLINE_RE = re.compile(r"[\r\n]")

# `.env` carries the NTRIP and web passwords, so a file this module creates is owner-only. A file
# that already exists keeps whatever mode the operator gave it - the atomic replace must not
# silently widen a 0600 secrets file to the umask default.
_NEW_FILE_MODE = 0o600


class _Entry(NamedTuple):
    key: str
    value: str  # decoded, exactly what python-dotenv would hand pydantic-settings
    comment: str  # "" or the trailing "   # ..." exactly as written, so a rewrite can keep it


def _decode(escapes: re.Pattern[str], value: str) -> str:
    # Every match is a backslash plus one ASCII character, so the ascii round-trip is safe.
    return escapes.sub(lambda m: m[0].encode("ascii").decode("unicode-escape"), value)


def _parse_line(raw: str) -> _Entry | None:
    """The key, decoded value and trailing comment of an assignment, or None for anything else."""
    m = _ASSIGN_RE.match(raw)
    if m is None:
        return None
    rest = m["rest"].lstrip(" \t")  # python-dotenv's `=` swallows the horizontal space after it
    quoting = ((_DOUBLE_QUOTED_RE, _DOUBLE_ESCAPES), (_SINGLE_QUOTED_RE, _SINGLE_ESCAPES))
    for pattern, escapes in quoting:
        quoted = pattern.match(rest)
        if quoted is not None:
            return _Entry(m["key"], _decode(escapes, quoted[1]), rest[quoted.end() :])
    split = _COMMENT_RE.search(rest)
    if split is None:
        return _Entry(m["key"], rest.rstrip(), "")
    return _Entry(m["key"], rest[: split.start()].rstrip(), rest[split.start() :])


def read_env(path: Path) -> dict[str, str]:
    """Every binding in *path*, decoded. Last occurrence wins, as in python-dotenv."""
    file = Path(path)
    if not file.exists():
        return {}
    values: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        entry = _parse_line(raw)
        if entry is not None:
            values[entry.key] = entry.value
    return values


def to_env_value(value: Any) -> str:
    """Render a settings value as the string a `.env` line should hold.

    The result is *unquoted*: `update_env` decides what needs quoting, so that a caller passing a
    plain string (a site name, a mode) is protected by exactly the same rule.
    """
    if value is None:
        return ""
    # Before the scalar branch: `Role` and `BaseMode` are `StrEnum`, so `str()` of a member is
    # fine, but `str()` of an `IntEnum` member would write "DynModel.PORTABLE".
    if isinstance(value, Enum):
        return _no_newline(str(value.value))
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list | tuple):
        return _no_newline(",".join(str(v) for v in value))
    return _no_newline(str(value))


def _no_newline(value: str) -> str:
    if _NEWLINE_RE.search(value):
        # No value in the message: the caller may be holding a password.
        raise ValueError("value must not contain a newline")
    return value


def _needs_quotes(value: str) -> bool:
    """True when a bare `KEY=value` line would not read back as *value*."""
    if value == "":
        return False
    if value != value.strip():
        return True  # python-dotenv strips both ends of a bare value
    if _COMMENT_RE.search(value):
        return True  # everything from `<space>#` on would be read as a comment
    if value[0] in "#\"'":
        return True  # would start a comment, or a quoted value that ends in the wrong place
    return any(char in value for char in "\"'\\")


def _encode(value: str) -> str:
    _no_newline(value)
    if not _needs_quotes(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Set every key in *updates*, leaving the rest of the file byte-for-byte alone.

    Values are given raw and quoted here as needed. Keys already present are rewritten in place
    (keeping their trailing `# comment`); the rest are appended. The whole file is then swapped in
    with a single `os.replace`, so a reader either sees the old file or the new one, never a
    half-written one. A value carrying a newline raises `ValueError` before anything is written.
    """
    path = Path(path).resolve()  # write through a symlinked `.env`, not over the link itself
    encoded = {key: _encode(value) for key, value in updates.items()}
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entries = [_parse_line(raw) for raw in lines]
    # python-dotenv is last-wins, so only the last occurrence of a key is in force: rewrite that
    # one and drop the earlier copies, which would otherwise sit in the file looking authoritative.
    last = {e.key: i for i, e in enumerate(entries) if e is not None and e.key in encoded}
    out: list[str] = []
    for i, (raw, entry) in enumerate(zip(lines, entries, strict=True)):
        if entry is None or entry.key not in encoded:
            out.append(raw)
            continue
        if last[entry.key] != i:
            continue  # an earlier duplicate of a key being rewritten below
        value, comment = encoded[entry.key], entry.comment
        if value == "" and comment.strip().startswith("#"):
            # `.env.example`'s own rule: a key with an empty value must not carry a trailing
            # comment, because a parser that does not strip it reads "# ..." as the value. Keep
            # the note - one line up, which is where the template puts it.
            out.append(comment.strip())
            out.append(f"{entry.key}=")
        else:
            out.append(f"{entry.key}={value}{comment}")
    out.extend(f"{key}={value}" for key, value in encoded.items() if key not in last)
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
