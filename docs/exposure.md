# Exposure: who can reach the station

By default the web UI and the NTRIP caster listen only on the host's Tailscale address. Your
own devices on the tailnet can reach them, and nobody else can. This page covers that default,
the two ways to reach the station from the open internet, and how to read a receiver on another
machine over Tailscale.

| | Tailscale (default) | Public IP + Caddy | Cloudflare Tunnel |
|---|---|---|---|
| Web UI | http over WireGuard | https (Let's Encrypt) | https (Cloudflare) |
| NTRIP | v1+v2 plain TCP 2101 | v1+v2 plain TCP 2101 (router forward) | v2 over HTTPS only |
| Auth | tailnet identity (+ optional password) | `WEB_PASSWORD` required | `WEB_PASSWORD` or Cloudflare Access |
| Router changes | none | forward 443 + 2101 (and 80) | none |
| Needs | a Tailscale client on every rover and browser | a public IPv4 address (not CGNAT) and a DNS name | a domain on Cloudflare |
| Cost | free (personal plan) | free | free |
| Compose | `docker compose up -d` | `docker compose --profile public up -d` | `docker compose --profile cloudflare up -d` |

Whichever path you pick, check it from outside with `scripts/check-exposure.sh`. It asks for
`/healthz` and then opens an NTRIP v2 stream, and passes once RTCM3 frames arrive:

```bash
scripts/check-exposure.sh <web-url> <ntrip-url> [user] [password]
# OK: 18 RTCM3 frames in 2040 bytes; first byte after 0.1 s, read for 4.0 s
```

It exits 0 when both checks pass and 1 when one fails, with a hint (wrong password, the proxy
cannot reach the daemon, a sourcetable instead of a stream, a tunnel that buffers). It needs
`curl`. `CHECK_TIMEOUT_S` (default 15) sets how long it waits for RTCM.

`mtrtk doctor`'s `exposure` line says what is reachable beyond Tailscale without a password, and
fails when the web UI would be.

## Tailscale (default)

```
NTRIP_BIND=tailscale
WEB_BIND=tailscale
NTRIP_PASSWORD=<password>
```

Install Tailscale on the station (`sudo tailscale up`, [setup.md](setup.md)) and on every phone,
laptop or rover computer that needs it. The station's address is `tailscale ip -4`.

- **Web UI:** `http://<tailscale-ip>:8080`. No password needed: the tailnet is the boundary.
  `WEB_PASSWORD` adds a login on top.
- **NTRIP:** `ntrip://rover:<password>@<tailscale-ip>:2101/MTRK`, v1 and v2 on the same port.
- **Check:** `scripts/check-exposure.sh http://<tailscale-ip>:8080 http://<tailscale-ip>:2101/MTRK rover <password>`
  from another tailnet device.

`tailscale` binds the `tailscale0` address only. If Tailscale is not up yet (the station booted
before `tailscaled`), mtrtk retries every 5 s, logs a warning once a minute, and never falls back
to `0.0.0.0`. Use the Tailscale admin console's access controls (ACLs) to limit which of your
devices may reach ports 8080 and 2101.

## Public IP + Caddy

For a station with a public IPv4 address that your router can forward to. Many mobile and some
home ISPs use carrier-grade NAT (the router's WAN address is in `100.64.0.0/10` or another
private range, or differs from what `curl -4 ifconfig.me` shows). Behind CGNAT this path cannot
work; use the Cloudflare Tunnel.

1. Point a DNS name at your public IP: an `A` record, for example `rtk.example.com`.
2. Forward TCP **443** (HTTPS) and **80** (the redirect to HTTPS, and Let's Encrypt's HTTP
   challenge) to the station, and TCP **2101** for NTRIP. Caddy proxies the web UI only; rovers
   reach the caster directly.
3. In `.env`:

   ```
   PUBLIC_DOMAIN=rtk.example.com
   # ACME_EMAIL: optional, for certificate expiry mail from the CA
   ACME_EMAIL=you@example.com
   WEB_BIND=127.0.0.1                # Caddy proxies to 127.0.0.1:WEB_PORT; `lan` also works
   WEB_PASSWORD=<long random password>
   NTRIP_BIND=lan                    # every interface, so the forwarded 2101 reaches it
   NTRIP_PASSWORD=<password>
   ```

4. Start it, then check from outside your network (a phone on mobile data, or a VPS):

   ```bash
   docker compose --profile public up -d
   scripts/check-exposure.sh https://rtk.example.com http://rtk.example.com:2101/MTRK rover <password>
   ```

Caddy (`docker/Caddyfile`) gets and renews the certificate itself and keeps it in the
`caddy_data` volume. It sends HSTS, compresses responses, and runs with its admin API off.
After an edit to the Caddyfile, run
`docker compose --profile public up -d --force-recreate caddy`. The file is bind-mounted on its
own, and an editor that saves by rename (`sed -i`, many vim and IDE setups) leaves the running
container on the old copy, which a plain `restart` would reload without any error.

`WEB_BIND=127.0.0.1` keeps the UI off the LAN and off the tailnet: only Caddy can reach it. Use
`WEB_BIND=lan` if you also want it on the tailnet address (it then listens on every interface,
behind the password). `lan` and `all` both listen on `0.0.0.0`.

NTRIP stays plain TCP on 2101 in this path: NTRIP v1 has no TLS, and most rover apps cannot do
NTRIP over TLS. The password crosses the internet in Basic auth, readable by anyone on the path.
Treat `NTRIP_PASSWORD` as a shared key for corrections, not as a secret that guards anything else.

## Cloudflare Tunnel

For a station behind CGNAT, or when you do not want to open router ports. `cloudflared` makes an
outbound connection to Cloudflare, and Cloudflare serves your hostnames over HTTPS. The tunnel
carries HTTP, not raw TCP, so NTRIP works only for clients that speak **NTRIP v2 over HTTPS**.

1. Add your domain to Cloudflare. In Zero Trust → Networks → Tunnels, create a tunnel of type
   *Cloudflared* and copy its token.
2. Add two public hostnames to the tunnel:
   - `rtk.<domain>` → service `HTTP`, URL `127.0.0.1:8080` (your `WEB_PORT`)
   - `ntrip.<domain>` → service `HTTP`, URL `127.0.0.1:2101` (your `NTRIP_PORT`)
3. In `.env`:

   ```
   TUNNEL_TOKEN=<token>
   WEB_BIND=127.0.0.1                # or lan; cloudflared reaches the daemon on loopback
   WEB_PASSWORD=<long random password>
   NTRIP_BIND=127.0.0.1              # or lan, to keep serving tailnet rovers on 2101 too
   NTRIP_PASSWORD=<password>
   ```

   If another `cloudflared` already runs on the host (`cloudflared service install`), it holds
   the metrics ports 20241-20245: set `TUNNEL_METRICS_PORT=20246`.

4. Start and check:

   ```bash
   docker compose --profile cloudflare up -d
   docker compose ps                 # mtrtk-cloudflared becomes healthy once the tunnel is up
   scripts/check-exposure.sh https://rtk.<domain> https://ntrip.<domain>/MTRK rover <password>
   ```

Rovers then use host `ntrip.<domain>`, port **443**, TLS on, mountpoint `MTRK`, NTRIP v2.

**Cloudflare Access instead of a password.** You can put an Access policy (e-mail one-time PIN,
your identity provider) on `rtk.<domain>` and run the UI without `WEB_PASSWORD`: then set
`WEB_BIND=127.0.0.1` and `WEB_ALLOW_INSECURE=1`, never `WEB_BIND=lan` with it, since that would
leave an open UI on your LAN and tailnet. `mtrtk doctor` warns that only Access protects the UI.
Do not put Access on `ntrip.<domain>`: NTRIP clients cannot log in to it. `check-exposure.sh` takes
an Access service token in `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`.

**Not yet verified through the real Cloudflare edge.** The chunked NTRIP v2 stream was tested
through a local HTTP reverse proxy, where the first RTCM byte arrived after 0.1 s. Whether
Cloudflare buffers it, how long the first byte takes through the edge, and the WebSocket through
the tunnel are still to be checked (Phase 9 acceptance). If `check-exposure.sh` says the stream is
buffered, serve rovers over the public-IP path or Tailscale instead.

## Clients

| Client | NTRIP | Tailscale | Public IP | Cloudflare Tunnel |
|---|---|---|---|---|
| SW Maps (Android) | v2 | yes (Tailscale app on the phone) | yes | only if it offers NTRIP over TLS; untested |
| Emlid Flow | v2 | yes (Tailscale app on the phone) | yes | only if it offers NTRIP over TLS; untested |
| u-center 1 (Windows) | v1 | yes | yes | no: plain TCP only |
| `str2str` (RTKLIB) | v1 | yes | yes | no: plain TCP only |
| `gnssntripclient` (pygnssutils) | v2 | yes | yes | yes, with `--https 1 --port 443` |
| `curl` (what the check script uses) | v2 | yes | yes | yes |
| mtrtk rover (`NTRIP_URL`) | v2, falls back to v1 | yes | yes | no: the client speaks plain TCP |

Examples:

```bash
# str2str: corrections into a serial rover
str2str -in ntrip://rover:<password>@<host>:2101/MTRK -out serial://ttyUSB0:115200
# gnssntripclient through the tunnel
gnssntripclient --server ntrip.<domain> --port 443 --https 1 --mountpoint MTRK \
  --ntripuser rover --ntrippassword <password>
```

In SW Maps and Emlid Flow, give the host, port 2101, mountpoint `MTRK`, user `rover` and the
password; their "get mountpoints" button reads the caster's sourcetable, which needs no password.

## Threat notes

- **Never set `WEB_ALLOW_INSECURE=1` on a public bind.** It exists for a trusted LAN, and for
  `WEB_BIND=127.0.0.1` behind Cloudflare Access. Anyone who reaches an open UI can change every
  setting, reset the receiver and read the raw logs.
- **Use a long random `WEB_PASSWORD`** (`openssl rand -base64 24`). mtrtk does not rate-limit
  login attempts, and the login cookie lasts 30 days. The cookie is `HttpOnly` but not marked
  `Secure`: Caddy's HSTS keeps returning browsers on HTTPS, but a browser that has lost the HSTS
  entry (cleared or expired) and then visits `http://` sends the cookie once, in plain text,
  before the redirect. So does any client that ignores HSTS.
- **Rotate `NTRIP_PASSWORD` when you share it** with someone outside your own devices, and again
  when they no longer need it. Corrections are all it protects, but an open caster serves anyone
  who finds it, and `NTRIP_MAX_CLIENTS` (32) is shared by everyone. An anonymous caster
  (`NTRIP_PASSWORD=` empty) belongs on the tailnet only. doctor warns about one only on
  `NTRIP_BIND=all` (or a public IP) or when the tunnel publishes it. On `NTRIP_BIND=lan` or a
  private IP it cannot tell whether your router forwards 2101, so it stays silent: on the
  public-IP path always set `NTRIP_PASSWORD`.
- **Cloudflare sees plaintext.** TLS ends at Cloudflare's edge, so Cloudflare can read the web UI
  traffic, the login password and the NTRIP stream. If that matters, use Tailscale.
- **Behind a proxy the client is 127.0.0.1.** Through the Cloudflare Tunnel, the caster's client
  list shows `cloudflared`'s address, not the rover's. Through Caddy, the web UI's logs show
  Caddy's. Rovers on the forwarded 2101 of the public-IP path reach the caster directly and keep
  their real address.

## Remote receivers over Tailscale

The receiver does not have to be plugged into the machine that runs mtrtk. A receiver on another
PC can be relayed over Tailscale with `socat` and appears on the mtrtk host as a pseudo-terminal.

On the PC with the receiver (stop anything else that reads the port, such as `gpsd`, first):

```bash
socat TCP-LISTEN:5001,bind=<that-pc's-tailscale-ip>,reuseaddr \
  FILE:/dev/ttyACM0,raw,echo=0
```

On the mtrtk host:

```bash
mkdir -p ~/dev
socat PTY,link=$HOME/dev/f9p,raw,echo=0 TCP:<that-pc's-tailscale-ip>:5001 &
MTRTK_SOURCE=$HOME/dev/f9p RECEIVER_ACK_TIMEOUT_S=5 uv run mtrtk base
```

**The restart wrapper is required, not optional.** Neither `socat` keeps running on its own:
the listener without `fork` exits after its one connection, and the PTY end exits when the slave
side is closed, which happens on every mtrtk reconnect (the no-data watchdog, a link timeout), on
`mtrtk doctor --probe` and on a daemon restart. Run each end in a loop:

```bash
while true; do socat ...; sleep 2; done      # the same socat command as above
```

or as a systemd unit with all three of these, so a far end that is down for a while does not
trip systemd's start limit (5 starts in 10 s by default) and leave the unit failed:

```ini
[Unit]
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/bin/socat PTY,link=/home/<you>/dev/f9p,raw,echo=0 TCP:<that-pc's-tailscale-ip>:5001
Restart=always
RestartSec=2
```

`mtrtk doctor` reports a missing PTY as "is the link that creates it (socat, ser2net) running?".
INS units work the same way, with `INS_PORT` instead of `MTRTK_SOURCE`
([ins-drivers.md](ins-drivers.md)). `RECEIVER_ACK_TIMEOUT_S` below applies to the F9P only: the
INS drivers use their own fixed 5 s timeouts.

Caveats:

- **Relays stall.** When Tailscale cannot make a direct connection it goes through a DERP relay
  (`tailscale status` shows `relay`), and the link then drops the odd reply and stalls for
  seconds. Raise `RECEIVER_ACK_TIMEOUT_S` (default 2, up to 30; 5 is a good start) so
  configuration writes and reads are not given up too early. mtrtk retries unanswered probes and
  readbacks, but a stall longer than the no-data watchdog still makes it reconnect.
- **One client per port.** Without `fork`, `TCP-LISTEN` takes one connection at a time, which is
  what you want: two programs writing configuration to one receiver would corrupt each other's
  replies. Do not add `fork`.
- **ACLs.** Anyone on the tailnet who can reach port 5001 has the raw receiver: they can read it,
  and they can reconfigure or reset it. Restrict the port to the mtrtk host in your Tailscale
  ACLs.
- **Native only.** The PTY is a `/dev/pts` device, which the compose file's device rules do not
  admit (they allow `ttyACM*` and `ttyUSB*`). Run the mtrtk end with `uv run` or the native
  install.
- On a pseudo-terminal `BAUD` is ignored: the far end's `socat` sets the line, or the USB CDC
  port ignores it anyway. Keep the link read: an unread tunnel backs up, and the far end's serial
  buffer overflows.
