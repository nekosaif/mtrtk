"""Move a station between hosts: database + sites + (masked) environment.

A backup is a `tar.gz` holding exactly four members:

- `manifest.json`: mtrtk version, creation time, station id, role, schema version, `with_secrets`;
- `mtrtk.db`: a consistent snapshot taken with SQLite's online backup API, so the writes a running
  daemon still holds in `mtrtk.db-wal` are in it (a plain file copy would miss them, or catch a
  half-written page);
- `env`: the `.env` the daemon reads (`MTRTK_ENV_FILE`), with every password, token and URL
  password replaced by `***` unless `with_secrets`;
- `sites.json`: the saved sites as plain JSON, readable without SQLite.

Raw `.ubx` logs are not included: they are hourly files under `DATA_DIR` that a plain `rsync`
moves better than an archive can, and the log index is rebuilt from the files themselves.

Restoring reads those members by name and never extracts anything else, so an archive carrying
`../` or absolute paths cannot write outside `DATA_DIR`. The database is checked before it
replaces anything: SQLite's integrity check must pass and its schema must not be newer than this
mtrtk knows. The archived `.env` is never merged automatically - it is written next to the
database as `restored.env` (owner-only) for the operator to compare and merge by hand.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from mtrtk import __version__
from mtrtk.config import Settings
from mtrtk.store.db import latest_schema_version
from mtrtk.web.api.config import MASK, SECRET_KEYS, mask_url_password
from mtrtk.web.envfile import encode_value, parse_assignment, read_env

FORMAT_VERSION = 1
DB_NAME = "mtrtk.db"
MEMBERS = ("manifest.json", DB_NAME, "env", "sites.json")
RESTORED_ENV = "restored.env"
RESTORED_SITES = "restored-sites.json"

# The settings `GET /api/config` masks, plus what `.env` carries for other readers: the
# Cloudflare tunnel token (compose) and the ROS 2 bridge's web token. A name that merely looks
# like a credential is masked too - a key added later must not leak because nobody listed it.
SECRET_ENV_KEYS = {k.upper() for k in SECRET_KEYS} | {"TUNNEL_TOKEN", "MTRTK_WS_TOKEN"}
_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|TOKEN|SECRET|PRIVATE|(?:^|_)API_?KEY(?:_|$)")
# `# KEY=value`: a commented-out assignment, often an old password or the real one parked.
_COMMENTED = re.compile(r"^(?P<lead>[^\S\r\n]*#[^\S\r\n]*)(?P<body>.*)$")
# `... WEB_PASSWORD=parked ...` anywhere in a comment's prose: the value runs to the next blank,
# less trailing punctuation. `WEB_PASSWORD: required` (no `=`) is documentation and stays.
_PROSE_ASSIGNMENT = re.compile(
    r"(?P<key>\b[A-Za-z_][A-Za-z0-9_]*)(?P<eq>[^\S\r\n]*=[^\S\r\n]*)"
    r"(?P<value>[^\s]+?)(?P<tail>[,;.)]*)(?=\s|$)"
)
# A credential carried as a URL query parameter: `?token=...`, `&api_key=...`, `&Password=...`.
_QUERY_SECRET = re.compile(
    r"(?P<name>[?&][^=&#]*(?:token|key|secret|passw(?:or)?d)[^=&#]*=)(?P<value>[^&#]*)", re.I
)
# `KEY="...` with no closing quote on the line: python-dotenv reads on to the next lines.
_OPEN_QUOTE = re.compile(r"""^[^=]*=[^\S\r\n]*(?P<q>["'])(?P<rest>.*)$""")


class BackupError(Exception):
    """The archive cannot be read or restored; the message says why and names the file."""


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return upper in SECRET_ENV_KEYS or _SECRET_NAME.search(upper) is not None


def _mask_url(value: str) -> str:
    """`mask_url_password`, plus the value of any query parameter named like a credential."""
    masked = mask_url_password(value)
    if "://" not in masked:
        return masked
    return _QUERY_SECRET.sub(lambda m: m["name"] + (MASK if m["value"] else ""), masked)


def _mask_prose(line: str) -> str:
    """A comment line with every `SECRET_NAME=value` in its text masked."""

    def hide(m: re.Match[str]) -> str:
        if not _is_secret_key(m["key"]):
            return m[0]
        return f"{m['key']}{m['eq']}{MASK}{m['tail']}"

    return _PROSE_ASSIGNMENT.sub(hide, line)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def masked_env(text: str) -> str:
    """*text* (a `.env` file) with every secret value replaced by `***`.

    Lines are read with the same grammar the daemon reads them with, so `export KEY=...`, quoted
    values and trailing comments are all recognised. An empty value stays empty: "unset" is
    configuration worth keeping. A value that is a URL with a password keeps everything but the
    password, which is what a `ntrip://user:pass@host/MP` correction source needs to be useful.

    A commented-out assignment (`# NTRIP_PASSWORD=old`) is masked the same way, since a parked
    password is often the real one or one used elsewhere; and the continuation lines of a
    multi-line quoted secret are dropped, not kept in the clear.
    """
    out: list[str] = []
    closing: str | None = None  # the quote that ends a multi-line secret being dropped
    # A BOM would glue itself to the first key and hide it from every rule below.
    for line in text.removeprefix("\ufeff").splitlines():
        if closing is not None:
            if _closes(line, closing):
                closing = None
            continue
        lead = ""
        parsed = parse_assignment(line)
        if parsed is None:
            commented = _COMMENTED.match(line)
            if commented is not None:
                lead = commented["lead"]
                parsed = parse_assignment(commented["body"])
        if parsed is None or parsed[1] == "":
            out.append(_mask_prose(line) if _is_comment(line) else line)
            continue
        key, value = parsed
        if _is_secret_key(key):
            out.append(f"{lead}{key}={MASK}")
            opened = _OPEN_QUOTE.match(line)
            if opened is not None and not _closes(opened["rest"], opened["q"]):
                closing = opened["q"]
            continue
        masked = _mask_url(value)
        if masked == value:
            out.append(_mask_prose(line) if lead else line)
            continue
        try:
            out.append(f"{lead}{key}={encode_value(masked)}")
        except ValueError:  # a value no line can carry back: hide it whole
            out.append(f"{lead}{key}={MASK}")
    return "\n".join(out) + "\n" if out else ""


def _closes(text: str, quote: str) -> bool:
    """True when *text* holds an unescaped *quote*."""
    return re.search(rf"(?<!\\){re.escape(quote)}", text) is not None


def env_differences(archived: str, current: Path) -> list[str]:
    """The archived `.env` assignments the *current* file does not already hold, for a hand merge.

    One `KEY=value` line per key, in the archive's order. No secret value is ever shown: a
    secret the backup masked is listed only where the current file has none (it cannot be
    compared otherwise), and a real one that differs is named without its value.
    """
    now = read_env(current)
    values: dict[str, str] = {}
    for line in archived.splitlines():
        parsed = parse_assignment(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]  # last wins, as python-dotenv reads it
    out: list[str] = []
    for key, value in values.items():
        here = now.get(key)
        if here == value:
            continue
        if _is_secret_key(key):
            if value == MASK:
                if not here:
                    out.append(f"{key}={MASK}  (masked in the backup: set it by hand)")
            elif value or here:
                out.append(f"{key}={MASK}  (differs: the archived value is in {RESTORED_ENV})")
            continue
        shown = _mask_url(value)
        if shown != value:
            # A real URL password (a with-secrets backup) that differs: never shown here.
            out.append(f"{key}={shown}  (differs: the archived value is in {RESTORED_ENV})")
        elif f":{MASK}@" in value or f"={MASK}" in value:
            if isinstance(here, str) and _mask_url(here) == value:
                continue  # the same URL; the backup masked the password the comparison needs
            out.append(f"{key}={value}  (password masked in the backup: set it by hand)")
        else:
            out.append(f"{key}={value}")
    return out


def _env_text(path: Path, with_secrets: bool) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8-sig")
    return text if with_secrets else masked_env(text)


@contextlib.contextmanager
def _sqlite(
    path: Path, *, readonly: bool = False, timeout: float = 10.0
) -> Iterator[sqlite3.Connection]:
    """A connection that is closed on exit (`sqlite3.Connection`'s own `with` only commits)."""
    target = f"{path.resolve().as_uri()}?mode=ro" if readonly else str(path)
    conn = sqlite3.connect(target, uri=readonly, timeout=timeout, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def _snapshot(src: Path, dst: Path) -> None:
    with _sqlite(src) as source, _sqlite(dst) as target:
        source.backup(target)


def _sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM sites ORDER BY name")]


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _owner_only(path: Path) -> IO[bytes]:
    """A new file only its owner can read: the archive may hold the station's passwords."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def create_backup(settings: Settings, out: Path, *, with_secrets: bool = False) -> Path:
    """Write the archive to *out* (replacing it atomically) and return its path.

    Safe next to a running daemon: the database is copied with SQLite's backup API, which
    takes a consistent snapshot without stopping the writer.
    """
    out = Path(out)
    db_path = settings.data_dir / DB_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    mtime = int(now.timestamp())
    with tempfile.TemporaryDirectory(prefix=".mtrtk-backup-", dir=out.parent) as tmp:
        sites: list[dict[str, Any]] = []
        schema = 0
        snapshot = Path(tmp) / DB_NAME
        has_db = db_path.is_file()
        env_file = settings.mtrtk_env_file
        if not has_db and not env_file.is_file():
            # From $HOME or cron, DATA_DIR and `.env` fall back to /data and ./.env: an archive
            # of nothing would only be found out at restore time.
            raise BackupError(
                f"nothing to back up: no database at {db_path.absolute()} and no .env at "
                f"{env_file.absolute()} (set DATA_DIR and MTRTK_ENV_FILE, or run from the clone)"
            )
        if has_db:
            try:
                _snapshot(db_path, snapshot)
                with _sqlite(snapshot) as conn:
                    schema = _user_version(conn)
                    sites = _sites(conn)
            except sqlite3.Error as exc:
                raise BackupError(f"cannot read {db_path}: {exc}") from exc
        manifest = {
            "format": FORMAT_VERSION,
            "version": __version__,
            "created_utc": now.isoformat(),
            "station_id": settings.station_id,
            "role": settings.role.value,
            "schema_version": schema,
            "db": has_db,
            "with_secrets": with_secrets,
        }
        partial = Path(tmp) / "archive.tar.gz"
        with _owner_only(partial) as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode(), mtime)
            if has_db:
                info = tar.gettarinfo(snapshot, arcname=DB_NAME)
                info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
                with snapshot.open("rb") as db_file:
                    tar.addfile(info, db_file)
            env = _env_text(env_file, with_secrets).encode()
            _add_bytes(tar, "env", env, mtime)
            sites_json = json.dumps(sites, indent=2, default=str).encode()
            _add_bytes(tar, "sites.json", sites_json, mtime)
        os.replace(partial, out)
    return out


def _member(tar: tarfile.TarFile, name: str) -> IO[bytes] | None:
    try:
        info = tar.getmember(name)
    except KeyError:
        return None
    if not info.isfile():
        raise BackupError(f"{name} in the archive is not a regular file")
    return tar.extractfile(info)


def _read_manifest(tar: tarfile.TarFile, archive: Path) -> dict[str, Any]:
    fh = _member(tar, "manifest.json")
    if fh is None:
        raise BackupError(f"{archive} has no manifest.json: not an mtrtk backup")
    try:
        manifest = json.loads(fh.read())
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"{archive}: manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupError(f"{archive}: manifest.json is not an object")
    try:
        fmt = int(manifest.get("format", FORMAT_VERSION))
    except (TypeError, ValueError) as exc:
        raise BackupError(f"{archive}: manifest.json has no usable format") from exc
    if fmt > FORMAT_VERSION:
        raise BackupError(
            f"{archive} was made by a newer mtrtk (backup format {fmt}; this mtrtk reads "
            f"{FORMAT_VERSION}): upgrade mtrtk first"
        )
    return manifest


def _check_database(path: Path) -> None:
    """Refuse a file that is not a sound mtrtk database, or one from a newer mtrtk."""
    try:
        with _sqlite(path, readonly=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise BackupError(
                    f"the archived database failed SQLite's integrity check: {result}"
                )
            version = _user_version(conn)
            conn.execute("SELECT count(*) FROM sites").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"the archived database is not a usable mtrtk database: {exc}") from exc
    latest = latest_schema_version()
    if version > latest:
        raise BackupError(
            f"the archived database is from a newer mtrtk (schema {version}; this mtrtk "
            f"knows {latest}): upgrade mtrtk first"
        )


def _ensure_not_in_use(db_path: Path) -> None:
    """Refuse to replace a database another process has open - a running daemon, typically.

    The daemon would keep writing to the file it opened, now unlinked, and the restored one
    would be ignored until its next start, then overwritten by nothing it remembers. SQLite will
    only take a WAL database out of WAL mode while no other connection has it open, so that
    switch is the test; it also folds the WAL back into the file before the copy is kept.
    """
    try:
        with _sqlite(db_path, timeout=1.0) as conn:
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc) and "busy" not in str(exc):
            return  # damaged rather than busy: `_keep_previous` keeps its bytes as they are
        mode = None
    except sqlite3.DatabaseError:
        return
    if mode is None or str(mode[0]).lower() != "delete":
        raise BackupError(
            f"{db_path} is open in another process: stop mtrtk first "
            "(systemctl stop mtrtk, or docker compose stop mtrtk), then restore"
        )


def _keep_previous(db_path: Path) -> Path:
    """Copy the database a forced restore is about to replace, WAL included, beside it."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    keep = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}")
    try:
        _snapshot(db_path, keep)
    except sqlite3.Error:  # unreadable as a database: keep the bytes as they are
        keep.unlink(missing_ok=True)
        shutil.copy2(db_path, keep)
        wal = db_path.with_name(f"{db_path.name}-wal")
        if wal.exists():
            shutil.copy2(wal, keep.with_name(f"{keep.name}-wal"))
    return keep


def _keep_reference(path: Path, text: str) -> None:
    """Before *path* is replaced by *text*, keep what it holds - unless that is *text* already.

    `restored.env` from an earlier restore may be the only copy of a with-secrets `.env`.
    """
    if not path.is_file() or path.read_text(encoding="utf-8") == text:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    keep = path.with_name(f"{path.name}.pre-restore-{stamp}")
    n = 1
    while keep.exists():
        keep = path.with_name(f"{path.name}.pre-restore-{stamp}-{n}")
        n += 1
    os.replace(path, keep)  # a rename keeps the owner-only mode


def _write_owner_only(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    with _owner_only(tmp) as fh:
        fh.write(text.encode("utf-8"))
    os.replace(tmp, path)


def restore_backup(archive: Path, settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """Put the archived database into `DATA_DIR`; the daemon must be stopped first.

    Raises `FileExistsError` when a database is already there and *force* is not given, and
    `BackupError` when the archive is unreadable, is not an mtrtk backup, or holds a database
    that is damaged or newer than this mtrtk. Nothing in `DATA_DIR` changes in either case.

    With *force*, the database being replaced is first copied to `mtrtk.db.pre-restore-<UTC>`.
    The archived `.env` is written to `DATA_DIR/restored.env` and the sites to
    `restored-sites.json`, for reference; neither is applied. An earlier restore's copies that
    differ are kept beside them as `<name>.pre-restore-<UTC>`.
    """
    archive = Path(archive)
    data_dir = settings.data_dir
    db_path = data_dir / DB_NAME
    previous: Path | None = None
    try:
        with tarfile.open(archive, "r:*") as tar:
            manifest = _read_manifest(tar, archive)
            data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".mtrtk-restore-", dir=data_dir) as tmp:
                staged = Path(tmp) / DB_NAME
                db_member = _member(tar, DB_NAME)
                if db_member is not None:
                    with staged.open("wb") as fh:
                        shutil.copyfileobj(db_member, fh)
                    _check_database(staged)
                sites_member = _member(tar, "sites.json")
                sites = json.loads(sites_member.read()) if sites_member is not None else []
                env_member = _member(tar, "env")
                env_text = env_member.read().decode("utf-8") if env_member is not None else ""

                if db_member is not None:
                    if db_path.exists():
                        if not force:
                            raise FileExistsError(f"{db_path} exists; pass --force to overwrite")
                        _ensure_not_in_use(db_path)
                        previous = _keep_previous(db_path)
                    # A WAL left beside the old file would be replayed into the new one.
                    for suffix in ("", "-wal", "-shm"):
                        data_dir.joinpath(f"{DB_NAME}{suffix}").unlink(missing_ok=True)
                    os.replace(staged, db_path)
    except FileExistsError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        # A truncated download, a file that is not a tar.gz, a sites.json that is not JSON, or
        # a DATA_DIR this user cannot write.
        raise BackupError(f"cannot restore {archive}: {exc}") from exc

    sites_text = json.dumps(sites, indent=2)
    _keep_reference(data_dir / RESTORED_SITES, sites_text)
    (data_dir / RESTORED_SITES).write_text(sites_text, encoding="utf-8")
    env_path: Path | None = None
    if env_text.strip():  # an empty member is a backup made where there was no `.env`
        env_path = data_dir / RESTORED_ENV
        _keep_reference(env_path, env_text)
        _write_owner_only(env_path, env_text)
    return {
        "manifest": manifest,
        "db": db_member is not None,
        "sites": len(sites) if isinstance(sites, list) else 0,
        "env": env_text,
        "env_path": str(env_path) if env_path is not None else None,
        "previous_db": str(previous) if previous is not None else None,
    }
