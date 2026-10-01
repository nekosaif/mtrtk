"""Every `VERIFY` marker in the INS drivers carries a short tag that docs/ins-drivers.md lists.

A marker is the word VERIFY in a comment or docstring under `src/mtrtk/rover/drivers/`, written
`VERIFY(<tag>)`: an assumption the code makes that only a real unit can confirm. The tag is the
row id in the verification matrix, so a new assumption cannot land without a row that says how
to check it and what happens when it is wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVERS = ROOT / "src" / "mtrtk" / "rover" / "drivers"
DOC = ROOT / "docs" / "ins-drivers.md"
MARKER = re.compile(r"\bVERIFY\b(?:\((?P<tag>[a-z0-9]+(?:-[a-z0-9]+)*)\))?")


def markers() -> list[tuple[str, int, str | None]]:
    found = []
    for path in sorted(DRIVERS.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in MARKER.finditer(line):
                found.append((str(path.relative_to(ROOT)), lineno, m.group("tag")))
    return found


def test_the_scan_sees_the_drivers_markers() -> None:
    tags = {tag for _, _, tag in markers()}
    # Both vendors keep assumptions only hardware can settle; an empty scan means a broken grep.
    assert any(t and t.startswith("sbg-") for t in tags)
    assert any(t and t.startswith("vn-") for t in tags)


def test_every_marker_has_a_short_tag() -> None:
    untagged = [f"{path}:{lineno}" for path, lineno, tag in markers() if tag is None]
    assert not untagged, f"write VERIFY(<tag>) and add the tag to docs/{DOC.name}: {untagged}"


def test_every_tag_is_in_the_verification_matrix() -> None:
    doc = DOC.read_text(encoding="utf-8")
    missing = sorted(
        f"{tag} ({path}:{lineno})"
        for path, lineno, tag in markers()
        if tag and f"`{tag}`" not in doc
    )
    assert not missing, f"add a row for each to the matrix in docs/{DOC.name}: {missing}"
