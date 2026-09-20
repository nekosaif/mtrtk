"""Write `web/src/lib/openapi.snapshot.json` from the daemon's own OpenAPI schema.

Run from the repository root:

    uv run python web/scripts/gen_openapi_snapshot.py

The SPA's contract test (`web/src/lib/contract.test.ts`) checks every route the TypeScript API
client references against this file, so a backend route that moves or a request body that
changes shape fails the frontend test suite instead of a page at runtime. Re-run this after any
change under `src/mtrtk/web/` and commit the result. Nothing here needs a receiver, a database
file or the network: the app is built against an inert context and only its route table is read.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.openapi.utils import get_openapi

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.statestore import StateStore
from mtrtk.store.db import Database
from mtrtk.web.app import create_app
from mtrtk.web.context import AppContext

OUT = Path(__file__).resolve().parents[1] / "src" / "lib" / "openapi.snapshot.json"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(_env_file=None, ntrip_password="pw", data_dir=root)  # type: ignore[call-arg]
        bus = Bus()
        ctx = AppContext(
            settings=settings,
            bus=bus,
            store=StateStore(bus),
            db=Database(root / "mtrtk.db"),  # never opened: only the route table is read
            daemon=SimpleNamespace(controller=None, caster=None, basemode=None, stop=None),
        )
        app = create_app(ctx, static_dir=root)
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    paths, version = len(schema["paths"]), schema["info"]["version"]
    print(f"wrote {OUT.relative_to(Path.cwd())}: {paths} paths, mtrtk {version}")


if __name__ == "__main__":
    main()
