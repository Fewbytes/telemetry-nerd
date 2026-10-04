set shell := ["bash", "-cu"]

default: test

test:
    uv run pytest

# Container tests (testcontainers: needs a Docker-compatible socket). Required in CI.
test-integration:
    uv run pytest -m integration

# Public-internet tests (public sources, Grafana, Wikimedia). Flaky by nature; non-blocking in CI.
test-network:
    uv run pytest -m network

lint:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

site-serve:
    cd site && hugo server --buildDrafts

site-build:
    cd site && hugo --minify

site-deploy: site-build
    cd site && npx wrangler deploy

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

# Build the UI and run Playwright against a throwaway daemon whose only source is the fixture
# PromQL server (src/telemetry_nerd/devtools/promfixture, started by playwright.config.ts):
# deterministic synthetic data, no VictoriaMetrics, no podman. Extra args go to playwright
# (e.g. `just e2e e2e/fleet.spec.ts`); E2E_PORT / E2E_FIXTURE_PORT move the two servers.
e2e *args:
    just ui-build
    cd ui && npx playwright install chromium && npx playwright test {{args}}

# Flake hunt (bead zek0.1): every test x n, with 4x CPU throttling. A pass-on-retry also fails
# (failOnFlakyTests), so any red here is a real flake or a real bug, never noise.
e2e-stress n="5":
    just ui-build
    cd ui && npx playwright install chromium && E2E_CPU_THROTTLE=4 npx playwright test --repeat-each {{n}}

# Unit suite x n under different PYTHONHASHSEEDs and pytest-randomly seeds, then vitest x n with
# different shuffle seeds (random order is on by default; this pins and varies the seeds).
test-stress n="3":
    uv run python scripts/stress_unit.py {{n}}
    cd ui && for i in $(seq {{n}}); do npx vitest run --sequence.seed=$i || exit 1; done

# Serve the e2e fixture PromQL source by hand (port 7079; --port N), e.g. to point `just serve
# --source-url http://127.0.0.1:7079 --source-flavor prometheus` at it.
fixture-source *args:
    uv run python -m telemetry_nerd.devtools.promfixture {{args}}

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

# --- Scenario evals (bead d77.3, spec §10): score an investigation against ground truth ---

# Offline by default (canned snapshot fixture or --snapshot/--truth). --live runs the scenario
# and an isolated daemon; --live --claude adds headless Claude Code (SPENDS TOKENS; never in CI).
eval scenario *args:
    uv run scripts/eval_scenario.py {{scenario}} {{args}}
