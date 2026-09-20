"""Time-series history for charts: the 1 s samples and the 1 m rollups behind one query.

One route answers both tables. The caller names metrics in the 1 s vocabulary it already knows
from `/api/state` (`h_acc_m`, `nsat_used`), picks a window, and either states a resolution or
lets `res=auto` choose - so a UI can widen a chart from ten minutes to a month without knowing
that the rollup renamed every column on the way in.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from mtrtk.store.sampler import SAMPLE_COLUMNS, SampleReader

router = APIRouter(prefix="/api/history", tags=["history"])

Resolution = Literal["1s", "1m"]

# Six hours of 1 s rows is 21 600 points - about as many as a chart can draw before the JSON,
# not the query, becomes the slow part. Past that `auto` moves to the minute series.
AUTO_1M_THRESHOLD = timedelta(hours=6)
TABLE: dict[str, str] = {"1s": "samples_1s", "1m": "samples_1m"}
STEP_S: dict[str, float] = {"1s": 1.0, "1m": 60.0}
# One window cap per resolution, each one the horizon the sampler's retention keeps
# (`keep_1s_h=24`, `keep_1m_d=90`): asking for more can only ever return less.
MAX_WINDOW: dict[str, timedelta] = {"1s": timedelta(hours=24), "1m": timedelta(days=90)}
WINDOW_CAP: dict[str, str] = {"1s": "24 hours", "1m": "90 days"}
WIDER = " (res=1m covers up to 90 days)"

# `samples_1m` renames every metric as it aggregates it, and not always to `<metric>_avg`: the
# unit suffix goes (`h_acc_m` -> `h_acc_avg`), and a metric the rollup only ever takes one
# aggregate of keeps that one (`jam_ind` -> `jam_ind_max`, `disk_free_gb` -> `disk_free_gb_min`).
# Written out rather than derived: the pairings exist only inside the rollup's SQL, and a suffix
# rule would have to drop `_per_s` from `rtcm_bytes_per_s` while keeping `_per_ms` in
# `noise_per_ms_avg`. This maps a 1 s name to the *representative* 1 m column; the others
# (`h_acc_max`, `nsat_used_min`, `pdop_max`, `n`) are asked for by their own names.
MINUTE_ALIASES: dict[str, str] = {
    "lat": "lat_avg",
    "lon": "lon_avg",
    "height_m": "height_avg",
    "h_acc_m": "h_acc_avg",
    "v_acc_m": "v_acc_avg",
    "fix_type": "fix_type_min",
    "carr_soln": "carr_soln_min",
    "nsat_used": "nsat_used_avg",
    "nsat_tracked": "nsat_tracked_avg",
    "pdop": "pdop_avg",
    "cno_mean": "cno_mean_avg",
    "jam_ind": "jam_ind_max",
    "agc_cnt": "agc_cnt_avg",
    "noise_per_ms": "noise_per_ms_avg",
    "corr_age_s": "corr_age_max",
    "baseline_m": "baseline_avg",
    "rtcm_bytes_per_s": "rtcm_bytes_avg",
    "ntrip_clients": "ntrip_clients_max",
    "cpu_pct": "cpu_pct_avg",
    "mem_pct": "mem_pct_avg",
    "disk_free_gb": "disk_free_gb_min",
    "temp_c": "temp_c_max",
}
# `hmsl_m`, `hdop` and `vdop` are deliberately absent: the rollup takes no aggregate of them, so
# at `1m` they are unknown metrics rather than silently something else.

# Declared so the client Phase 4 generates carries them; FastAPI infers only the 2xx and its own
# validation 422 (which is what `res` outside auto|1s|1m raises).
HISTORY_ERRORS: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            "from/to are not ISO-8601 instants with a timezone, are out of order, span more "
            "than the resolution's cap, or a metric is not a column of the chosen table"
        )
    },
}


def _reader(request: Request) -> SampleReader:
    """The app's one history reader, built on first use; it caches each table's column names.

    Deliberately not a `Sampler`: that subscribes to the bus in its constructor, and one
    subscription per chart refresh is exactly what the lifespan-owned-subscriber rule exists to
    prevent. A reader holds only the connection, so it needs no place in the lifespan either.
    """
    reader = getattr(request.app.state, "history_reader", None)
    if reader is None:
        reader = SampleReader(request.app.state.ctx.db)
        request.app.state.history_reader = reader
    return reader


def _auto(window: timedelta) -> Resolution:
    return "1m" if window > AUTO_1M_THRESHOLD else "1s"


def _instant(value: str, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{label} must be ISO-8601") from exc
    if dt.tzinfo is None:
        # Samples are dated in receiver UTC; a naive instant would silently mean local time.
        raise HTTPException(422, f"{label} must include a timezone (use Z or +00:00)")
    return dt


def _window(
    from_: str, to: str, res: Literal["auto", "1s", "1m"]
) -> tuple[datetime, datetime, Resolution]:
    """The window and the resolution that will answer it, or a 422 saying which is wrong.

    The cap is applied to the resolution actually chosen, not the one asked for: `res=auto` over
    a year picks `1m` and is refused against the 90-day cap, never against the 24-hour one.
    """
    start, end = _instant(from_, "from"), _instant(to, "to")
    if start >= end:
        raise HTTPException(422, "from must be before to")
    window = end - start
    resolution: Resolution = res if res != "auto" else _auto(window)
    if window > MAX_WINDOW[resolution]:
        raise HTTPException(
            422,
            f"that range is {window}; at res={resolution} ask for at most "
            f"{WINDOW_CAP[resolution]} per request{WIDER if resolution == '1s' else ''}",
        )
    return start, end, resolution


def _resolve(metric: str, resolution: Resolution, known: frozenset[str]) -> str | None:
    """The column that answers `metric` at this resolution, or None if nothing does."""
    if metric in known:
        return metric
    if resolution == "1m":
        alias = MINUTE_ALIASES.get(metric) or f"{metric}_avg"
        if alias in known:
            return alias
    return None


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, list[str]]:
    """Every metric a chart may ask for, per resolution.

    `ts` is always the first column of an answer, so it is not listed as something to ask for.
    A 1 s name that `/api/history` maps for you (`h_acc_m` -> `h_acc_avg`) appears only in the
    `1s` list; the `1m` list is the rollup's own columns, which is what a UI needs to offer
    `h_acc_max` or `nsat_used_min` as series of their own.
    """
    minute = await _reader(request).columns("samples_1m")
    return {
        "1s": [c for c in SAMPLE_COLUMNS if c != "ts"],
        "1m": [c for c in minute if c != "ts"],
    }


@router.get("", responses=HISTORY_ERRORS)
async def history(
    request: Request,
    metrics_csv: str = Query(alias="metrics", description="comma-separated metric names"),
    from_: str = Query(alias="from", description="ISO-8601 instant with a timezone, inclusive"),
    to: str = Query(description="ISO-8601 instant with a timezone, exclusive"),
    res: Literal["auto", "1s", "1m"] = Query("auto", description="1m above a six-hour window"),
) -> dict[str, Any]:
    """Columns-and-rows rather than a list of objects: a day of 1 s samples is 86 400 rows, and
    repeating the metric names on every one of them would double the response for nothing.
    """
    start, end, resolution = _window(from_, to, res)
    known = frozenset(await _reader(request).columns(TABLE[resolution]))
    columns: list[str] = []  # the rollup's own column names, for the SQL
    names: list[str] = []  # the caller's spellings, echoed back so a client never guesses `_avg`
    unknown: list[str] = []
    for metric in _wanted(metrics_csv):
        resolved = _resolve(metric, resolution, known)
        if resolved is None:
            unknown.append(metric)
        elif resolved not in columns:  # two spellings of one column are still one column
            columns.append(resolved)
            names.append(metric)
    if unknown:
        # At `1m` the 1 s names this route maps for the caller are allowed too, so say so.
        mapped = set(MINUTE_ALIASES) if resolution == "1m" else set()
        allowed = sorted((known - {"ts"}) | mapped)
        raise HTTPException(
            422,
            f"unknown metrics for res={resolution}: {', '.join(unknown)}; "
            f"allowed: {', '.join(allowed)}",
        )
    if not columns:
        raise HTTPException(422, "metrics is empty")
    # The row cap is the window's own natural count: one row per second or per minute. The
    # window caps above make it finite, and `ts` being the primary key makes it exact.
    rows = await _reader(request).history(
        TABLE[resolution],
        start.timestamp(),
        end.timestamp(),
        ["ts", *columns],
        limit=math.ceil((end - start).total_seconds() / STEP_S[resolution]),
    )
    return {
        "res": resolution,
        "columns": ["ts", *names],
        "rows": [[r["ts"], *(r[c] for c in columns)] for r in rows],
    }


def _wanted(metrics_csv: str) -> list[str]:
    """The metric names asked for, `ts` dropped: it is always the answer's first column."""
    return [m for m in (part.strip() for part in metrics_csv.split(",")) if m and m != "ts"]
