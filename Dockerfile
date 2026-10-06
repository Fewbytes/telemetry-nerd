# syntax=docker/dockerfile:1
# Telemetry Nerd daemon image (bead ijg.1): workspace UI + HTTP API + MCP at /mcp.
# Build:  just docker-build        Run:  just docker-run
# Tier-2 code (bead b98) runs as IPython kernel subprocesses inside this container (no sandbox
# image); the scientific stack comes with the `analysis` extra: `--build-arg EXTRAS=analysis`
# builds the -full variant (just docker-build-full).

FROM node:26-trixie-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS build
WORKDIR /src
ARG EXTRAS=""
COPY pyproject.toml uv.lock hatch_build.py ./
COPY src/ src/
# ui/dist is prebuilt by the node stage, so hatch_build.py bundles it without needing node.
COPY --from=ui /ui/dist ui/dist
COPY ui/package.json ui/package.json
# ruptures has no cp314 wheel yet (sdist only), so the full variant needs a compiler here; the
# toolchain stays in this build stage and never reaches the final image.
RUN uv build --wheel --out-dir /dist \
 && if [ -n "$EXTRAS" ]; then apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential; fi \
 && uv venv /opt/venv \
 && spec=$(ls /dist/*.whl) \
 && if [ -n "$EXTRAS" ]; then spec="$spec[$EXTRAS]"; fi \
 && VIRTUAL_ENV=/opt/venv uv pip install "$spec"
# Slim the venv (~-150 MB): strip debug symbols from native extensions and drop pyarrow's
# C++/Cython development files, tests, and unused feature modules, none of which this codebase
# imports at runtime (checked with `import pyarrow.<x>` grep across src/). pyarrow's core
# lib.so hard-links libarrow_{compute,dataset,acero,substrait} and libparquet at dlopen time
# (see `otool -L`/`ldd` on lib*.so) and Table.group_by().aggregate() lazily imports
# pyarrow.acero, so those dylibs and the acero binding stay; only Flight (RPC server/client,
# ~20 MB of libarrow_flight) and the standalone format/filesystem bindings (csv/json/feather/
# parquet/dataset/orc readers, s3/gcs/azure/hdfs filesystems) are safe to drop. Tier-2 kernels
# (ipykernel) never use the debugger (debugpy) or tab completion (jedi/parso), and both are
# optional imports there: dropping them saves ~43 MB. The analysis extra's packages (scipy,
# statsmodels, scikit-learn, pandas, ...) ship test suites inside the package: drop those too.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils \
 && sp=$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') \
 && rm -rf "$sp/pyarrow/include" "$sp/pyarrow/tests" "$sp/pyarrow/parquet" \
 && find "$sp/pyarrow" -type f \( -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' -o -name '*.h' -o -name '*.cc' \) -delete \
 && rm -f "$sp"/pyarrow/_flight*.so "$sp"/pyarrow/libarrow_flight* "$sp"/pyarrow/libarrow_python_flight* "$sp"/pyarrow/flight.py \
 && rm -f "$sp"/pyarrow/_csv*.so "$sp"/pyarrow/csv.py \
 && rm -f "$sp"/pyarrow/_json*.so "$sp"/pyarrow/json.py \
 && rm -f "$sp"/pyarrow/_feather*.so "$sp"/pyarrow/feather.py \
 && rm -f "$sp"/pyarrow/_parquet.cpython*.so "$sp"/pyarrow/_parquet_encryption*.so "$sp"/pyarrow/libarrow_python_parquet_encryption* \
 && rm -f "$sp"/pyarrow/_dataset.cpython*.so "$sp"/pyarrow/_dataset_parquet*.so "$sp"/pyarrow/_dataset_orc*.so "$sp"/pyarrow/dataset.py \
 && rm -f "$sp"/pyarrow/_orc*.so "$sp"/pyarrow/orc.py \
 && rm -f "$sp"/pyarrow/_s3fs*.so "$sp"/pyarrow/_gcsfs*.so "$sp"/pyarrow/_azurefs*.so "$sp"/pyarrow/_hdfs*.so \
 && rm -f "$sp"/pyarrow/_pyarrow_cpp_tests*.so \
 && find "$sp" -type d -name tests \( -path "$sp/scipy/*" -o -path "$sp/statsmodels/*" -o -path "$sp/sklearn/*" -o -path "$sp/pandas/*" -o -path "$sp/pywt/*" -o -path "$sp/ruptures/*" \) -prune -exec rm -rf {} + \
 && rm -rf "$sp/debugpy" "$sp/jedi" "$sp/parso" \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -type f \( -name '*.so' -o -name '*.so.[0-9]*' \) -exec strip --strip-unneeded {} + \
 && /opt/venv/bin/python -c 'import duckdb, numpy, polars, pyarrow, pyarrow.compute, telemetry_nerd.cli' \
 && /opt/venv/bin/python -c 'import pyarrow as pa; t = pa.table({"a": [1, 1, 2]}); assert t.group_by("a").aggregate([([], "count_all")]).num_rows == 2' \
 && if [ -n "$EXTRAS" ]; then /opt/venv/bin/python -c 'import scipy.stats, statsmodels.api, sklearn.cluster, ruptures, pywt'; fi \
 && /opt/venv/bin/python -c 'import ipykernel.ipkernel, jupyter_client, zmq; from ipykernel.debugger import _is_debugpy_available as d; assert not d'

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
