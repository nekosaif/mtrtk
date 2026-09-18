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
    PYTHONUNBUFFERED=1 DATA_DIR=/data PATH="/app/.venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends tini gzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY --from=rtklib /rtklib/app/consapp/convbin/gcc/convbin /rtklib/app/consapp/rnx2rtkp/gcc/rnx2rtkp /usr/local/bin/
COPY --from=web /web/dist ./src/mtrtk/web/static
VOLUME ["/data"]
ENTRYPOINT ["tini", "--", "mtrtk"]
CMD ["run"]
