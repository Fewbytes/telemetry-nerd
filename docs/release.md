# Releasing

Releases are cut by the **Release** workflow (`.github/workflows/release.yml`). Never push `v*`
tags by hand: a tag must imply that everything passed.

## Procedure

1. Bump the version in `pyproject.toml`, `.claude-plugin/plugin.json` and
   `.claude-plugin/marketplace.json` (`python3 scripts/check_version.py` must pass) and merge to
   `master`.
2. GitHub, Actions, **Release**, Run workflow on `master`, `version` = `X.Y.Z` (no `v`).
   Or: `gh workflow run release.yml -f version=X.Y.Z`.
3. The workflow runs, in order:
   - `prepare`: refuses unless run on `master`, the version is `X.Y.Z`, the tag does not exist and
     `check_version.py vX.Y.Z` passes; records the master HEAD sha.
   - `ci` (reusable `ci.yml`): `python` (lint, unit tests), `ui`, `e2e` (Playwright on the fixture
     source) and `integration` (`pytest -m integration`, VictoriaMetrics testcontainers) must all be
     green. The `network` job (`-m network`, public internet) runs with one retry but never blocks.
   - `tag`: only after all of the above, creates the annotated tag `vX.Y.Z` on the tested sha and
     pushes it.
   - `image`: builds and pushes the slim and `-full` images (`image.yml`) for that tag.
4. If a gate fails nothing is tagged or published; fix and run the workflow again.

## Why the image is built inside the workflow

A tag pushed with the default `GITHUB_TOKEN` does not trigger other workflows, so `ci.yml`'s tag
trigger would never fire for it. `release.yml` therefore calls `image.yml` directly. `ci.yml`
keeps a tag-push `image` job (same gates: python, ui, e2e, integration) only as a safety net for a
tag pushed by a person.

## Test groups

| Marker | What | Command | CI |
| --- | --- | --- | --- |
| (none) | unit tests | `just test` | required |
| `integration` | VictoriaMetrics containers (testcontainers; needs a Docker-compatible socket) | `just test-integration` | required, gates release |
| `network` | public-internet sources, Grafana, Wikimedia | `just test-network` | non-blocking, one retry |
