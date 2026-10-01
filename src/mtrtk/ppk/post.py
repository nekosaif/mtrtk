"""A PPK job's post-processing, in a child process: `python -m mtrtk.ppk.post ARGS RESULT`.

`pipeline._postprocess_in_child` writes ARGS (the output directory, the request, the rover's
raw data, the window) and reads RESULT (`{summary, events, warnings}`). Parsing a long track and
writing its files takes hundreds of MB and holds the GIL; out here, the worst it can do is fail
its own job, never the daemon that runs the caster or the NTRIP client.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from mtrtk.ppk.pipeline import PpkRequest, _postprocess, _read_pos


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m mtrtk.ppk.post ARGS.json RESULT.json", file=sys.stderr)
        return 2
    args = json.loads(Path(argv[0]).read_text())
    out = Path(args["out"])
    req = PpkRequest.model_validate(args["request"])
    window = None
    if args.get("window"):
        a, z = (datetime.fromisoformat(t) for t in args["window"])
        window = (a, z)
    rover_ubx = Path(args["rover_ubx"]) if args.get("rover_ubx") else None
    warnings: list[str] = []
    records = _read_pos(out / "track.pos")
    summary, events = _postprocess(
        out, records, req, rover_ubx, window, warnings, bool(args.get("zero_baseline"))
    )
    result = {"summary": summary, "events": events, "warnings": warnings}
    Path(argv[1]).write_text(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
