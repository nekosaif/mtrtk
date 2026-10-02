"""Static checks on the user docs: links and anchors resolve, the README stays a landing page,
and every replay line it shows names a recording the UI can actually draw.

Links and headings inside fenced code blocks are ignored; inline code is stripped before links
are matched, so a `[x](y)` written as an example inside backticks is not a link.
"""

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DOCS = sorted((ROOT / "docs").glob("*.md"))
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PAGES = [README, CONTRIBUTING, *DOCS]
README_MAX_LINES = 150
NAV_EOE = b"\xb5\x62\x01\x61"  # UBX NAV-EOE: the end-of-epoch marker the state store waits for

LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
INLINE_CODE = re.compile(r"`[^`\n]*`")
REPLAY = re.compile(r"mtrtk replay\s+(tests/fixtures/[^\s`]+\.ubx)")


def _prose_lines(text: str) -> list[str]:
    """The lines of a Markdown page outside fenced code blocks."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            out.append(line)
    return out


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link in a heading keeps its text
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    seen: Counter[str] = Counter()
    anchors: set[str] = set()
    for line in _prose_lines(page.read_text(encoding="utf-8")):
        m = HEADING.match(line)
        if not m:
            continue
        slug = _slug(m.group(2))
        anchors.add(slug if seen[slug] == 0 else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _links(page: Path) -> list[str]:
    links: list[str] = []
    for line in _prose_lines(page.read_text(encoding="utf-8")):
        links.extend(LINK.findall(INLINE_CODE.sub("", line)))
    return [link for link in links if not re.match(r"^[a-z][a-z0-9+.-]*:", link)]


def test_the_pages_exist() -> None:
    names = {page.name for page in DOCS}
    for name in ("setup.md", "hardware.md", "exposure.md", "firmware.md", "troubleshooting.md"):
        assert name in names


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(page: Path) -> None:
    bad: list[str] = []
    for link in _links(page):
        path, _, anchor = link.partition("#")
        target = (page.parent / path).resolve() if path else page
        if not target.exists():
            bad.append(f"{link}: {path} does not exist")
            continue
        if anchor and target.suffix == ".md" and anchor not in _anchors(target):
            bad.append(f"{link}: no heading #{anchor} in {target.relative_to(ROOT)}")
    assert not bad, f"{page.relative_to(ROOT)}: " + "; ".join(bad)


def test_slug_follows_github_rules() -> None:
    assert _slug("2. Clone and configure") == "2-clone-and-configure"
    assert _slug("Antenna type and height") == "antenna-type-and-height"
    assert _slug("`mtrtk doctor` / healthcheck") == "mtrtk-doctor--healthcheck"


def test_readme_is_a_landing_page() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= README_MAX_LINES, f"README.md has {len(lines)} lines"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_replay_lines_name_a_fixture_with_epochs(page: Path) -> None:
    """The 10 s / 60 s raw fixtures carry no NAV-EOE, so the UI would sit on "Waiting for data"."""
    for fixture in REPLAY.findall(page.read_text(encoding="utf-8")):
        path = ROOT / fixture
        assert path.is_file(), f"{page.name}: {fixture} does not exist"
        has_epochs = NAV_EOE in path.read_bytes()
        assert has_epochs, f"{page.name}: {fixture} has no UBX NAV-EOE frame"


def test_readme_shows_a_replay_line_that_runs_off_data() -> None:
    """The quick-start replay must not fall back to DATA_DIR=/data: a dev host cannot write it."""
    text = README.read_text(encoding="utf-8")
    m = REPLAY.search(text)
    assert m, "README.md shows no `mtrtk replay tests/fixtures/...` line"
    paragraph = text[text.rfind("\n\n", 0, m.start()) : m.start()]
    data_dir = re.search(r"DATA_DIR=(\S+)", paragraph)
    assert data_dir, "the README replay line leaves DATA_DIR at its /data default"
    assert data_dir.group(1).rstrip("/") != "/data"
