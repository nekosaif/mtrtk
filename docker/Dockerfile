# syntax=docker/dockerfile:1.7

# ---------- 1. web UI ----------
FROM node:22-alpine AS web
WORKDIR /web
RUN corepack enable && corepack prepare pnpm@11 --activate
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---------- 2. RTKLIB demo5 (convbin, rnx2rtkp) ----------
FROM debian:bookworm-slim AS rtklib
ARG RTKLIB_TAG=v2.5.1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential gfortran git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${RTKLIB_TAG} https://github.com/rtklibexplorer/RTKLIB.git /rtklib
RUN make -C /rtklib/app/consapp/convbin/gcc -j"$(nproc)" \
    && make -C /rtklib/app/consapp/rnx2rtkp/gcc -j"$(nproc)" \
    && strip /rtklib/app/consapp/convbin/gcc/convbin /rtklib/app/consapp/rnx2rtkp/gcc/rnx2rtkp

# ---------- 3. runtime ----------
FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 DATA_DIR=/data PATH="/usr/local/mtrtk/bin:/app/.venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends tini gzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY --from=rtklib /rtklib/app/consapp/convbin/gcc/convbin /rtklib/app/consapp/rnx2rtkp/gcc/rnx2rtkp /usr/local/bin/
COPY --from=web /web/dist ./src/mtrtk/web/static
# The daemon runs as `mtrtk` (uid/gid 1000, in dialout = gid 20 as on Debian hosts); the image
# stays USER root only so docker/entrypoint.sh can fix /data's ownership before dropping to it.
# The web UI's settings file lives in /data: /app is root-owned, and the data volume is what
# survives a recreate.
RUN groupadd -g 1000 mtrtk && useradd -u 1000 -g 1000 -G dialout -m -s /usr/sbin/nologin mtrtk \
 && mkdir -p /data && chown mtrtk:mtrtk /data
ENV MTRTK_ENV_FILE=/data/.env
COPY --chmod=0755 docker/entrypoint.sh /entrypoint.sh
# `mtrtk` on PATH runs the real one as uid 1000 even from a root `docker exec` (and the root
# healthcheck), so doctor checks the daemon's access, not root's, and writes stay the daemon's.
COPY --chmod=0755 docker/mtrtk-as-daemon.sh /usr/local/mtrtk/bin/mtrtk
VOLUME ["/data"]
EXPOSE 8080 2101
# The entrypoint ends in `tini -s -- mtrtk run` as uid 1000: tini forwards SIGTERM and reaps the
# convbin / rnx2rtkp children of jobs.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["mtrtk", "run"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["mtrtk", "healthcheck"]
