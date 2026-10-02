# Receiver firmware

mtrtk was built and tested on a ZED-F9P running **HPG 1.13** (protocol version 27.12) and is
written to run unchanged on **HPG 1.51**, the current release. **Upgrade to 1.51**: u-blox's RTK
and robustness fixes since 1.13 are the reason, and why `mtrtk doctor` warns on anything older
than HPG 1.32. mtrtk works on 1.13 in the meantime: no mtrtk feature needs the upgrade. This page
says how to check the firmware, why to upgrade, how, and what mtrtk does differently on each
version.

## Check the version

- **Web UI:** Receiver page → *Firmware*: the firmware (`HPG 1.13`), the protocol version
  (`27.12`) and the optional features the receiver supports.
- **API:** `curl -s http://<tailscale-ip>:8080/api/status`. The `firmware` object holds
  `fw_version`, `protver` and `module`.
- **Doctor:** `mtrtk doctor --probe` polls MON-VER. Only one program can read the port, so stop
  the daemon first (`docker compose stop mtrtk`, then `docker compose run --rm mtrtk doctor --probe`,
  then `docker compose start mtrtk`; native: `sudo systemctl stop mtrtk`, then
  `.venv/bin/mtrtk doctor --probe`, then `sudo systemctl start mtrtk`). It warns under HPG 1.32.

## Why upgrade

- **u-blox's fixes.** This is the reason to upgrade. The HPG release notes list RTK, robustness
  and security improvements since 1.13; read them on the ZED-F9P page at u-blox.com.
- **Three-frequency RINEX.** `convbin` runs with `-f 3` instead of `-f 2` for CLI exports
  (`mtrtk export`) of logs recorded on HPG 1.51 or newer, which read the firmware from the
  sidecars, and for exports from the running daemon (UI, API) only while an L5-band signal is
  tracked; otherwise `-f 2`. `-f 3` keeps a third frequency. Whether your module tracks
  L5 at all depends on its hardware variant and on signal settings that mtrtk does not write (its
  signal plan is L1/L2 on every firmware, below). Do not upgrade for L5 alone.
- **`lastCorrectionAge`.** mtrtk decodes NAV-PVT's correction-age field on any firmware. Whether
  HPG 1.13 fills it in is unverified (spec open item 1).

The spectrum (MON-SPAN), MON-COMMS and NAV-TIMELS already work on HPG 1.13: they were checked on
the receiver mtrtk was built with. They are not a reason to upgrade.

**Not yet verified on HPG 1.51.** No receiver on 1.51 has run mtrtk so far. The optional-feature
probe and the "skip what the receiver refuses" rules below are what make it safe to try, and
`RECEIVER_STRICT=1` (the default) stops the daemon with the rejected keys if a core key is
refused at startup (a later reconnect only reports it as `receiver.error` and retries).

## Upgrade procedure

You need a Windows PC with **u-center 1** (not u-center 2: the steps below are u-center 1's), the
USB-C cable, and the firmware file from u-blox (ZED-F9P product page →
Documentation & resources → firmware: the HPG 1.51 `.bin`, named like
`UBX_F9_100_HPG151....bin`, with its release notes).

1. Stop mtrtk on the station (`docker compose stop mtrtk` or `sudo systemctl stop mtrtk`) and
   move the receiver to the Windows PC.
2. In u-center 1, connect to the receiver's COM port.
3. *Tools → Firmware Update*. Choose the `.bin`. Tick **Use this baudrate for update** and set it
   to **9600**, and tick **Enter safeboot before update**. Leave the other options at their
   defaults.
4. Press the green *GO* button and wait. The update takes about 2 minutes. Do not unplug the
   receiver until u-center says it has finished; the receiver then restarts.
5. Reconnect, and check *View → Messages View → UBX → MON → VER*: it should show the new version.

## After the upgrade

- **The configuration is rewritten by mtrtk.** Expect the update to clear the configuration the
  receiver kept in flash. mtrtk does not depend on it: it reads the receiver's configuration back
  on every connect and writes what differs. The first apply after a daemon start writes RAM, BBR
  and flash; a reconnect writes RAM only (to save flash wear). So **restart mtrtk** after putting
  the receiver back (`docker compose up -d`, or `sudo systemctl start mtrtk`), rather than relying
  on a daemon that was left running.
- **Survey-in or site.** mtrtk writes the time mode on every start, so it does not matter whether
  the update kept it. With `BASE_MODE=survey-in` the daemon starts a new survey. With `BASE_MODE=fixed` it writes the
  active site again, and the RTCM 1005 check confirms it within seconds
  ([base.md](base.md#the-1005-check)). A site from PPP is still valid: the antenna has not moved.
- **The Receiver page changes.** *Firmware* shows the new version and protocol version, and the
  *Supported* / *Unsupported* lists come from a fresh probe. The raw-log sidecars record the
  firmware of each hour, so exports of older hours keep their old receiver version and
  frequency count.

## What mtrtk does on each version

The core configuration is the same on both versions. Features that a firmware may lack are
probed by asking for their configuration keys (CFG-VALGET), and a feature whose keys the
receiver does not have is switched off and listed as unsupported instead of failing the start.

| | HPG 1.13 (PROTVER 27.12) | HPG 1.51 |
|---|---|---|
| Core profile: rates, USB protocols, UBX messages, RTCM 1005/MSM/1230, TMODE | written and verified | same keys |
| Signal plan | GPS L1C/A+L2C, GLONASS L1+L2, Galileo E1+E5b, BeiDou B1+B2, QZSS L1C/A+L2C, SBAS off | the same plan; mtrtk writes no L5/E5a/B2a/NavIC keys |
| MON-SPAN, MON-COMMS, NAV-TIMELS | probed: all three supported (checked on hardware) | probed |
| Keys mtrtk never sends | `CFG-SIGNAL-PLAN`, `NAV2-*`, RXM-COR, MON-SYS, SEC-SIG, NAV-PL, NAV-TIMETRUSTED, the Galileo OSNMA keys, `CFG-RTCM-DF003_IN_FILTER`, RTCM 1006 | the same: none of them are used on any version |
| RINEX export | `convbin -f 2` | `-f 3` (from the live receiver: when an L5-band signal is tracked; from the CLI: from the firmware in the sidecars) |
| Restarting a survey-in | writing the same survey-in values does not restart a running survey, so mtrtk's restart turns TMODE off first, then on | the same two-step restart |
| MON-RF blocks | both blocks report id 0; the UI numbers them by position | numbered as reported |
| Polls for MON-SPAN / MON-COMMS | not answered (only the periodic output arrives), which is why the probe uses CFG-VALGET | the same probe |
| MSM before a valid position | MSM and 1230 flow as soon as the profile is applied; 1005 waits for a valid survey-in or fixed site | not yet checked |
| `mtrtk doctor --probe` | WARN: older than HPG 1.32 | OK |
