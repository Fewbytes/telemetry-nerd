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
