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
