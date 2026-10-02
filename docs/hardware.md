# Hardware

What mtrtk was built and tested on, how to mount the antenna, and how to read the receiver's RF
health. Software setup is in [setup.md](setup.md); firmware in [firmware.md](firmware.md).

## The receiver: SparkFun GPS-RTK-SMA (u-blox ZED-F9P)

| | |
|---|---|
| Host link | USB-C, USB CDC-ACM: `/dev/ttyACM0`, with a stable `/dev/serial/by-id/usb-u-blox_..._GNSS_receiver-if00` link |
| USB id | vendor `1546` (u-blox), product `01a9`. `MTRTK_SOURCE=auto` finds the receiver by the by-id name, then by vendor id |
| Antenna port | SMA, with 3.3 V bias for an active antenna |
| Antenna detection | Not wired on this board, so mtrtk leaves the receiver's antenna supervisor (`CFG-HW-ANT_*`) at its default and never enables it. The antenna status on the Receiver page cannot tell a cut cable from a good one here |
| Power | From USB: 5 V, about 100 mA with the antenna. Any Pi USB port is enough |
| Firmware tested | HPG 1.13 (PROTVER 27.12). The same build is meant to run on HPG 1.51 ([firmware.md](firmware.md)) |

mtrtk talks only UBX and RTCM3 over USB: it turns NMEA output off, and the rover writes RTCM into
the same port. The UART pins are not used.

## The antenna

mtrtk was tested with a SparkFun multi-band magnetic-mount antenna (GPS-15192, a u-blox
ANN-MB-00) on 5 m of cable.

- **Ground plane.** A patch antenna like this needs a metal plate under it, at least 10 cm
  across, or a metal roof. Without one, multipath and the phase centre get worse.
- **Sky.** Clear sky above 10° elevation all round. mtrtk's profile sets a 10° elevation mask
  (`CFG-NAVSPG-INFIL_MINELEV`). Keep the antenna away from metal edges, walls and railings, and at
  least 1 m from Wi-Fi, LTE and other transmitting antennas.
- **Cable.** 5 m of RG174 loses about 3-4 dB at L1/L2. The antenna's own amplifier makes that up,
  so it is fine. Longer runs want lower-loss cable (RG58, LMR-200).
- **Base mounting.** A base antenna must not move. The PPP position is the antenna's position on
  the day it was measured: a bump of a centimetre puts that centimetre into every rover. Fix it
  mechanically (a bolted plate, a survey pillar), not with the magnet alone.
- **Indoors.** A survey-in will not validate indoors: the NAV-SVIN mean accuracy stays around
  10 m ([base.md](base.md)).

### ARP and phase centre

This antenna has no ANTEX calibration: no published phase-centre offset or variation. So mtrtk
treats every position as the position of the **antenna reference point (ARP)**, the bottom of
the mount. `ANTENNA_TYPE=NONE` tells PPP services the same, and they report the ARP.

- Expect a centimetre-level vertical offset between this antenna's positions and those of a
  calibrated survey antenna on the same mark. Rovers on the same antenna model see the same
  offset, so relative work does not notice it.
- `ANTENNA_HEIGHT_M` is the height of the ARP above the ground mark. It goes into the RINEX
  header only. For a base site, export with `ANTENNA_HEIGHT_M=0`; why is explained in
  [ppp-workflow.md](ppp-workflow.md#antenna-type-and-height).

## Two receivers on one host

Every ZED-F9P has the same USB descriptor, so two of them share one by-id name and
`/dev/serial/by-id/` keeps only one of the two links. Give each receiver its own USB serial
number string: in u-center (View → Generation 9 Configuration View → Advanced Configuration),
set `CFG-USB-SERIAL_NO_STR0` .. `STR3` and write it to the flash layer. Then unplug and replug
the receiver (or send it a hardware reset): the host reads the USB descriptor only when the
device enumerates. Each receiver then gets its own by-id link, and
`MTRTK_SOURCE=/dev/serial/by-id/<that link>` picks one. mtrtk does not write these keys itself.
Without u-center, `/dev/serial/by-path/...` names a receiver by the USB socket it is plugged into,
which works as long as nobody swaps the cables.

## ModemManager

ModemManager (installed on Ubuntu desktop and many laptops) probes every new `ttyACM*` with AT
commands. On a GNSS receiver that delays the port by about 30 s, and at worst writes junk into
it. `udev/99-mtrtk-ublox.rules` sets `ID_MM_DEVICE_IGNORE=1` on u-blox devices (vendor 1546) and
gives the `dialout` group read/write on the port. `install.sh` installs it; for Docker, see
[setup.md](setup.md#1-prepare-the-host). `mtrtk doctor` warns when ModemManager runs without the
rule.

## RF interference

The Receiver page shows one *RF block* panel per front end (HPG 1.13 reports both as block 0, so
they are numbered by position). Each has the jamming indicator, the jamming state, the AGC count,
the noise level, the I/Q balance and the antenna state, each with a trend line. *Spectrum* draws
MON-SPAN when the firmware has it. The `jamming` alert fires when the jamming indicator stays at
200 or more (of 255), or the jamming state at *warning* or worse, for 30 s.

| What you see | Likely cause | What to do |
|---|---|---|
| Jamming indicator high or state *warning*/*critical*, a spike in the spectrum | A transmitter near the antenna: Wi-Fi, LTE, a radio link, a cheap USB 3 device | Move the antenna (1 m from other antennas is a minimum), or switch the source off and watch the indicator fall |
| AGC count pinned at the top or bottom of its range | No signal reaching the front end, or far too much: an open or shorted cable, a damaged antenna, or a strong transmitter | Check the cable and the connectors; try another antenna |
| Noise level high on every block, C/N0 low on every satellite | Broadband noise, often a USB 3 hub, port or cable next to the antenna or its cable | Move the receiver and cable away from USB 3 hardware, or use a USB 2 port |
| C/N0 low only on low satellites or in one direction | Obstruction or multipath | Raise or move the antenna |
