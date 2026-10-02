# Contributing to mtrtk

## Development setup

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm 11 for the web
UI, and RTKLIB (`convbin`, `rnx2rtkp`) for the export and PPK tests (`apt install rtklib`, or
the demo5 build `docker/Dockerfile` pins). The receiver is optional: the committed UBX fixtures
replay through the whole daemon and UI.

```bash
uv sync
pnpm --dir web install
uv run mtrtk replay tests/fixtures/f9p_hpg113_raw_10s.ubx --speed 10 --loop
```

`pnpm --dir web dev` serves the UI with hot reload; `pnpm --dir web build:static` copies the
built SPA into `src/mtrtk/web/static`, which the daemon serves.

## Tests and checks

CI (`.github/workflows/ci.yml`) runs all of these, and a change should pass them locally first:

```bash
uv lock --check
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
pnpm --dir web build && pnpm --dir web test && pnpm --dir web lint
```

- Tests that need RTKLIB skip when it is missing; `MTRTK_REQUIRE_CONVBIN=1` makes them fail
  instead (CI sets it).
- `tests/hardware/` talks to a live receiver and reconfigures it. Those tests are marked
  `hardware` and excluded by default; `MTRTK_TEST_PORT=/dev/ttyACM0 uv run pytest -m hardware
  tests/hardware` runs them on that port (the base and F9P tests otherwise use the first u-blox
  device found; the rover test, which reflashes the receiver, needs the variable). Never point
  them at a receiver that is serving a station.
- A backend route change needs `uv run python web/scripts/gen_openapi_snapshot.py`; the
  snapshot test fails until the committed schema matches.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
  `docs:`, `ci:`, `chore:` ...).

## Specs and plans

The design is `docs/superpowers/specs/2026-09-18-mtrtk-design.md`; it is binding. Each phase is
built from a plan in `docs/superpowers/plans/` (see its `README.md` for the order): TDD tasks
with their interfaces, tests and commit messages. A plan is written against the interfaces that
earlier phases planned; before executing one, compare it with the code that exists and record
any drift. A change of behaviour starts with the spec, not the plan.

## Releasing

Versions follow [SemVer](https://semver.org/). One version is written in seven places
(`pyproject.toml`, `uv.lock`, `src/mtrtk/__init__.py`, `web/package.json`, both ROS 2
`package.xml` files and `ros2/mtrtk_bridge/setup.py`); `tests/unit/test_version_sync.py` fails if
they disagree. Never edit them by hand:

1. Put the changes under `## [Unreleased]` in `CHANGELOG.md`
   ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) sections: Added, Changed, Fixed ...).
2. `uv run python scripts/bump-version.py X.Y.Z` sets every version source and moves the
   Unreleased entries under `## [X.Y.Z] - <today>` (`--date YYYY-MM-DD` to choose the date). It
   checks everything before writing anything and refuses a version already in the changelog or
   an empty Unreleased section.
3. Review the diff, run the checks above, then
   `git commit -am "chore: release vX.Y.Z" && git tag vX.Y.Z && git push && git push origin vX.Y.Z`.

The `vX.Y.Z` tag runs `.github/workflows/release.yml`:

- **verify**: the tag must equal `v` + `mtrtk.__version__`, `CHANGELOG.md` must have entries
  under `## [X.Y.Z]`, and the unit tests must pass.
- **images**: each image is built for amd64 and smoke-tested (the mtrtk image on arm64 too, under
  qemu), then pushed for `linux/amd64` and `linux/arm64`:
  - `ghcr.io/<owner>/mtrtk:X.Y.Z` and `:latest`
  - `ghcr.io/<owner>/mtrtk-ros2:X.Y.Z-humble`, `:X.Y.Z-jazzy`, `:humble` and `:jazzy`
- **github-release**: a GitHub Release named after the tag, with the changelog section as its
  notes and `mtrtk-X.Y.Z-docs.tar.gz` (docs, README, changelog, licence) attached.

Every push to `main` also publishes `ghcr.io/<owner>/mtrtk:edge` from `ci.yml`, after the tests
pass. So `latest` is the newest release, `X.Y.Z` pins one, and `edge` tracks `main`. Only plain
`vX.Y.Z` tags release; the per-phase tags (`v0.7.0-phase7` ...) do not. The workflow uses the
repository's `GITHUB_TOKEN` (`packages: write`); a first push creates the ghcr packages as
private, so make them public in the package settings if hosts pull without logging in.
