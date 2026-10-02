# Centimetre-accurate base coordinates with PPP

Survey-in gives the base a position good to about a metre. Rovers inherit that error: their
positions are centimetre-accurate *relative to the base* and off by whatever the base is off by.
Precise Point Positioning (PPP) services compute the antenna position to a few millimetres from
hours of raw data. mtrtk exports that data as RINEX for the service and reads the result back
into a site the base can broadcast.

> **Not yet verified on a real submission.** Everything below up to the upload is tested
> against real raw logs (a one-hour CSRS-PPP and OPUS export, 2026-10-01). No 24 h export has
> been submitted to CSRS-PPP, AUSPOS or OPUS yet. The CSRS-PPP importer **has** been checked
> against NRCan's own published sample results (static and kinematic, CSRS-PPP v3/v5.11,
> `tests/fixtures/ppp/csrs_v3_*`): the `.sum`, the `.pos` and the full-output `.zip` import
> correctly, and a kinematic result is refused with a request to resubmit in Static mode.
> The AUSPOS and OPUS importers are still built from reconstructed samples. Treat the
> service-side steps (what the upload form asks, what the e-mail contains) as expected rather
> than confirmed. Spec open item 5 tracks this.

## Before you start

- **Open sky, for the whole day.** PPP cannot fix a bad antenna site. An antenna indoors or next
  to a wall gives a survey-in sigma of 10 m or more and a PPP result no better. Mount it where it
  will stay, and do not touch it for the 24 h.
- **The base keeps running.** The raw log does not depend on the position mode: survey-in, fixed
  or off all log the same RXM-RAWX and RXM-SFRBX hours under `DATA_DIR/ubx/`.
- **Settings that end up in the files.** `STATION_ID` (4 letters or digits) and `COUNTRY`
  (ISO 3166 alpha-3, e.g. `BGD`) name the RINEX files; an export with a value that cannot name
  them is refused before any work. `MARKER_NAME`, `OBSERVER`, `AGENCY`, `ANTENNA_TYPE` and
  `ANTENNA_HEIGHT_M` go into the RINEX header. The receiver version comes from the firmware
  (`HPG 1.13`). The approximate position comes from the active site, if there is one; otherwise
  convbin computes it from the data. See *Antenna type and height* below before changing the
  last two.
- **Disk.** An export stages the spliced UBX and the observation file on the `DATA_DIR` card. It
  refuses to start when that would leave less than half of `MIN_FREE_GB` free (2.5 GB with the
  default 5), so live raw logging keeps its headroom. Retention deletes the oldest raw hours when
  free space falls below `MIN_FREE_GB`. Mark the day's hours *keep* on the Logs page if the card
  is tight. *Keep* protects the raw hours only: each time retention deletes an hour, it also
  deletes the finished export jobs whose window ended by that hour's end. So an export job can
  still be removed when space runs low, even with its raw hours kept. Download the result soon
  after it is made.

## 1. Collect 24 h

Let the base log 24 whole hours. The Site page's *Centimetre site from PPP* panel counts the hours
of raw data on disk in the last 24 h ("24 of 24 hours"), and the Logs page shows each hour on its
availability strip. The services take less (AUSPOS 1 h at least, CSRS-PPP a few hours), but a
day averages out the multipath and the satellite geometry.

## 2. Export

**Web UI.** Site page → step 1 → *Export the last 24 h for CSRS-PPP*. That opens Logs with the
*Export RINEX* panel set to the `csrs-ppp` preset and the last 24 whole hours (the hour still being
written is left out). Check the window, then *Start export*. The job runs in the background:
*Export jobs* shows its progress, and once it is done its files are downloads there.

**CLI.**

```bash
uv run mtrtk export --preset csrs-ppp \
    --from "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:00:00Z)" \
    --to   "$(date -u +%Y-%m-%dT%H:00:00Z)" \
    --out /tmp/csrs
```

This is the same window the UI picks: the last 24 whole hours, ending at the start of the hour
still being written.

`--from` and `--to` are ISO 8601 with a timezone. `--out` must not already hold this export's
files or a `manifest.json`; `--overwrite` replaces them. `mtrtk export` can run next to the
daemon. It reads the database read-only, and the firmware for the header comes from the raw logs'
sidecars.

**One export at a time.** Inside the daemon, export jobs queue and run one at a time: clicking
*Start export* twice gives two jobs, the second *queued* until the first has finished. The
synchronous zip download (`GET /api/export/rinex`) is refused (409) while an export job is queued
or running, and a new job is refused while a synchronous download runs. Between processes
(`mtrtk export` next to the daemon), every export also holds `DATA_DIR/.export.lock`, and the
second one is refused with "another export is running on this DATA_DIR ...". A web job that hits
the lock is still accepted and queued, then ends as *failed* with that message when it runs.
The synchronous download is limited to 6 h, so a 24 h export is always a job or the CLI.

**What you get** for a 24 h window starting 2026-10-01 06:00 UTC (day of year 274), station `MTRK`,
country `BGD`:

| Preset | Observation file | Navigation file | Format |
|---|---|---|---|
| `csrs-ppp` | `MTRK00BGD_R_20262740600_01D_30S_MO.crx.gz` | `MTRK00BGD_R_20262740600_01D_MN.rnx.gz` | RINEX 3.04, 30 s, all systems, Hatanaka + gzip |
| `auspos` | `MTRK00BGD_R_20262740600_01D_30S_MO.rnx.gz` | `MTRK00BGD_R_20262740600_01D_MN.rnx.gz` | RINEX 3.04, 30 s, all systems, gzip |
| `opus` | `mtrk2740.26o` | `mtrk2740.26n` | RINEX 2.11, 30 s, GPS only, uncompressed |
| `generic` | `MTRK00BGD_R_20262740600_01D_00U_MO.rnx` | `MTRK00BGD_R_20262740600_01D_MN.rnx` | RINEX 3.04, native rate; interval, Hatanaka and gzip adjustable |

Each export also writes `manifest.json`: the preset, window, RINEX version, interval, epoch and
navigation-message counts, file sizes and warnings. A full day at 30 s is 2880 epochs. The
navigation file is one mixed file for all systems. The PPP services fetch their own orbits, so
you usually upload only the observation file.

**Warnings to read before uploading** (in the manifest, on the job, and printed by the CLI):

- *the data covers only … of the … window*: the raw logs start, stop or have gaps inside the window
  (less than 90 % covered).
- *only … of data*: under an hour of actual data, which AUSPOS refuses and CSRS-PPP solves poorly.
- *the window includes an hour that is still being written*: export again once that hour closes.
- *no navigation messages* / *only N navigation messages*: no or a thin navigation file. The PPP
  services do not need it.
- `opus` always warns that the F9P sends L2C, not the L2P OPUS expects (see step 3).

## 3. Submit

The *Export RINEX* panel links to each preset's service, and the Site page's step 2 lists them.

- **CSRS-PPP** (Natural Resources Canada, free, worldwide; needs a free NRCan account). Upload the
  `.crx.gz`, choose **Static** processing and the **ITRF** reference frame (the NAD83 options are
  for Canada). The result comes back by e-mail as a zip holding the `.sum` summary and the `.pos`
  epoch file, among others. The service's file-size and duration limits have not been checked
  against a real upload.
- **AUSPOS** (Geoscience Australia, free, worldwide). Upload the `auspos` preset's `_MO.rnx.gz`.
  It takes 1 h to 7 days and uses the GPS observations. The result is a PDF report and a SINEX
  (`.snx`) file by e-mail. If the form asks for the antenna type and height, give the same values
  as `ANTENNA_TYPE` and `ANTENNA_HEIGHT_M` (height 0 for a base site; see *Antenna type and
  height*).
- **OPUS** (US National Geodetic Survey). Only for sites in the USA, so it does not apply to this
  installation and **has not been tried**. It wants GPS L1/L2 data, and the F9P tracks L2C rather
  than L2P, so whether OPUS accepts the `opus` preset's RINEX 2.11 is unknown (spec open item 4).

## 4. Import

**Web UI.** Site page → step 3 → *Import PPP result*. Choose the file. The dialog takes the
CSRS-PPP `.sum` or `.pos`, the e-mailed `.zip` itself (it reads the `.sum` from it, else the
`.pos`, `.snx` or `.txt`), an AUSPOS `.snx`, or the OPUS e-mail saved as `.txt`. The *OPUS frame*
choice (ITRF or NAD83) only matters for OPUS, which reports both. The file is only parsed, and
nothing is saved yet. Check what it shows:

- **source and format**, e.g. `csrs-ppp (csrs-sum)`.
- **frame @ epoch** as the service reports it, e.g. `ITRF20 @ 2026.7500` for the worked window
  above (the mid-point, 2026-10-01 18:00 UTC). CSRS-PPP's coordinates
  are in ITRF2020 at the epoch of the observations.
- **X, Y, Z** in metres, each with its **1σ per ECEF axis**. CSRS-PPP quotes 95 % figures, which
  are divided by 1.96. A `.pos` file, or a `.sum` with no Cartesian block, gives north/east/up
  sigmas, which are rotated into per-axis ECEF sigmas (correlations dropped). SINEX and OPUS
  sigmas are taken as reported. A dash means the file gave none.
- **position**: latitude, longitude and ellipsoidal height, plus the parser's notes.

The site name defaults to `<STATION_ID>-<source>-<epoch>`, e.g. `MTRK-csrs-ppp-2026.75`. You can
change it. *Activate it* is on by default. With it, *Save and activate* saves the site, switches a
running base to fixed mode on it within 10 s, and saves the mode and the site to `.env`, so a
restart keeps it. A file it cannot read is refused with what went wrong, what to upload instead
and the file's first 200 characters.

**CLI.**

```bash
uv run mtrtk ppp-import ~/Downloads/result.zip                    # parse and print only
uv run mtrtk ppp-import ~/Downloads/result.zip --save-site roof-ppp --activate
```

`--prefer-frame itrf|nad83` (default `itrf`) is the OPUS frame choice. `--save-site NAME` saves the
result as a site. `--activate` (which needs `--save-site`) also makes it the active site. A running
base picks it up within 10 s whatever `BASE_MODE` says, but the CLI does not edit `.env`: set
`BASE_MODE=fixed` yourself, or the next start runs a survey-in again (see `docs/base.md`,
*Fixed sites*). Without `--save-site` the command prints the numbers and the suggested name.

**Frames.** The coordinates stay in the service's frame. Rovers working against this base get
positions in that frame too, at that epoch. The plate the base sits on keeps moving (a few
centimetres a year), so a site from a result two years old is a few centimetres off ITRF *now*.
It is still consistent for relative work.

## Antenna type and height

- **`ANTENNA_TYPE`.** This installation's antenna is a SparkFun GPS-15192, which is a u-blox
  ANN-MB-00. Whether CSRS-PPP's antenna model file (ANTEX) has a calibration for the ANN-MB-00,
  and under which IGS code, **has not been checked**. Until it is, keep `ANTENNA_TYPE=NONE`: the
  service then applies no antenna model. The result refers to the antenna reference point (ARP),
  give or take the antenna's uncalibrated phase-centre offset. For a multi-band patch that offset
  is of the order of centimetres, mostly vertical. Rovers on the same antenna model see the same
  offset, so relative work does not notice it.
- **`ANTENNA_HEIGHT_M`.** It goes into the RINEX header (`ANTENNA: DELTA H/E/N`) and nowhere else.
  The base's fixed position, and the RTCM 1005 it broadcasts, are the antenna's position. A PPP
  service that applies the header height reports the **mark below the antenna**, which is not
  what the base must broadcast. So export with `ANTENNA_HEIGHT_M=0` for a base site, and note the
  height separately if you also want the mark. If you exported with a height, check whether the
  result is of the mark or of the antenna. If it is the mark, the import flows cannot fix it:
  *Import PPP result* and `mtrtk ppp-import --save-site` save the parsed ECEF as it is. Add the
  height `h` back along the local vertical by hand, X += h·cos(lat)·cos(lon),
  Y += h·cos(lat)·sin(lon), Z += h·sin(lat), and enter the corrected ECEF through the Site page's
  *Add site* or `mtrtk sites add NAME --ecef X Y Z`. (Or add `h` to the ellipsoidal height and
  use `mtrtk sites add NAME --llh LAT LON H`.) Whether CSRS-PPP applies the header height has not been
  verified on a real submission.

## Verify

1. **The 1005 matches.** Once the site is active and the base is in fixed mode, the Site page says
   "RTCM 1005 matches the active site: every axis within 0.5 mm", and a `site_verified` event is
   logged. A mismatch (`site_mismatch`) means the receiver is broadcasting something other than the
   site, for example because a TMODE write failed. The outcome line under the sites table says
   what the receiver did.
2. **The offset from the survey-in is plausible.** Compare the PPP position with the survey-in mean
   it replaces. The difference is the survey-in's absolute error, usually 0.5–2 m under open sky. A
   difference of tens of metres means the wrong file, the wrong station in a multi-station SINEX,
   or an antenna that moved.
3. **The sigmas are small.** A 24 h static CSRS-PPP solution under open sky should give 1σ of a few
   millimetres to a centimetre or two per axis. Larger sigmas point at the sky view or a short or
   gappy window (check the export's warnings).
4. **Rovers shift once.** Rover positions jump by the offset between the old and new base
   positions when the site is activated, and are stable after that.
