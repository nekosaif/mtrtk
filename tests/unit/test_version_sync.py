"""Every place the version is written down agrees, and a release has a changelog section to cut.

`scripts/bump-version.py X.Y.Z` rewrites all of them; `release.yml` refuses a tag that does not
match `mtrtk.__version__`. Each source is parsed here on its own terms (TOML, JSON, XML, and the
`setup()` call's keyword via `ast`), not with the bump script's patterns, so a pattern that
silently misses a file shows up as drift.
"""

import ast
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import mtrtk

ROOT = Path(__file__).resolve().parents[2]


def _setup_version(path: Path) -> str:
    calls = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup"
    ]
    assert len(calls) == 1, path
    (version,) = [kw.value for kw in calls[0].keywords if kw.arg == "version"]
    assert isinstance(version, ast.Constant), ast.dump(version)
    assert isinstance(version.value, str), version.value
    return version.value


def _package_xml_version(path: Path) -> str:
    version = ET.parse(path).getroot().findtext("version")
    assert version is not None, path
    return version.strip()


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
        "ros2/mtrtk_bridge/setup.py": _setup_version(ROOT / "ros2/mtrtk_bridge/setup.py"),
    }
    for pkg in ("mtrtk_msgs", "mtrtk_bridge"):
        versions[f"ros2/{pkg}/package.xml"] = _package_xml_version(
            ROOT / "ros2" / pkg / "package.xml"
        )
    return versions


def test_all_version_sources_agree() -> None:
    versions = version_sources()
    assert len(set(versions.values())) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", mtrtk.__version__), mtrtk.__version__


def test_changelog_has_unreleased_section() -> None:
    assert "\n## [Unreleased]\n" in (ROOT / "CHANGELOG.md").read_text()
