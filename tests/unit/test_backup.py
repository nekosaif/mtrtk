import io
import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.backup import BackupError, create_backup, env_differences, restore_backup
from mtrtk.cli import main
from mtrtk.config import Settings
from mtrtk.store.db import Database
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo


async def seed(data_dir: Path, name: str = "roof") -> None:
    db = Database(data_dir / "mtrtk.db")
    await db.open()
    await SitesRepo(db).add(Site.from_ecef(name, 1.0, 2.0, 3.0, source="manual"))
    await db.close()


async def site_names(db_path: Path) -> list[str]:
    db = Database(db_path)
    await db.open()
    try:
        return [s.name for s in await SitesRepo(db).list()]
    finally:
        await db.close()


def file_text(path: str) -> str:
    return Path(path).read_text()


def write_file(path: str, text: str) -> None:
    Path(path).write_text(text)


def remove_file(path: str) -> None:
    Path(path).unlink()


def file_mode(path: str) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)


def member_text(archive: Path, name: str) -> str:
    with tarfile.open(archive) as tar:
        f = tar.extractfile(name)
        assert f is not None
        return f.read().decode()


async def test_backup_masks_secrets_and_includes_sites(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    await seed(data)
    env = tmp_path / ".env"
    env.write_text(
        "ROLE=base\nNTRIP_PASSWORD=supersecret\nWEB_PASSWORD=alsosecret\nSTATION_ID=MTRK\n"
    )
    settings = Settings(
        _env_file=None,
        data_dir=data,
        ntrip_password="supersecret",
        mtrtk_env_file=env,
    )
    archive = create_backup(settings, tmp_path / "b.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert {"manifest.json", "mtrtk.db", "env", "sites.json"} <= set(names)
    env_text = member_text(archive, "env")
    assert "supersecret" not in env_text and "alsosecret" not in env_text
    assert "NTRIP_PASSWORD=***" in env_text and "STATION_ID=MTRK" in env_text
    sites = json.loads(member_text(archive, "sites.json"))
    assert sites[0]["name"] == "roof"
    manifest = json.loads(member_text(archive, "manifest.json"))
    assert manifest["with_secrets"] is False and manifest["station_id"] == "MTRK"
    assert manifest["schema_version"] >= 1


async def test_backup_masks_every_spelling_of_a_secret(tmp_path: Path) -> None:
    """The file is masked as python-dotenv reads it: `export`, quotes, comments and URLs."""
    data = tmp_path / "data"
    data.mkdir()
    env = tmp_path / ".env"
    env.write_text(
        "# NTRIP_PASSWORD=commented-out-secret   # the old one\n"
        "export NTRIP_PASSWORD='quoted secret'\n"
        '  WEB_PASSWORD = "hunter2 #1"   # the web login\n'
        "ALERT_WEBHOOK_URL=https://hooks.example/T0/abc\n"
        "TUNNEL_TOKEN=eyJhIjoiYiJ9\n"
        "MTRTK_WS_TOKEN=wstoken123\n"
        "NTRIP_URL=ntrip://rover:urlpass@base:2101/MTRK\n"
        "WEB_PASSWORD_EMPTY_NEIGHBOUR=1\n"
        "ACTIVE_SITE=\n"
        # Keys no list names: only the "looks like a credential" rule masks these.
        "FOO_API_KEY=k1apikey\n"
        "SOME_SECRET=s1secret\n"
        "DB_PASSWD=p1passwd\n"
        "KEYBOARD=us\n"
        "APIKEYSTORE=kept-value\n"
        "#NTRIP_URL=ntrip://old:oldurlpass@base:2101/MTRK\n"
        'MTRTK_WS_TOKEN="first line of a\n'
        'multi-line token"\n'
    )
    settings = Settings(_env_file=None, data_dir=data, ntrip_password="", mtrtk_env_file=env)
    env_text = member_text(create_backup(settings, tmp_path / "b.tar.gz"), "env")
    for secret in ("quoted secret", "hunter2", "hooks.example", "eyJhIjoiYiJ9", "wstoken123"):
        assert secret not in env_text, secret
    assert "urlpass" not in env_text
    assert "NTRIP_URL=ntrip://rover:***@base:2101/MTRK" in env_text
    assert "ACTIVE_SITE=\n" in env_text  # unset stays visibly unset
    assert "WEB_PASSWORD_EMPTY_NEIGHBOUR=***" in env_text
    for secret in ("k1apikey", "s1secret", "p1passwd"):
        assert secret not in env_text, secret
    assert "FOO_API_KEY=***" in env_text and "DB_PASSWD=***" in env_text
    assert "KEYBOARD=us" in env_text and "APIKEYSTORE=kept-value" in env_text
    # A commented-out password is often the real one, or one used elsewhere.
    assert "commented-out-secret" not in env_text and "# NTRIP_PASSWORD=***" in env_text
    assert "oldurlpass" not in env_text
    assert "#NTRIP_URL=ntrip://old:***@base:2101/MTRK" in env_text
    assert "first line" not in env_text and "multi-line token" not in env_text


async def test_backup_with_secrets(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    await seed(data)
    env = tmp_path / ".env"
    env.write_text("NTRIP_PASSWORD=supersecret\n")
    settings = Settings(
        _env_file=None,
        data_dir=data,
        ntrip_password="supersecret",
        mtrtk_env_file=env,
    )
    archive = create_backup(settings, tmp_path / "b.tar.gz", with_secrets=True)
    assert "supersecret" in member_text(archive, "env")
    assert json.loads(member_text(archive, "manifest.json"))["with_secrets"] is True


async def test_archive_is_owner_only(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    await seed(data)
    settings = Settings(_env_file=None, data_dir=data, ntrip_password="x")
    archive = create_backup(settings, tmp_path / "out" / "b.tar.gz", with_secrets=True)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
    assert {m.name for m in members} == {"manifest.json", "mtrtk.db", "env", "sites.json"}
    for m in members:  # extracted by hand with `tar x`, nothing becomes world-readable
        assert (m.mode, m.uid, m.gid, m.uname, m.gname) == (0o600, 0, 0, "", ""), m.name


async def test_backup_snapshot_includes_uncheckpointed_wal(tmp_path: Path) -> None:
    """A running daemon keeps recent writes in `mtrtk.db-wal`; a file copy would miss them."""
    data = tmp_path / "data"
    data.mkdir()
    db = Database(data / "mtrtk.db")
    await db.open()
    await SitesRepo(db).add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    try:  # still open: the write sits in the WAL
        settings = Settings(_env_file=None, data_dir=data, ntrip_password="x")
        archive = create_backup(settings, tmp_path / "b.tar.gz")
    finally:
        await db.close()
    assert [s["name"] for s in json.loads(member_text(archive, "sites.json"))] == ["roof"]


async def test_restore_refuses_then_forces(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src)
    settings_src = Settings(
        _env_file=None,
        data_dir=src,
        ntrip_password="x",
        mtrtk_env_file=tmp_path / "none.env",
    )
    archive = create_backup(settings_src, tmp_path / "b.tar.gz")
    dst = tmp_path / "dst"
    dst.mkdir()
    settings_dst = Settings(_env_file=None, data_dir=dst, ntrip_password="x")
    info = restore_backup(archive, settings_dst)
    assert (dst / "mtrtk.db").exists() and info["sites"] == 1
    assert json.loads((dst / "restored-sites.json").read_text())[0]["name"] == "roof"
    with pytest.raises(FileExistsError):
        restore_backup(archive, settings_dst)
    before = sorted(p.name for p in dst.iterdir())
    with pytest.raises(FileExistsError):
        restore_backup(archive, settings_dst)
    assert sorted(p.name for p in dst.iterdir()) == before  # nothing in DATA_DIR changed
    restore_backup(archive, settings_dst, force=True)
    assert await site_names(dst / "mtrtk.db") == ["roof"]


async def test_forced_restore_keeps_the_database_it_replaces(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src, "roof")
    archive = create_backup(
        Settings(_env_file=None, data_dir=src, ntrip_password="x"),
        tmp_path / "b.tar.gz",
    )
    dst = tmp_path / "dst"
    dst.mkdir()
    await seed(dst, "field")
    info = restore_backup(
        archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"), force=True
    )
    assert await site_names(dst / "mtrtk.db") == ["roof"]
    previous = info["previous_db"]
    assert previous is not None and await site_names(Path(previous)) == ["field"]
    assert not (dst / "mtrtk.db-wal").exists() or (dst / "mtrtk.db-wal").stat().st_size == 0


async def test_restore_refuses_a_database_a_running_daemon_holds(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src, "roof")
    archive = create_backup(
        Settings(_env_file=None, data_dir=src, ntrip_password="x"), tmp_path / "b.tar.gz"
    )
    dst = tmp_path / "dst"
    dst.mkdir()
    await seed(dst, "field")
    live = Database(dst / "mtrtk.db")
    await live.open()  # what the daemon holds for as long as it runs
    try:
        with pytest.raises(BackupError, match="stop mtrtk"):
            restore_backup(
                archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"), force=True
            )
        assert [s.name for s in await SitesRepo(live).list()] == ["field"]
    finally:
        await live.close()
    assert not list(dst.glob("mtrtk.db.pre-restore-*"))


async def test_restore_writes_the_archived_env_owner_only(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src)
    env = tmp_path / ".env"
    env.write_text("NTRIP_PASSWORD=supersecret\nSTATION_ID=ABCD\n")
    settings = Settings(_env_file=None, data_dir=src, ntrip_password="x", mtrtk_env_file=env)
    archive = create_backup(settings, tmp_path / "b.tar.gz", with_secrets=True)
    dst = tmp_path / "dst"
    info = restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert info["env_path"] == str(dst / "restored.env")
    assert "supersecret" in file_text(info["env_path"])
    assert file_mode(info["env_path"]) == 0o600
    assert info["manifest"]["with_secrets"] is True


def _tar_with(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_restore_refuses_an_archive_that_is_not_an_mtrtk_backup(tmp_path: Path) -> None:
    archive = _tar_with(tmp_path / "x.tar.gz", {"mtrtk.db": b"not a database"})
    settings = Settings(_env_file=None, data_dir=tmp_path / "d", ntrip_password="x")
    with pytest.raises(BackupError, match="manifest"):
        restore_backup(archive, settings)
    assert not (tmp_path / "d" / "mtrtk.db").exists()


def test_restore_refuses_a_corrupt_database(tmp_path: Path) -> None:
    manifest = json.dumps({"version": "0.1.0", "with_secrets": False}).encode()
    archive = _tar_with(
        tmp_path / "x.tar.gz", {"manifest.json": manifest, "mtrtk.db": b"not a database" * 100}
    )
    settings = Settings(_env_file=None, data_dir=tmp_path / "d", ntrip_password="x")
    with pytest.raises(BackupError, match="database"):
        restore_backup(archive, settings)
    assert not (tmp_path / "d" / "mtrtk.db").exists()


def test_restore_refuses_a_database_from_a_newer_mtrtk(tmp_path: Path) -> None:
    db_path = tmp_path / "new.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sites (name TEXT)")
        conn.execute("PRAGMA user_version=999")
    conn.close()
    manifest = json.dumps({"version": "9.9.9", "with_secrets": False}).encode()
    archive = _tar_with(
        tmp_path / "x.tar.gz", {"manifest.json": manifest, "mtrtk.db": db_path.read_bytes()}
    )
    settings = Settings(_env_file=None, data_dir=tmp_path / "d", ntrip_password="x")
    with pytest.raises(BackupError, match="newer"):
        restore_backup(archive, settings)
    assert not (tmp_path / "d").exists() or not list((tmp_path / "d").iterdir())


def test_restore_ignores_members_outside_the_archive_layout(tmp_path: Path) -> None:
    manifest = json.dumps({"version": "0.1.0", "with_secrets": False}).encode()
    archive = _tar_with(
        tmp_path / "x.tar.gz", {"manifest.json": manifest, "../escaped": b"x", "/abs": b"y"}
    )
    dst = tmp_path / "deep" / "d"
    info = restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert info["sites"] == 0 and info["db"] is False
    # Staging happens inside DATA_DIR, so `../escaped` would land in DATA_DIR itself: look
    # everywhere, not one level up.
    assert not [p for p in tmp_path.rglob("*") if p.name in ("escaped", "abs")]
    assert not Path("/abs").exists()
    assert {p.name for p in dst.iterdir()} == {"restored-sites.json"}


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    env = tmp_path / ".env"
    env.write_text("NTRIP_PASSWORD=supersecret\nSTATION_ID=ABCD\n")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MTRTK_ENV_FILE", str(env))
    return data


async def test_cli_backup_and_restore(cli_env: Path, tmp_path: Path) -> None:
    await seed(cli_env)
    runner = CliRunner()
    out = tmp_path / "b.tar.gz"
    r = runner.invoke(main, ["backup", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert str(out) in r.output and "supersecret" not in member_text(out, "env")

    r = runner.invoke(main, ["restore", str(out)])
    assert r.exit_code != 0 and "--force" in r.output

    # The new host's .env differs: only the differences are shown, for a hand merge.
    env = os.environ["MTRTK_ENV_FILE"]
    write_file(env, "NTRIP_PASSWORD=\nSTATION_ID=WXYZ\nBAUD=115200\n")
    r = runner.invoke(main, ["restore", str(out), "--force"])
    assert r.exit_code == 0, r.output
    assert "1 site" in r.output
    assert "STATION_ID=ABCD" in r.output and "NTRIP_PASSWORD=***" in r.output
    assert "BAUD" not in r.output and "supersecret" not in r.output
    assert file_text(env) == "NTRIP_PASSWORD=\nSTATION_ID=WXYZ\nBAUD=115200\n"  # not merged


def test_env_differences(tmp_path: Path) -> None:
    current = tmp_path / ".env"
    current.write_text(
        "STATION_ID=MTRK\nNTRIP_PASSWORD=local\nWEB_PASSWORD=\nNTRIP_URL=ntrip://u:p@a:2101/M\n"
    )
    archived = (
        "STATION_ID=MTRK\nNTRIP_PASSWORD=***\nWEB_PASSWORD=***\nNTRIP_URL=ntrip://u:***@b:2101/M\n"
        "ROLE=rover\n"
    )
    assert env_differences(archived, current) == [
        "WEB_PASSWORD=***  (masked in the backup: set it by hand)",
        "NTRIP_URL=ntrip://u:***@b:2101/M  (password masked in the backup: set it by hand)",
        "ROLE=rover",
    ]
    with_secrets = "NTRIP_PASSWORD=remote\nNTRIP_URL=ntrip://u:p@a:2101/M\n"
    assert env_differences(with_secrets, current) == [
        "NTRIP_PASSWORD=***  (differs: the archived value is in restored.env)"
    ]


def test_env_differences_compares_urls_without_their_passwords(tmp_path: Path) -> None:
    current = tmp_path / ".env"
    current.write_text("NTRIP_URL=ntrip://u:p@a:2101/M\n")
    # The usual case: a masked backup of the same correction source is not a difference.
    assert env_differences("NTRIP_URL=ntrip://u:***@a:2101/M\n", current) == []
    # A with-secrets backup whose URL password differs is one, and the password is not shown.
    lines = env_differences("NTRIP_URL=ntrip://u:OTHER@a:2101/M\n", current)
    assert lines == [
        "NTRIP_URL=ntrip://u:***@a:2101/M  (differs: the archived value is in restored.env)"
    ]
    # A different host from a with-secrets backup: shown masked, also pointing at the file.
    lines = env_differences("NTRIP_URL=ntrip://u:OTHER@b:2101/M\n", current)
    assert lines == [
        "NTRIP_URL=ntrip://u:***@b:2101/M  (differs: the archived value is in restored.env)"
    ]
    assert not any("OTHER" in line for line in lines)


async def test_cli_restore_does_not_print_secrets(cli_env: Path, tmp_path: Path) -> None:
    await seed(cli_env)
    runner = CliRunner()
    out = tmp_path / "b.tar.gz"
    assert runner.invoke(main, ["backup", "--out", str(out), "--with-secrets"]).exit_code == 0
    (cli_env / "mtrtk.db").unlink()
    r = runner.invoke(main, ["restore", str(out)])
    assert r.exit_code == 0, r.output
    assert "supersecret" not in r.output and "restored.env" in r.output


def test_cli_restore_reports_a_bad_archive(cli_env: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"definitely not gzip")
    r = CliRunner().invoke(main, ["restore", str(bad)])
    assert r.exit_code != 0 and "Traceback" not in r.output and "bad.tar.gz" in r.output


def _sqlite_file(path: Path, sql: str) -> bytes:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    return path.read_bytes()


def _manifest(**extra: object) -> bytes:
    return json.dumps({"format": 1, "version": "0.1.0", "with_secrets": False, **extra}).encode()


def test_restore_refuses_a_database_that_fails_the_integrity_check(tmp_path: Path) -> None:
    """A database that opens, and whose sites table reads, but with a damaged page elsewhere."""
    db_path = tmp_path / "damaged.db"
    rows = ",".join(f"({i}, '{'x' * 200}')" for i in range(200))
    data = bytearray(
        _sqlite_file(
            db_path,
            "PRAGMA page_size=4096; CREATE TABLE sites (name TEXT);"
            "INSERT INTO sites VALUES ('roof');"
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, body TEXT);"
            f"INSERT INTO logs VALUES {rows};"
            "CREATE INDEX logs_body ON logs(body);",
        )
    )
    page = 4096
    assert len(data) > 8 * page
    last = len(data) // page - 1  # an index page, far from the schema and the sites table
    data[last * page + 8 : last * page + 200] = b"\xa5" * 192
    db_path.write_bytes(bytes(data))
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM sites").fetchone() == (1,)
    conn.close()
    archive = _tar_with(
        tmp_path / "x.tar.gz", {"manifest.json": _manifest(), "mtrtk.db": bytes(data)}
    )
    dst = tmp_path / "d"
    dst.mkdir()
    with pytest.raises(BackupError, match="integrity|usable"):
        restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert list(dst.iterdir()) == []


def test_restore_refuses_a_database_without_a_sites_table(tmp_path: Path) -> None:
    db = _sqlite_file(tmp_path / "other.db", "CREATE TABLE something (x INTEGER);")
    archive = _tar_with(tmp_path / "x.tar.gz", {"manifest.json": _manifest(), "mtrtk.db": db})
    dst = tmp_path / "d"
    dst.mkdir()
    with pytest.raises(BackupError, match="not a usable mtrtk database"):
        restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert list(dst.iterdir()) == []


def test_restore_refuses_a_newer_archive_format(tmp_path: Path) -> None:
    archive = _tar_with(tmp_path / "x.tar.gz", {"manifest.json": _manifest(format=2)})
    dst = tmp_path / "d"
    with pytest.raises(BackupError, match="newer mtrtk"):
        restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert not dst.exists() or list(dst.iterdir()) == []


async def test_restore_removes_a_stale_wal_left_without_its_database(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    await seed(src, "roof")
    archive = create_backup(
        Settings(_env_file=None, data_dir=src, ntrip_password="x"), tmp_path / "b.tar.gz"
    )
    # Another database's un-checkpointed WAL, as a crash or a hand `rm mtrtk.db` leaves it.
    old = tmp_path / "old"
    old.mkdir()
    await seed(old, "field")
    live = Database(old / "mtrtk.db")
    await live.open()
    try:
        await SitesRepo(live).add(Site.from_ecef("pier", 4.0, 5.0, 6.0, source="manual"))
        dst = tmp_path / "dst"
        dst.mkdir()
        for suffix in ("-wal", "-shm"):
            stray = old / f"mtrtk.db{suffix}"
            assert stray.exists()
            (dst / f"mtrtk.db{suffix}").write_bytes(stray.read_bytes())
    finally:
        await live.close()
    restore_backup(archive, Settings(_env_file=None, data_dir=dst, ntrip_password="x"))
    assert not (dst / "mtrtk.db-shm").exists()
    assert not (dst / "mtrtk.db-wal").exists()
    assert await site_names(dst / "mtrtk.db") == ["roof"]


def test_backup_refuses_when_there_is_nothing_to_back_up(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "nodata",
        ntrip_password="x",
        mtrtk_env_file=tmp_path / "missing.env",
    )
    with pytest.raises(BackupError) as caught:
        create_backup(settings, tmp_path / "b.tar.gz")
    assert str(tmp_path / "nodata") in str(caught.value)
    assert str(tmp_path / "missing.env") in str(caught.value)
    assert not (tmp_path / "b.tar.gz").exists()


def test_cli_backup_refuses_an_empty_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mtrtk backup` from $HOME or cron, where neither DATA_DIR nor .env resolves."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.delenv("MTRTK_ENV_FILE", raising=False)
    r = CliRunner().invoke(main, ["backup", "--out", str(tmp_path / "e.tar.gz")])
    assert r.exit_code != 0
    assert "nothing to back up" in r.output and "Traceback" not in r.output
    assert not (tmp_path / "e.tar.gz").exists()


def test_cli_backup_without_a_database_says_what_it_archived(cli_env: Path, tmp_path: Path) -> None:
    r = CliRunner().invoke(main, ["backup", "--out", str(tmp_path / "b.tar.gz")])
    assert r.exit_code == 0, r.output
    assert f"note: no database at {cli_env / 'mtrtk.db'}; only .env archived" in r.output
    assert os.environ["MTRTK_ENV_FILE"] in r.output


async def test_cli_backup_without_an_env_does_not_claim_one(cli_env: Path, tmp_path: Path) -> None:
    await seed(cli_env)
    remove_file(os.environ["MTRTK_ENV_FILE"])
    r = CliRunner().invoke(main, ["backup", "--out", str(tmp_path / "b.tar.gz")])
    assert r.exit_code == 0, r.output
    assert "only .env archived" not in r.output
    assert f"note: no .env at {os.environ['MTRTK_ENV_FILE']}" in r.output


def test_cli_backup_reports_an_unwritable_destination(cli_env: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    r = CliRunner().invoke(main, ["backup", "--out", str(blocker / "b.tar.gz")])
    assert r.exit_code != 0 and "Traceback" not in r.output and str(blocker) in r.output


async def test_cli_restore_of_a_backup_without_an_env(cli_env: Path, tmp_path: Path) -> None:
    """An archive whose env member is empty holds no configuration: it 'matches' nothing."""
    src = tmp_path / "src"
    src.mkdir()
    await seed(src)
    archive = create_backup(
        Settings(
            _env_file=None, data_dir=src, ntrip_password="x", mtrtk_env_file=tmp_path / "no.env"
        ),
        tmp_path / "b.tar.gz",
    )
    assert member_text(archive, "env") == ""
    r = CliRunner().invoke(main, ["restore", str(archive)])
    assert r.exit_code == 0, r.output
    assert "the backup holds no .env" in r.output and "it matches" not in r.output
    assert not (cli_env / "restored.env").exists()


async def test_restore_keeps_an_earlier_restored_env(tmp_path: Path) -> None:
    """A second restore must not overwrite the only copy of the first one's (secret) env."""
    first_env = tmp_path / "first.env"
    first_env.write_text("NTRIP_PASSWORD=firstsecret\n")
    src = tmp_path / "src"
    src.mkdir()
    await seed(src)
    first = create_backup(
        Settings(_env_file=None, data_dir=src, ntrip_password="x", mtrtk_env_file=first_env),
        tmp_path / "first.tar.gz",
        with_secrets=True,
    )
    second_env = tmp_path / "second.env"
    second_env.write_text("STATION_ID=ZZZZ\n")
    env_only = create_backup(  # made on a host with no database
        Settings(
            _env_file=None,
            data_dir=tmp_path / "empty",
            ntrip_password="x",
            mtrtk_env_file=second_env,
        ),
        tmp_path / "second.tar.gz",
    )
    dst = tmp_path / "dst"
    settings = Settings(_env_file=None, data_dir=dst, ntrip_password="x")
    restore_backup(first, settings)
    restore_backup(env_only, settings)  # no database in it, so no --force needed
    assert "STATION_ID=ZZZZ" in file_text(str(dst / "restored.env"))
    kept = list(dst.glob("restored.env.pre-restore-*"))
    assert len(kept) == 1 and "firstsecret" in kept[0].read_text()
    assert file_mode(str(kept[0])) == 0o600
    assert len(list(dst.glob("restored-sites.json.pre-restore-*"))) == 1
    restore_backup(env_only, settings)  # the same again: nothing new worth keeping
    assert len(list(dst.glob("restored.env.pre-restore-*"))) == 1
