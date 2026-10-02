# PPK (post-processed kinematic)

Use PPK when the rover had no live corrections (no network, a long baseline) or when you want the
best track after the fact. The inputs are the rover's raw UBX (a rover always logs it) and the
base's raw UBX for the same time range. The output is an RTKLIB track with a quality for every
epoch, plus a position for every camera pulse (TIM-TM2).

mtrtk runs RTKLIB's `convbin` (UBX to RINEX) and `rnx2rtkp` (the kinematic solution) on the
host that runs the job. The Docker image ships RTKLIB demo5, which resolves ambiguities better.
A stock RTKLIB 2.4.3 (`apt install rtklib`) also works, with fewer options; the PPK page shows
which build it found.

## From the UI

The PPK page has a form, the list of PPK jobs, and the result of the job you pick.

1. **Rover.** Choose one of three sources:
   - **Session**: a field session's time range over this host's raw logs. Only a rover has
     sessions.
   - **Window**: any UTC range of this host's raw logs, up to 7 days.
   - **Upload**: a raw UBX from any receiver (an F9P, or an INS's GNSS port) or a RINEX
     observation file. Uploads can be up to 2 GB.
2. **Base.** Choose one of three sources:
   - **Remote base**: an mtrtk base over Tailscale. Its address is guessed from this rover's
     `NTRIP_URL` (`http://<caster host>:8080`). If the base has a `WEB_PASSWORD`, enter it; it
     is used for this run only and is not stored.
   - **Upload**: a raw UBX, or RINEX observations with an optional RINEX navigation file.
   - **Local logs**: this host's own raw logs, starting an hour before the window so the
     ephemerides are valid at its start.
3. **Base position.** Choose one:
   - **Automatic.** A base's raw logs record the site each hour was logged at, and that site
     is used: a base moved and re-sited since is placed where it stood during the window, not
     where it stands now (the warnings say so when the two differ). Hours logged at two sites
     are refused: the base moved inside the window. Logs that record no site (survey-in, or an
     older mtrtk) fall back to the active site, of the remote base or of this host. An
     uploaded RINEX uses its `APPROX POSITION XYZ`, which is often only approximate. The
     site used is in `summary.json` (`inputs.base_xyz_source`, and `inputs.base.logged_site`).
   - **Site**: one of this host's sites.
   - **Manual XYZ**: ECEF metres. Latitude and longitude typed here are refused.
4. **Options.** Camera events, QZSS, and the elevation mask (sent as the rnx2rtkp override
   `pos1-elmask`).
5. **Run PPK.** The job appears in the list with its progress. When it is done, press **View**.

The result shows:
- the track on a map, coloured by quality: fixed green, float amber, DGPS/SBAS orange, single
  red (PPP, which a kinematic run does not produce, teal), the same colours as the strip;
- the quality strip, one cell per epoch;
- the statistics and the warnings;
- the first 200 camera events;
- every output file to download.

If the daemon refuses a request, the page shows its reason as sent, field by field.

## From the CLI

```bash
mtrtk ppk --session 3 --base-url http://100.100.50.10:8080 --out ./ppk-2026-09-19
mtrtk ppk --from 2026-09-19T08:00Z --to 2026-09-19T09:30Z --base-logs --site roof --out ./ppk
mtrtk ppk --rover flight.ubx --base base.ubx --base-xyz -26748.172 5837156.618 2561801.261 --out ./ppk
mtrtk ppk --rover flight.ubx --base base.24o --base-nav base.24n --out ./ppk
```

With `--base-logs` and neither `--site` nor `--base-xyz`, the base position is chosen as in
**Automatic** above: the site the hours were logged at, else this host's active site.

`--set key=value` overrides an rnx2rtkp option; repeat it for several options. The output
layout options (`out-solformat`, `out-timesys`, `out-timeform`, `out-degform`, `out-outhead`,
`out-height`, `out-fieldsep`) cannot be overridden, because the track and the events are read
from that layout. `--base-password` (or `MTRTK_BASE_PASSWORD`) is the remote base's
`WEB_PASSWORD`.

## From the API

| Route | What it does |
|---|---|
| `GET /api/ppk/defaults` | What the host can run (`rnx2rtkp`, `convbin`, `demo5`), the option file a job starts from (`conf`), the remote base's guessed address (`ntrip_base_url`), and `max_upload_bytes`. |
| `POST /api/ppk/upload` | A multipart form with `kind` (`rover` or `base`) and then `file`. The file is streamed to `DATA_DIR/uploads/<upload_id>/<name>`. Answers `{upload_id, name, bytes, detected: "ubx" \| "rinex", rinex: "obs" \| "nav" \| null, kind}`. A file that is neither UBX nor RINEX gets 422, and so does a gzip or Hatanaka-compressed one. Over 2 GB gets 413. 409 if it would leave less than half of `MIN_FREE_GB` free (as for an export: retention keeps a full card right at `MIN_FREE_GB`, and prunes the oldest raw hours back to it after the upload). Uploads are removed after 7 days. |
| `POST /api/ppk` | Queues a job of kind `ppk` and answers with its job row. The body is `{rover: {kind, session_id?, start?, end?, upload_id?}, base: {kind, url?, password?, upload_id?, nav_upload_id?}, base_site?, base_xyz?, events, include_qzss, conf_overrides}`. 404 for an unknown upload, or for a window that no raw log covers. 409 if there is no job runner. 422 for a source missing what it needs, a window longer than 7 days, a navigation file next to a raw (UBX) base, both `base_site` and `base_xyz`, coordinates that are not ECEF, a pinned option override, or a `file-*` option (those name host files; only the CLI takes them). |

Results come through the jobs routes:
- `GET /api/jobs/{id}`: its `result` is `summary.json`.
- `GET /api/jobs/{id}/files` and `GET /api/jobs/{id}/files/{name}`: the output files.

## Outputs

| File | Contents |
|---|---|
| `track.pos` | The rnx2rtkp solution: llh in degrees, GPST. |
| `track.csv` | One row per epoch, with `q` (1 fixed, 2 float, 4 DGPS, 5 single) and the sigmas. |
| `track.geojson` | One LineString for each same-quality run, plus a Point every 10 epochs. Its times (`start`, `end`, `time`) are GPST with no UTC offset, as the collection's `time_system` says. |
| `track.kml` | The same runs, coloured by quality, for Google Earth. |
| `events.csv` | One row per camera pulse (see below). |
| `events.geojson` | The placed camera pulses. |
| `summary.json` | Epochs, fixed / float / single %, the mean σ of the fixed epochs, gaps, warnings and the inputs used. `first_time`, `last_time` and the gaps are GPST with no UTC offset (`time_system: "GPST"`), 18 s ahead of UTC. |
| `ppk.conf` | The exact option file rnx2rtkp ran with. |
| `rnx2rtkp.log` | The command line and what rnx2rtkp printed. |
| `rover.rnx`, `base.rnx` (and `_MN.rnx`) | The RINEX observation and navigation files the run used. |
| `track_events.pos` | demo5 rnx2rtkp (the image's) only: its own solution at the event marks. mtrtk's `events.csv` does not use it. |

Heights are ellipsoidal (WGS 84), as `out-height=ellipsoidal` pins them, in every file. The
KML is drawn clamped to the ground, since Google Earth reads an absolute altitude as height
above mean sea level (about 50 m off in Bangladesh). The positions are of the antenna
reference point (ARP): the rover's `ANTENNA_HEIGHT_M` goes into the RINEX header only, and
rnx2rtkp is not told to reduce either side to a pole tip or a marker. If a base site is a
marker position (for example a PPP solution reduced to the mark), the track is off by the base
antenna's height; give the base's ARP instead.

Disk space. A job holds about 2.1 times the raw window on each side (the spliced copy and its
RINEX) plus the solution, on the card the raw logs live on. Before it splices, and again before
the base side, it checks that this fits and still leaves half of `MIN_FREE_GB` free; a remote
base is checked as it arrives. Otherwise it fails with "not enough free space": process a
shorter window, or delete old jobs. Its scratch counts as temporary for retention, and one left
by a power cut is removed when the daemon next starts. Parsing the solution and writing the
track run in a separate process, so a very long track can fail its job but not the daemon.

## Geotagging photos

`events.csv` lists each EXTINT pulse with its GPS week, time of week, GPST and UTC time, and its
position. The position is interpolated between the two solution epochs on either side of the
pulse. Each row has a `status`:
- `ok`: the pulse was placed.
- `gap_too_large`: the epochs on either side are more than 2 s apart.
- `no_neighbours`: the pulse is outside the track.

Match the pulse `count` to the order of the images. `q` and `sdn_m/sde_m/sdu_m` are taken from
the worse of the two neighbouring epochs.

## Reading the result

- `fixed_pct` near 100 with a `mean_sd_fixed` of a few millimetres is a good result.
- A mostly float result has one of these causes:
  - a long baseline;
  - a poor sky view;
  - base and rover windows that barely overlap (check `gaps` and the warnings).
- "base position unknown": give a site or `base_xyz`, or make sure the base has an active site.
- If the base and the rover are the same receiver's logs, the result is a zero baseline against
  itself, and the warnings say so. RTKLIB keeps such a solution at float with millimetre
  sigmas. With identical observations every double-difference ambiguity is exactly zero, and
  rnx2rtkp treats a zero state as "not yet estimated", so it never tries to fix it. This
  self-test proves the pipeline works, not the fix rate, so no "check baseline length" warning
  is added for it. To test the fix rate, use two
  receivers on one antenna (a splitter) or a real baseline.
- A short span can make the combined (forward + backward) solution come out empty. The job then
  uses the forward-only solution and says so in its warnings.
