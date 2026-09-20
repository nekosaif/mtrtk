# mtrtk web UI

`pnpm install`, then `pnpm dev` (Vite on :5173, proxies `/api`, `/healthz` and `/ws` to a daemon on :8080), `pnpm test` (vitest), `pnpm lint` (tsc) and `pnpm build` (→ `dist/`, which `docker/Dockerfile` copies into the image).
`pnpm build:static` builds and copies `dist/` to `../src/mtrtk/web/static/` so a local `uv run mtrtk base` serves the SPA; both paths are git-ignored.
Design tokens (colours, fonts, radii, light/dark) live once in `src/index.css`; see `docs/superpowers/plans/2026-09-19-phase4-frontend.md` for the binding design system.
