#!/usr/bin/env python3
"""Set every version source to X.Y.Z and cut the changelog's Unreleased section.

    uv run python scripts/bump-version.py 0.2.0 [--date YYYY-MM-DD]

Rewrites `pyproject.toml`, `uv.lock` (the mtrtk entry, so `uv lock --check` stays green),
`src/mtrtk/__init__.py`, `web/package.json`, both ROS 2 `package.xml` files and the bridge's
`setup.py`, and moves the entries under `## [Unreleased]` in `CHANGELOG.md` to a new
`## [X.Y.Z] - YYYY-MM-DD` heading. Only the version strings change; every file keeps its
formatting. Every source is checked before anything is written, so a failed run changes nothing.
`tests/unit/test_version_sync.py` checks that the sources agree; `release.yml` refuses a tag
that does not match.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"\d+\.\d+\.\d+")

# (file, pattern): group 1 is the text before the version, group 2 the text after it. Each
# pattern must match exactly once.
SOURCES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", r'^(version = ")[^"]+(")'),
    ("uv.lock", r'^(\[\[package\]\]\nname = "mtrtk"\nversion = ")[^"]+(")'),
    ("src/mtrtk/__init__.py", r'^(__version__ = ")[^"]+(")'),
    ("web/package.json", r'^(  "version": ")[^"]+(")'),
    ("ros2/mtrtk_msgs/package.xml", r"^(\s*<version>)[^<]+(</version>)"),
    ("ros2/mtrtk_bridge/package.xml", r"^(\s*<version>)[^<]+(</version>)"),
    ("ros2/mtrtk_bridge/setup.py", r'^(\s*version=")[^"]+(")'),
)
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]\n"


def bump_sources(root: Path, version: str) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for rel, pattern in SOURCES:
        path = root / rel
        text = path.read_text()
        matches = len(re.findall(pattern, text, flags=re.M))
        if matches != 1:
            raise SystemExit(f"{rel}: expected one version to rewrite, found {matches}")
        out[path] = re.sub(pattern, rf"\g<1>{version}\g<2>", text, count=1, flags=re.M)
    return out


def cut_changelog(root: Path, version: str, date: str) -> dict[Path, str]:
    path = root / CHANGELOG
    text = path.read_text()
    if text.count(UNRELEASED) != 1:
        raise SystemExit(f"{CHANGELOG}: expected one '{UNRELEASED.strip()}' heading")
    if re.search(rf"^## \[{re.escape(version)}\]", text, flags=re.M):
        raise SystemExit(f"{CHANGELOG}: '## [{version}]' already exists")
    unreleased = text.split(UNRELEASED, 1)[1].split("\n## [", 1)[0]
    if not unreleased.strip():
        raise SystemExit(f"{CHANGELOG}: the Unreleased section is empty; nothing to release")
    heading = f"## [{version}] - {date}\n"
    return {path: text.replace(UNRELEASED, f"{UNRELEASED}\n{heading}", 1)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the new version, MAJOR.MINOR.PATCH")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="release date")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    version: str = args.version
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"version must be MAJOR.MINOR.PATCH (no 'v', no suffix), got {version!r}")
    dt.date.fromisoformat(args.date)
    root: Path = args.root
    writes = bump_sources(root, version) | cut_changelog(root, version, args.date)
    for path, text in writes.items():
        path.write_text(text)
    print(
        f"bumped to {version}. Review the diff, then:\n"
        f"  git commit -am 'chore: release v{version}' && git tag v{version}\n"
        f"  git push && git push origin v{version}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
