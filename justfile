set shell := ["bash", "-cu"]

default: test

test:
    uv run pytest

test-integration:
    uv run pytest -m integration

lint:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

dev-up:
    podman compose -f deploy/dev/compose.yml up -d

dev-down:
    podman compose -f deploy/dev/compose.yml down

seed hours="6":
    uv run python scripts/seed_synthetic.py --url http://127.0.0.1:8428 --hours {{hours}}

# Run a queue-sim scenario (deploy/queue-sim/scenarios/<name>.yaml) as an exporter on :9201;
# ground truth goes to build/queue-sim/<name>.json. Extra args: --port N --speed X --truth F.
queue-sim scenario *args:
    uv run scripts/queue_sim.py {{scenario}} {{args}}

serve *args:
    uv run telemetry-nerd serve {{args}}

ui-install:
    cd ui && npm install

ui-build:
    cd ui && npm run build

ui-test:
    cd ui && npm test

ui-check:
    cd ui && npx svelte-check --tsconfig ./tsconfig.app.json --fail-on-warnings

ui-dev:
    cd ui && npm run dev

# Fresh VictoriaMetrics data each run (dev data is synthetic, so recreating the
# volume is safe); then build the UI and run Playwright against a throwaway daemon.
e2e:
    podman compose -f deploy/dev/compose.yml down -v
    just dev-up
    just ui-build
    cd ui && npx playwright install chromium && npx playwright test

# Render brand PNGs, favicon.ico and the social card from assets/brand/*.svg into assets/brand/dist.
brand:
    uv run --script scripts/build_brand.py

# Build the wheel (bundles the UI), install it into throwaway dirs, smoke-test serve.
verify-install:
    uv run scripts/verify_install.py

image := "telemetry-nerd:dev"

# Build the daemon image (podman by default; `CONTAINER=docker just docker-build` works too).
# podman needs --format docker for the HEALTHCHECK to be kept in the image config.
docker-build:
    c="${CONTAINER:-podman}"; fmt=""; [ "$c" = podman ] && fmt="--format docker"; \
        $c build $fmt -t {{image}} .

# Build the -full variant (adds the `analysis` extra: scipy, statsmodels, scikit-learn, ...).
docker-build-full:
    c="${CONTAINER:-podman}"; fmt=""; [ "$c" = podman ] && fmt="--format docker"; \
        $c build $fmt --build-arg EXTRAS=analysis -t {{image}}-full .

# Run the daemon image: UI at http://127.0.0.1:7070, data in the named volume tn-data.
# Set TN_SOURCE_URL to a source reachable from the container (host: host.containers.internal).
docker-run *args:
    ${CONTAINER:-podman} run --rm -p 127.0.0.1:7070:7070 -v tn-data:/data \
        -e TN_SOURCE_URL="${TN_SOURCE_URL:-http://host.containers.internal:8428}" {{args}} {{image}}

lab-up:
    podman compose -f deploy/missing-data-lab/compose.yml up -d

lab-down:
    podman compose -f deploy/missing-data-lab/compose.yml down -v

# --- OpenTelemetry demo (deploy/demo; docs/demo.md): own compose project tn-demo, VM on :8429 ---
demo_compose := "podman compose -f deploy/demo/compose.yml"

# Bring the trimmed demo up; `just demo-up full` adds frontend-proxy/flagd-ui/image-provider.
demo-up profile="":
    {{demo_compose}} {{ if profile != "" { "--profile " + profile } else { "" } }} up -d

# Stop the demo; the VictoriaMetrics volume (tn-demo_tn-demo-vmdata) is kept.
demo-down:
    {{demo_compose}} --profile full --profile spike --profile queue-sim down

# Stop the demo AND wipe its metrics volume. Never touches deploy/dev.
demo-reset:
    {{demo_compose}} --profile full --profile spike --profile queue-sim down -v

demo-ps:
    {{demo_compose}} --profile full --profile spike --profile queue-sim ps

# List / get / set flagd flags: `just demo-flag set paymentFailure 50%`, `just demo-flag off`.
demo-flag *args:
    uv run scripts/demo_flag.py {{args}}

# --- Scenarios (docs/demo.md, bead 1h9.5): flagd fault schedules + ground-truth JSON ---

# List scenario definitions (scenarios/*.yml).
scenario-list:
    uv run scripts/scenario.py list

# Run one against the running demo in real time (~13 min); writes scenarios/runs/<name>-<ts>.json.
# Extra args: --scale 0.3 (shorter), --dry-run, --force.
scenario name *args:
    uv run scripts/scenario.py run {{name}} {{args}}
