# HTTP API and WebSocket

The daemon serves its API from inside its own event loop (uvicorn, no separate process), on
`WEB_BIND`/`WEB_PORT` — by default the Tailscale address on port 8080. Every role serves it: a
rover's screen is the same screen as a base's, with the base panels empty.

- Base URL: `http://<bind-host>:8080`
- Interactive docs: `/api/docs` (Swagger UI) and `/api/openapi.json`
- Liveness: `/healthz` — the one route that is never behind the password
- WebSocket: `/ws`
- Anything else that is not under `/api`, `/ws`, `/healthz` or `/assets` serves the SPA's
  `index.html`, or `503 {"detail": "UI not built; …"}` when the bundle is absent.

## Authentication

Authentication exists only when `WEB_PASSWORD` is set. With it unset every route is open, which
is why `Settings` refuses a non-`tailscale` `WEB_BIND` unless a password is set or
`WEB_ALLOW_INSECURE=1` says the exposure is deliberate.

| Step | Request | Effect |
| --- | --- | --- |
| Log in | `POST /api/login {"password": "…"}` | `200 {"token": "<hex>"}` and a `Set-Cookie: mtrtk_session=<token>; HttpOnly; SameSite=Lax; Max-Age=2592000`. Wrong password: `401`. With no password configured it answers `200 {"token": ""}` and sets no cookie. |
| Call | any `/api/*` | The cookie is enough for a browser. A non-browser client may send `Authorization: Bearer <token>` instead. |
| WebSocket | `/ws?token=<token>` | The cookie and the `Authorization` header are used first; `?token=` is the fallback for clients that can set neither. |
| Log out | `POST /api/logout` | Clears the cookie. |

The token is derived, not stored: `HMAC-SHA256(key=sha256(password), msg=b"mtrtk-session")`,
hex-encoded. It therefore survives a restart, and changing `WEB_PASSWORD` invalidates every
session. There is no user list and no expiry beyond the cookie's 30 days.

`/api/docs` and `/api/openapi.json` are behind the same dependency: **when a password is set, the
interactive docs require a login first** (open `/api/login` from the UI, or send the bearer
token). On a Tailscale-only deployment with no password they open straight away.

## Conventions

Everything is JSON unless it is a file download. Errors are `{"detail": …}` — a string for the
hand-written refusals, a list of `{"loc", "msg", "type"}` objects for a body or query that failed
validation. Validation details under `/api` never echo the offending value back, so a password
posted to the wrong field cannot end up in a log or a browser console.

| Code | Meaning across the whole API |
| --- | --- |
| 200 | Done. A command answers with the state it produced, not a bare `{"ok": true}`, wherever there is one to give. |
| 401 | `WEB_PASSWORD` is set and the request carried no valid session (`WWW-Authenticate: Bearer`). Applies to every `/api/*` route except `/api/login`; `/healthz` is always open. |
| 404 | The named thing does not exist: a site, an event id, a raw log, a job, a result file. |
| 409 | The request is well formed but the daemon cannot do it *now*: no receiver controller, receiver not connected, passive (replay) source, no base-mode manager, no job runner, the job is still running, the raw log is the open hour, the file is marked `keep`, fixed mode with no site. This is the code a UI should render as an explanation, not as a bug. |
| 422 | The request itself is wrong: a bad body, an unknown settings key, a read-only key, an unknown metric, an out-of-order or oversized time window, an unknown UBX message name. |
| 500 | A bug. Nothing in the API raises it deliberately. |
| 503 | The SPA bundle is not built (`GET /` and SPA routes only). |
| 504 | The receiver did not answer in time (`LinkTimeout`). Only the three `/api/receiver/*` commands can produce it. |

Timestamps are ISO-8601 UTC strings in JSON bodies, and float epoch seconds in the history rows
and in the WebSocket `epoch` message (`t`).

## Status and system

| Route | Answer |
| --- | --- |
| `GET /healthz` | `{"status": "ok", "role", "connected"}`. No auth, no database access — safe as a container healthcheck (`mtrtk healthcheck` is exactly this request). |
| `GET /api/status` | One screen: `role`, `version`, `uptime_s`, `connected`, `source`, `firmware{fw_version,protver,module}`, `fix{fix_type_name,carr_soln_name,num_sv}`, `position`, `accuracy`, `survey_in`, `ntrip_clients`, `ntrip_rejected`, `rtcm_bytes_per_s`, `epoch_count`, `capabilities`. |
| `GET /api/state` | The whole `ReceiverState` — the same object the WebSocket sends as its snapshot. |
| `GET /api/system` | `hostname`, `tailscale_ip`, `data_dir`, `stats` (CPU, memory, disk, temperature, load, `ts_utc`; `null` until the first sample) and `versions`. |

## Configuration

`GET /api/config` returns:

```json
{
  "values":         { "role": "base", "ntrip_password": "***", "...": "every Settings field" },
  "pending":        { "svin_min_duration_s": 300 },
  "env_file":       ".env",
  "secret_keys":    ["alert_webhook_url", "ntrip_password", "tunnel_token", "web_password"],
  "live_keys":      ["active_site", "base_mode", "svin_acc_limit_m", "svin_min_duration_s"],
  "read_only_keys": ["mtrtk_env_file"],
  "url_secret_keys":["ntrip_url"]
}
```

- **`secret_keys`** come back as `"***"` when set. Post `"***"` back and the stored value is kept;
  post anything else and it is written. That is what lets a form submit the whole object.
- **`url_secret_keys`** carry a secret inside them (`ntrip://user:pass@host/MP`). Only the
  password part is masked, and posting the masked URL back keeps the stored password while
  accepting any other change to the URL.
- **`read_only_keys`** are refused by `PUT` with a 422. `mtrtk_env_file` is one because moving the
  pointer would leave the running daemon reading one file while every later write went to another.
- **`live_keys`** take effect without a restart, provided a base-mode manager is running.
- **`pending`** is what `.env` says and the running process does not: the changes a restart would
  pick up. Under Compose, `env_file:` values reach the process as environment variables, which
  outrank the file — there a plain container restart keeps the old values and only
  `docker compose up -d` (a recreate) applies them.

`PUT /api/config {"values": {…}}` validates everything first, then writes `.env` and applies what
it can, and answers `{"changed": ["…"], "restart_required": bool}`. Nothing is written when a
single key is refused. 422 for an unknown key, a read-only key, a value `Settings` rejects, or
`ntrip_password: null` (which would silently turn "undecided" into anonymous access); 409 for
`base_mode=fixed` with no site to sit on.

`POST /api/restart` sets the daemon's stop event and answers `{"ok": true}` — the process exits
and the supervisor (Docker `restart: unless-stopped`, or systemd) starts it again with the new
`.env`. The response is sent before the process goes away; the UI should expect the socket to
drop and poll `/healthz` until it answers again.

## Receiver

| Route | Notes |
| --- | --- |
| `GET /api/receiver` | Always 200, even with nothing connected: `{"connected", "passive", "source", "capabilities", "firmware"}`. |
| `POST /api/receiver/reapply` | Re-probe and re-apply the role's profile. Nothing is written when the receiver already matches. 409/504. |
| `POST /api/receiver/reset {"kind": "hot"\|"warm"\|"cold"\|"factory"}` | A hardware reset: the USB device drops off the bus and comes back, so expect `receiver.disconnected` then `receiver.connected` on the WebSocket. `factory` also wipes the configuration, and the reconnect re-applies the whole profile to every layer, flash included. 409/422/504. |
| `POST /api/receiver/poll {"msg_class": "MON", "msg_id": "MON-VER"}` | The parsed fields of the reply plus `identity`. A firmware that does not know the message answers `200` with `identity: "ACK-NAK"` — that is the receiver refusing, not an error; a firmware that says nothing at all is a `504`. A name pyubx2 cannot build is a `422`. |

All four share the 409s: `no receiver: this daemon runs without a receiver controller`,
`receiver not connected`, and (for `reapply`/`reset`) `receiver is in passive mode: …` on a replay
source.

## Base station

| Route | Notes |
| --- | --- |
| `GET /api/base/mode` | `{"available", "mode", "site", "verified", "last_1005", "svin": {"min_duration_s", "acc_limit_m"}}`. `available: false` means this daemon runs no base-mode manager (rover role, or a replay source). |
| `PUT /api/base/mode {"mode", "svin_min_duration_s"?, "svin_acc_limit_m"?, "site"?}` | Writes the receiver *and* `.env`, through the same path as `PUT /api/config`. `site` is only meaningful with `mode: "fixed"`. 409 with no manager or no site; 422 for a value the settings refuse. |
| `GET /api/base/survey` | The live `SurveyIn`: `active`, `valid`, `dur_s`, `obs`, `mean_x_m`/`mean_y_m`/`mean_z_m`, `mean_acc_m`. |
| `POST /api/base/survey/restart` | TMODE off, then survey-in again — re-sending the same parameters does **not** restart a survey on HPG 1.13, which is why this is its own route. 409 when the base is not in survey-in mode, or when the receiver refused either half (the detail says which, and what state that leaves the base in). |
| `POST /api/base/survey/freeze {"name", "activate": false}` | Save a completed survey-in as a site. 409 when the survey is not valid yet, or the name is taken. |
| `GET /api/base/sites` · `POST /api/base/sites` | The saved ECEF sites. A site is given as `x,y,z` (metres) **or** `lat,lon,height_m`; half a coordinate is a 422. 409 on a duplicate name. |
| `POST /api/base/sites/{name}/activate` | Make it the active fixed site. A running base picks it up within 10 s. 404 for an unknown name. |
| `DELETE /api/base/sites/{name}` | 404 unknown, 409 when it is the active site. |

## NTRIP caster

| Route | Notes |
| --- | --- |
| `GET /api/ntrip` | What a rover needs plus what the caster is doing: `running`, `host`, `port`, `bind_mode`, `mountpoint`, `anonymous`, `username`, `connection_url` (password masked), `clients`, `max_clients`, `rejected`, `sourcetable`. With no caster it still answers 200 with the configured values and `null` for the live ones. |
| `GET /api/ntrip/clients` | The rovers connected right now; `[]` with no caster. |
| `GET /api/ntrip/history?limit=100` | Past connections, newest first, including ones from earlier runs. 422 outside 1…1000. |

## Raw logs

| Route | Notes |
| --- | --- |
| `GET /api/logs` | `{"files": [{name, hour_utc, bytes, complete, keep, open, msg_counts, start_utc, end_utc}], "total_bytes", "hours", "disk_free_gb", "min_free_gb"}`. The filesystem is walked per request, so a card moved between machines shows up at once. |
| `GET /api/logs/availability?from=&to=` | One slot per hour: `{hour_utc, available, bytes, complete}`. At most 366 days per request (422). |
| `GET /api/logs/window?from=&to=` | The whole hours overlapping the window, concatenated, as `application/octet-stream`. At most 48 hours (422); 404 when no log overlaps. |
| `GET /api/logs/{name}` | One file (`SSSS_YYYYMMDD_HH.ubx`). The hour still being written streams what exists at the moment of the request. |
| `PATCH /api/logs/{name} {"keep": true}` | Mark the hour so retention never prunes it. |
| `DELETE /api/logs/{name}[?force=1]` | 409 for the hour the writer has open (`force` does not override it), for the newest hour when this daemon runs no raw logger at all (there `?force=1` does override), and for a `keep` mark (clear it with `PATCH` first). |

The daemon publishes its raw-log writer to the API, so on a base that is logging, the open hour is
known exactly: it is refused by name rather than guessed at, and the newest *closed* hour needs no
`force`. The guess only remains where no writer exists — the rover role, or a replay without
`REPLAY_LOG=1`.

## History and events

`GET /api/history?metrics=h_acc_m,nsat_used&from=…&to=…&res=auto|1s|1m` answers columns-and-rows:

```json
{"res": "1s", "columns": ["ts", "h_acc_m", "nsat_used"], "rows": [[1789861769.0, 8.68, 25]]}
```

`ts` is always the first column. `from`/`to` are ISO-8601 instants **with a timezone**; `res=auto`
takes the minute rollup above a six-hour window. The window cap is the retention horizon of the
resolution: 24 hours at `1s`, 90 days at `1m` (422 beyond it, or for `from >= to`). At `1m` the 1 s
metric names are mapped for you (`h_acc_m` → `h_acc_avg`), and the rollup's own columns
(`h_acc_max`, `nsat_used_min`, `pdop_max`, `n`) can be asked for by name. `GET /api/history/metrics`
lists what each resolution accepts; an unknown metric is a 422 that names the allowed ones.

`GET /api/events?limit=200&level=info|warning|error` is the event log, newest first by id (the host
clock can step, the id cannot). `POST /api/events/{id}/ack` marks one acknowledged (404 when
retention has already deleted it).

## Background jobs

Phase 3 ships the read side; Phase 5 adds the routes that submit exports and PPK runs.

| Route | Notes |
| --- | --- |
| `GET /api/jobs?kind=&limit=50` | Newest first. |
| `GET /api/jobs/{id}` | One job. |
| `GET /api/jobs/{id}/files` | `[{"name", "bytes"}]` — what the job wrote into its own directory. |
| `GET /api/jobs/{id}/files/{name}` | One result file. |
| `DELETE /api/jobs/{id}` | Forgets the row and deletes the directory. 409 while the job is running: stopping work and forgetting it are separate decisions. |

A job row is `{id, kind, status, created_utc, updated_utc, progress, message, params, result,
error}`. The lifecycle is `queued → running → done | failed`; `progress` is 0…1 with an optional
`message`; `params` is echoed back as submitted (and carries no secrets); `result` is the job
function's own dict; `error` is `"TypeError: …"` for a job that raised. One job runs at a time.
Every transition is published on the bus and reaches the WebSocket as topic `jobs`, so a UI never
needs to poll. Two lifecycle rules matter to the UI:

- A restart fails whatever was in flight, with `error: "interrupted by restart"`. Nothing is
  requeued — re-running half-finished work unasked could repeat what it had already written.
- A shutdown cancels the running job and records `error: "shutdown"` (or `"cancelled"`), with five
  seconds to do so.

`409 {"detail": "this daemon has no job runner"}` on every job route means exactly that — the same
URLs work on a daemon that runs one, so the UI hides the panel rather than reporting a bug.

## WebSocket `/ws`

```
ws://<host>:8080/ws?topics=pvt,sats,rtcm,svin,rf,span,ntrip,events,system,receiver,base,jobs,rawlog[&token=…]
```

One hub serves every socket from a single bus subscription, so a hundred browsers cost what one
does. `topics` is a comma-separated subset; unknown names are dropped, and asking for nothing (or
for nothing recognisable) subscribes to all of them. The server never expects a message; anything
a client sends is read and discarded, which is how a disconnect is noticed.

**First message, always:**

```json
{"type": "snapshot", "role": "base", "topics": ["pvt", "sats"], "state": { … }}
```

`state` is the same object as `GET /api/state`. Render from it, then apply the stream.

**Per receiver epoch** (one message, whatever the client asked for, driven by NAV-EOE):

```json
{"type": "epoch", "t": 1789861804.99, "pvt": {"position", "accuracy", "dops", "fix", "velocity", "time"},
 "sats": {"sats": [...], "sat_summary": {...}}, "rtcm": {...}, "svin": {...}}
```

Only the keys whose topics the client subscribed to are present; `pvt`, `sats`, `rtcm` and `svin`
ride this bundle and never arrive as their own message. `t` is the receiver's UTC as epoch
seconds, or `null` before the first time fix.

**Everything else:**

```json
{"type": "update", "topic": "receiver", "source": "receiver.connected", "data": "serial:/dev/ttyACM0"}
```

`topic` is the subscription name; **`source` is the bus topic underneath it**, which is how a
client tells `receiver.connected` from `receiver.disconnected` or `receiver.reset` — they all
arrive as topic `receiver`. The mapping:

| topic | source bus topics |
| --- | --- |
| `rf` | `state.hardware`, `state.rf` |
| `span` | `state.spectrum` (throttled to one per second — a 256-bin spectrum is the fattest payload sent) |
| `ntrip` | `ntrip.clients` |
| `events` | `events.new` |
| `system` | `system.stats` |
| `jobs` | `jobs.update` |
| `receiver` | anything `receiver.*` |
| `base` | anything `base.*` |
| `rawlog` | anything `rawlog.*` |

**Back-pressure and close codes.** Each socket has a 50-message outbox. A client that cannot keep
up is closed rather than allowed to hold the bus up behind it; it should reconnect and take a
fresh snapshot.

| Code | Meaning |
| --- | --- |
| 1001 | The hub is shutting down (the daemon is stopping). |
| 1008 | Before the handshake: the password is set and the request carried no valid token. After it: the client fell 50 messages behind. |
| 1011 | The server was started without its lifespan — a bug, not a state to retry into. |

## The healthcheck command

`mtrtk healthcheck` resolves `WEB_BIND` the same way the daemon does (`lan`/`all` → `0.0.0.0` →
asked on `127.0.0.1`; `tailscale` → the tailscale0 address, and exit 1 when there is none), GETs
`/healthz` with a three-second timeout and exits 0 only on `{"status": "ok"}`. It is what
`docker-compose.yml` runs every 30 s.
