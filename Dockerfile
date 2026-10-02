# syntax=docker/dockerfile:1
# Telemetry Nerd daemon image (bead ijg.1): workspace UI + HTTP API + MCP at /mcp.
# Build:  just docker-build        Run:  just docker-run
# The M5 tier-2 sandbox image (bead b98) is a separate image (deploy/sandbox/); this one
# stays free of Jupyter/kernel deps.

FROM node:26-trixie-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS build
WORKDIR /src
COPY pyproject.toml uv.lock hatch_build.py ./
COPY src/ src/
# ui/dist is prebuilt by the node stage, so hatch_build.py bundles it without needing node.
COPY --from=ui /ui/dist ui/dist
COPY ui/package.json ui/package.json
RUN uv build --wheel --out-dir /dist \
 && uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install /dist/*.whl

FROM python:3.14-slim-trixie
RUN useradd --system --create-home --uid 10001 tn \
 && mkdir /data && chown tn:tn /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TN_DATA_DIR=/data \
    TN_HOST=0.0.0.0 \
    TN_PORT=7070
# Inside the container the daemon binds 0.0.0.0 (a loopback bind is unreachable through the
# published port). The Host/Origin allowlist stays loopback-only: reach it via
# `-p 127.0.0.1:7070:7070` -> http://127.0.0.1:7070. Other hostnames need TN_ALLOWED_HOSTS.
# There is no authentication: never publish the port on a public interface.
USER tn
VOLUME /data
EXPOSE 7070
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7070/api/health', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["telemetry-nerd"]
CMD ["serve"]
