"""Third-party code that runs with write tokens, or on an internet-facing host, is pinned
(security-7): actions to commit SHAs, compose images to a version and digest, uv's installer to a
version. Moving one is a deliberate edit, not whatever a tag points at today."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
KEEP_CREDENTIALS = {
    ("release.yml", "verify"),
    ("release.yml", "floating-tags"),
    ("ci.yml", "image"),
}


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow: Path) -> None:
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", workflow.read_text(), re.M)
    assert uses
    loose = [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u)]
    assert loose == [], loose


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_default_to_a_read_only_token(workflow: Path) -> None:
    assert re.search(r"^permissions:\s*\n\s+contents: read$", workflow.read_text(), re.M)


def test_checkouts_keep_no_credentials_unless_they_use_them() -> None:
    """Only release.yml's verify and floating-tags and ci.yml's image job (main's head) talk to
    the remote after checkout; every other job gets a checkout with no token in .git/config."""
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        for m in re.finditer(r"uses: actions/checkout@\S+[^\n]*\n(?P<next>[^\n]*)", text):
            job = re.findall(r"^  ([\w-]+):\s*$", text[: m.start()], re.M)[-1]
            if (workflow.name, job) in KEEP_CREDENTIALS:
                continue
            assert "persist-credentials: false" in m["next"], (workflow.name, job)


def test_a_release_tag_must_be_on_main() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in text
    assert "fetch-depth: 0" in text


def test_compose_images_are_pinned_to_a_version_and_digest() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    images = re.findall(r"^\s+image:\s*(\S+)", compose, re.M)
    third_party = [i for i in images if not i.startswith("ghcr.io/nekosaif/")]
    assert third_party
    for image in third_party:
        assert re.fullmatch(r"[\w./-]+:\d[\w.-]*@sha256:[0-9a-f]{64}", image), image


def test_the_uv_installer_is_versioned() -> None:
    text = (ROOT / "install.sh").read_text()
    assert "https://astral.sh/uv/install.sh" not in text
    assert re.search(r'^UV_VERSION="0\.9\.\d+"$', text, re.M)
    assert "https://astral.sh/uv/$UV_VERSION/install.sh" in text
