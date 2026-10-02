"""Every place the version is written down agrees, and a release has a changelog section to cut.

`scripts/bump-version.py X.Y.Z` rewrites all of them; `release.yml` refuses a tag that does not
match `mtrtk.__version__`. Each source is parsed here on its own terms (TOML, JSON, XML), not
with the bump script's patterns, so a pattern that silently misses a file shows up as drift.
"""

import json
import re
import tomllib
from pathlib import Path

import mtrtk

ROOT = Path(__file__).resolve().parents[2]


def _tag(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    assert match is not None, pattern
    return match.group(1)


def version_sources() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (locked,) = [p["version"] for p in lock["package"] if p["name"] == "mtrtk"]
    versions = {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "version"
        ],
        "mtrtk.__version__": mtrtk.__version__,
        "web/package.json": json.loads((ROOT / "web/package.json").read_text())["version"],
        "uv.lock": locked,
        "ros2/mtrtk_bridge/setup.py": _tag(
            (ROOT / "ros2/mtrtk_bridge/setup.py").read_text(), r'\bversion="([^"]+)"'
        ),
    }
    for pkg in ("mtrtk_msgs", "mtrtk_bridge"):
        xml = (ROOT / "ros2" / pkg / "package.xml").read_text()
        versions[f"ros2/{pkg}/package.xml"] = _tag(xml, r"<version>([^<]+)</version>")
    return versions


def test_all_version_sources_agree() -> None:
    versions = version_sources()
    assert len(set(versions.values())) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", mtrtk.__version__), mtrtk.__version__


def test_changelog_has_unreleased_section() -> None:
    assert "\n## [Unreleased]\n" in (ROOT / "CHANGELOG.md").read_text()
