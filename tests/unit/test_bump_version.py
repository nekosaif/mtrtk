"""`scripts/bump-version.py`, run against a copy of the repository's version sources."""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bump-version.py"
SOURCES = (
    "pyproject.toml",
    "uv.lock",
    "src/mtrtk/__init__.py",
    "web/package.json",
    "ros2/mtrtk_msgs/package.xml",
    "ros2/mtrtk_bridge/package.xml",
    "ros2/mtrtk_bridge/setup.py",
    "CHANGELOG.md",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in SOURCES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, tmp_path / rel)
    return tmp_path


def bump(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tree), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def snapshot(tree: Path) -> dict[str, str]:
    return {rel: (tree / rel).read_text() for rel in SOURCES}


def test_bump_rewrites_every_source_and_cuts_the_changelog(tree: Path) -> None:
    before = snapshot(tree)
    result = bump(tree, "9.8.7", "--date", "2031-02-03")
    assert result.returncode == 0, result.stderr
    after = snapshot(tree)

    assert tomllib.loads(after["pyproject.toml"])["project"]["version"] == "9.8.7"
    lock = tomllib.loads(after["uv.lock"])
    assert [p["version"] for p in lock["package"] if p["name"] == "mtrtk"] == ["9.8.7"]
    assert '__version__ = "9.8.7"' in after["src/mtrtk/__init__.py"]
    assert '  "version": "9.8.7",' in after["web/package.json"]
    for pkg in ("mtrtk_msgs", "mtrtk_bridge"):
        assert "<version>9.8.7</version>" in after[f"ros2/{pkg}/package.xml"]
    assert 'version="9.8.7"' in after["ros2/mtrtk_bridge/setup.py"]

    changelog = after["CHANGELOG.md"]
    assert "## [Unreleased]\n\n## [9.8.7] - 2031-02-03\n" in changelog
    # The entries stay where they were and now sit under the release heading.
    assert changelog.replace("## [9.8.7] - 2031-02-03\n\n", "") == before["CHANGELOG.md"]

    # Only the version strings changed: formatting, other packages' versions, the rest intact.
    for rel in SOURCES[:-1]:
        current = before[rel]
        assert len(after[rel].splitlines()) == len(current.splitlines()), rel
        changed = [
            (a, b)
            for a, b in zip(current.splitlines(), after[rel].splitlines(), strict=True)
            if a != b
        ]
        assert len(changed) == 1, (rel, changed)
        assert "9.8.7" in changed[0][1], (rel, changed)


def test_a_bad_version_changes_nothing(tree: Path) -> None:
    before = snapshot(tree)
    for bad in ("1.2", "v1.2.3", "1.2.3-rc1", "01.2.3x"):
        result = bump(tree, bad)
        assert result.returncode != 0, bad
        assert "MAJOR.MINOR.PATCH" in result.stderr, (bad, result.stderr)
    assert snapshot(tree) == before


def test_a_missing_source_aborts_before_any_write(tree: Path) -> None:
    before = snapshot(tree)
    setup = tree / "ros2/mtrtk_bridge/setup.py"
    setup.write_text(setup.read_text().replace("version=", "verzion="))
    before["ros2/mtrtk_bridge/setup.py"] = setup.read_text()
    result = bump(tree, "9.8.7")
    assert result.returncode != 0
    assert "ros2/mtrtk_bridge/setup.py" in result.stderr
    assert snapshot(tree) == before


def test_a_version_already_in_the_changelog_is_refused(tree: Path) -> None:
    assert bump(tree, "9.8.7", "--date", "2031-02-03").returncode == 0
    before = snapshot(tree)
    result = bump(tree, "9.8.7")
    assert result.returncode != 0
    assert "## [9.8.7]" in result.stderr
    assert snapshot(tree) == before


def test_an_empty_unreleased_section_is_refused(tree: Path) -> None:
    assert bump(tree, "9.8.7", "--date", "2031-02-03").returncode == 0
    before = snapshot(tree)
    result = bump(tree, "9.8.8")
    assert result.returncode != 0
    assert "Unreleased" in result.stderr
    assert snapshot(tree) == before
