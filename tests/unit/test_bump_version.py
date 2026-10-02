"""`scripts/bump-version.py`, run against a copy of the repository's version sources.

The version sources are copied from the repository so the patterns are tested against the real
files, but the changelog is synthetic: the repository's own `CHANGELOG.md` has an empty
Unreleased section on every release commit, and these tests must pass on that commit too.
"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bump-version.py"
VERSION_SOURCES = (
    "pyproject.toml",
    "uv.lock",
    "src/mtrtk/__init__.py",
    "web/package.json",
    "ros2/mtrtk_msgs/package.xml",
    "ros2/mtrtk_bridge/package.xml",
    "ros2/mtrtk_bridge/setup.py",
)
SOURCES = (*VERSION_SOURCES, "CHANGELOG.md")
CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

### Added

- something new

## [0.0.1] - 2000-01-01

### Added

- the first thing
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in VERSION_SOURCES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, tmp_path / rel)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
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


def add_unreleased_entry(tree: Path, entry: str) -> None:
    path = tree / "CHANGELOG.md"
    text = path.read_text()
    path.write_text(text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n- {entry}\n", 1))


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
    for rel in VERSION_SOURCES:
        current = before[rel]
        assert len(after[rel].splitlines()) == len(current.splitlines()), rel
        changed = [
            (a, b)
            for a, b in zip(current.splitlines(), after[rel].splitlines(), strict=True)
            if a != b
        ]
        assert len(changed) == 1, (rel, changed)
        assert "9.8.7" in changed[0][1], (rel, changed)


def test_a_second_release_after_new_entries_bumps_again(tree: Path) -> None:
    # The state bump-version.py leaves (an empty Unreleased) is the state of the tagged commit:
    # the next release starts from it once an entry is added.
    assert bump(tree, "9.8.7", "--date", "2031-02-03").returncode == 0
    add_unreleased_entry(tree, "a later fix")
    result = bump(tree, "9.8.8", "--date", "2031-03-04")
    assert result.returncode == 0, result.stderr
    after = snapshot(tree)
    assert '__version__ = "9.8.8"' in after["src/mtrtk/__init__.py"]
    assert after["CHANGELOG.md"].index("## [9.8.8] - 2031-03-04") < after["CHANGELOG.md"].index(
        "## [9.8.7] - 2031-02-03"
    )
    notes = bump(tree, "9.8.8", "--notes")
    assert notes.returncode == 0, notes.stderr
    assert notes.stdout == "- a later fix\n"


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3-rc1", "01.2.3x", "01.2.3", "1.02.3"])
def test_a_bad_version_changes_nothing(tree: Path, bad: str) -> None:
    before = snapshot(tree)
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
    # The sources are not at 0.0.1, so writing them before the changelog check would show.
    before = snapshot(tree)
    result = bump(tree, "0.0.1")
    assert result.returncode != 0
    assert "## [0.0.1]" in result.stderr
    assert snapshot(tree) == before


def test_an_empty_unreleased_section_is_refused(tree: Path) -> None:
    assert bump(tree, "9.8.7", "--date", "2031-02-03").returncode == 0
    before = snapshot(tree)
    result = bump(tree, "9.8.8")
    assert result.returncode != 0
    assert "Unreleased" in result.stderr
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    ("changelog", "message"),
    [
        ("# Changelog\n\n## [0.0.1] - 2000-01-01\n\n- old\n", "expected one '## [Unreleased]'"),
        (
            "# Changelog\n\n## [Unreleased]\n\n- a\n\n## [Unreleased]\n\n- b\n",
            "expected one '## [Unreleased]'",
        ),
        # Subheadings with no entries under them are not release notes.
        (
            "# Changelog\n\n## [Unreleased]\n\n### Added\n\n### Fixed\n\n## [0.0.1] - 2000-01-01\n",
            "the Unreleased section is empty",
        ),
    ],
    ids=["no-unreleased", "two-unreleased", "headings-only"],
)
def test_a_malformed_changelog_changes_nothing(tree: Path, changelog: str, message: str) -> None:
    (tree / "CHANGELOG.md").write_text(changelog)
    before = snapshot(tree)
    result = bump(tree, "9.8.7")
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert snapshot(tree) == before


@pytest.mark.parametrize("bad", ["2031-13-01", "tomorrow", ""])
def test_a_bad_date_is_refused_cleanly(tree: Path, bad: str) -> None:
    before = snapshot(tree)
    result = bump(tree, "9.8.7", "--date", bad)
    assert result.returncode != 0
    assert "--date" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert snapshot(tree) == before


def test_the_date_is_written_as_yyyy_mm_dd(tree: Path) -> None:
    result = bump(tree, "9.8.7", "--date", "20310203")
    assert result.returncode == 0, result.stderr
    assert "## [9.8.7] - 2031-02-03\n" in (tree / "CHANGELOG.md").read_text()


# --notes prints a released section's entries: release.yml's GitHub Release body.

NOTES_CHANGELOG = """\
# Changelog

## [Unreleased]

- not released yet

## [0.3.0] - 2031-03-01

### Added

- three

## [0.2.0] - 2031-02-01

## [0.1.0] - 2031-01-01

### Fixed

- one
- one more
"""


@pytest.mark.parametrize(
    ("version", "notes"),
    [
        ("0.3.0", "### Added\n\n- three\n"),
        ("0.1.0", "### Fixed\n\n- one\n- one more\n"),  # the last section in the file
    ],
)
def test_notes_print_the_section_without_its_heading(tree: Path, version: str, notes: str) -> None:
    (tree / "CHANGELOG.md").write_text(NOTES_CHANGELOG)
    before = snapshot(tree)
    result = bump(tree, version, "--notes")
    assert result.returncode == 0, result.stderr
    assert result.stdout == notes
    assert snapshot(tree) == before


@pytest.mark.parametrize(
    ("version", "message"),
    [("0.4.0", "no '## [0.4.0] - "), ("0.2.0", "no entries under '## [0.2.0]")],
    ids=["missing", "empty"],
)
def test_notes_refuse_a_missing_or_empty_section(tree: Path, version: str, message: str) -> None:
    (tree / "CHANGELOG.md").write_text(NOTES_CHANGELOG)
    result = bump(tree, version, "--notes")
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert result.stdout == ""
