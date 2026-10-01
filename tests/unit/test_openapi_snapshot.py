"""The SPA's contract test reads a committed OpenAPI snapshot: it must be the live app's schema.

Otherwise a backend change that skipped the regenerate step leaves both suites green while the
UI calls a route shape that no longer exists.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "web" / "scripts" / "gen_openapi_snapshot.py"


def test_the_committed_openapi_snapshot_is_the_live_schema() -> None:
    spec = importlib.util.spec_from_file_location("gen_openapi_snapshot", SCRIPT)
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    committed = json.loads(gen.OUT.read_text())
    live = json.loads(gen.render(gen.build_schema()))
    # The version moves with every release; the contract is the rest.
    committed["info"].pop("version", None)
    live["info"].pop("version", None)
    assert live == committed, (
        "web/src/lib/openapi.snapshot.json is stale: "
        "run `uv run python web/scripts/gen_openapi_snapshot.py` and commit the result"
    )
