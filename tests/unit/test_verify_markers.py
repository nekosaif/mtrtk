"""Every `VERIFY` marker in the INS drivers carries a short tag that docs/ins-drivers.md lists.

A marker is the word VERIFY in a comment or docstring under `src/mtrtk/rover/drivers/` (and in
`src/mtrtk/config.py`, where the INS settings are), written `VERIFY(<tag>)`: an assumption the
code makes that only a real unit can confirm. The tag is the row id in the verification matrix
(the first cell of a row in its tables), so a new assumption cannot land without a row that says
how to check it and what happens when it is wrong. A mention elsewhere in the doc (wiring, the
checklist) does not count.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVERS = ROOT / "src" / "mtrtk" / "rover" / "drivers"
CONFIG = ROOT / "src" / "mtrtk" / "config.py"
DOC = ROOT / "docs" / "ins-drivers.md"
MATRIX_HEADING = "## Verified / unverified matrix"
MARKER = re.compile(r"\bVERIFY\b(?:\((?P<tag>[a-z0-9]+(?:-[a-z0-9]+)*)\))?")


def markers() -> list[tuple[str, int, str | None]]:
    found = []
    for path in [*sorted(DRIVERS.rglob("*.py")), CONFIG]:
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


def matrix_section(doc: str) -> str:
    """The matrix: from its heading to the next `## ` heading."""
    start = doc.index(MATRIX_HEADING)
    end = doc.find("\n## ", start + len(MATRIX_HEADING))
    return doc[start : end if end != -1 else len(doc)]


def has_row(matrix: str, tag: str) -> bool:
    return re.search(rf"^\| `{re.escape(tag)}` \|", matrix, re.M) is not None


def test_the_matrix_check_wants_a_row_not_a_mention() -> None:
    doc = "## Verified / unverified matrix\n\n| `a-b` | x |\nsee `c-d`\n\n## Checklist\n| `e-f` |\n"
    matrix = matrix_section(doc)
    assert has_row(matrix, "a-b")
    assert not has_row(matrix, "c-d")  # prose
    assert not has_row(matrix, "e-f")  # a table in another section


def test_every_tag_is_in_the_verification_matrix() -> None:
    matrix = matrix_section(DOC.read_text(encoding="utf-8"))
    missing = sorted(
        f"{tag} ({path}:{lineno})"
        for path, lineno, tag in markers()
        if tag and not has_row(matrix, tag)
    )
    assert not missing, f"add a row for each to the matrix in docs/{DOC.name}: {missing}"


def test_every_unverified_row_has_a_marker_in_the_code() -> None:
    """The other way round: an assumption still open names the code that makes it."""
    matrix = matrix_section(DOC.read_text(encoding="utf-8"))
    unverified = matrix[matrix.index("### Unverified until hardware") :]
    rows = re.findall(r"^\| `([a-z0-9-]+)` \|", unverified, re.M)
    assert rows, "the Unverified table has no rows: a broken heading or table?"
    tagged = {tag for _, _, tag in markers()}
    assert not sorted(set(rows) - tagged), "tag the code with VERIFY(<tag>) for each"
