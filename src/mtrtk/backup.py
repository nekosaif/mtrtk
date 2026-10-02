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


class BackupError(Exception):
    """The archive cannot be read or restored; the message says why and names the file."""


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return upper in SECRET_ENV_KEYS or _SECRET_NAME.search(upper) is not None


def masked_env(text: str) -> str:
    """*text* (a `.env` file) with every secret value replaced by `***`.

    Lines are read with the same grammar the daemon reads them with, so `export KEY=...`, quoted
    values and trailing comments are all recognised. An empty value stays empty: "unset" is
    configuration worth keeping. A value that is a URL with a password keeps everything but the
    password, which is what a `ntrip://user:pass@host/MP` correction source needs to be useful.
    """
    out: list[str] = []
    for line in text.splitlines():
        parsed = parse_assignment(line)
        if parsed is None or parsed[1] == "":
            out.append(line)
            continue
        key, value = parsed
        if _is_secret_key(key):
            out.append(f"{key}={MASK}")
            continue
        masked = mask_url_password(value)
        if masked == value:
            out.append(line)
            continue
        try:
            out.append(f"{key}={encode_value(masked)}")
        except ValueError:  # a value no line can carry back: hide it whole
            out.append(f"{key}={MASK}")
    return "\n".join(out) + "\n" if out else ""


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
        shown = mask_url_password(value)
        if shown != value and mask_url_password(here) == shown:
            continue  # the same URL with a password the comparison cannot see
        out.append(f"{key}={shown}")
    return out


def _env_text(path: Path, with_secrets: bool) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
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
            env = _env_text(settings.mtrtk_env_file, with_secrets).encode()
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
    `restored-sites.json`, for reference; neither is applied.
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

    (data_dir / RESTORED_SITES).write_text(json.dumps(sites, indent=2), encoding="utf-8")
    env_path: Path | None = None
    if env_member is not None:
        env_path = data_dir / RESTORED_ENV
        _write_owner_only(env_path, env_text)
    return {
        "manifest": manifest,
        "db": db_member is not None,
        "sites": len(sites) if isinstance(sites, list) else 0,
        "env": env_text,
        "env_path": str(env_path) if env_path is not None else None,
        "previous_db": str(previous) if previous is not None else None,
    }
