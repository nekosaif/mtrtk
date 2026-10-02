# Setup

## Updating

### Docker images

`docker-compose.yml` runs `ghcr.io/nekosaif/mtrtk:latest`, and the `ros2` profile runs
`ghcr.io/nekosaif/mtrtk-ros2:${ROS_DISTRO:-humble}`. Every image is built for `linux/amd64` and
`linux/arm64`. The tags are:

| Tag | What it is | When it changes |
|---|---|---|
| `mtrtk:latest` | The newest release. This is the compose default. | When a `vX.Y.Z` release is published, and only if it is the newest release. |
| `mtrtk:X.Y.Z` (e.g. `mtrtk:0.1.0`) | One release, pinned. | Never. |
| `mtrtk:edge` | The current `main` branch. Its tests have passed, but it is not a release. | On every push to `main`. |
| `mtrtk-ros2:humble`, `mtrtk-ros2:jazzy` | The newest release of the ROS 2 bridge for that distro. | With `mtrtk:latest`. |
| `mtrtk-ros2:X.Y.Z-humble`, `mtrtk-ros2:X.Y.Z-jazzy` | One release of the bridge, pinned. | Never. |

To update to the newest release:

```bash
git pull                     # compose file, .env.example and docs for the new release
docker compose pull
docker compose up -d
```

Read the release's section in `CHANGELOG.md` first, and copy any new settings from
`.env.example` into `.env`. Settings that are not in `.env` keep their defaults.

`:latest` follows releases, not `main`. To pin a release or to track `main`, change the image in
a `docker-compose.override.yml` next to `docker-compose.yml`. Compose reads that file
automatically, so `git pull` never conflicts with it:

```yaml
services:
  mtrtk:
    image: ghcr.io/nekosaif/mtrtk:0.1.0   # or :edge to follow main
```

Then run `docker compose pull && docker compose up -d`. Delete the override to go back to
`:latest`. Before the first release, `:latest` is the last image that `main` built under the old
tagging, and it no longer changes. Use `:edge` until `0.1.0` is out.

To build from source rather than pull, run
`git pull && docker compose build && docker compose up -d`.
