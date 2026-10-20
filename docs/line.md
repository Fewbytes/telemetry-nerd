# Running on the sweatshop assembly line

telemetry-nerd is built by a sweatshop line on `sweatshop-1`: makers claim `line`-labelled beads,
and the line's merge guard fast-forwards `master` only for a commit whose clean-room `just ci`
passed. This page is what the repo owes the line.

## The required check: `just ci`

The witness runs `just ci` from the candidate commit in the `Dockerfile.line` image (the line's
`[runner] image`); makers run it before they submit. It runs, in order: `uv sync --locked`,
`just lint`, `scripts/check_version.py`, `uv run pytest`, UI `npm ci`, vitest, svelte-check and
the Playwright e2e on the fixture PromQL source. It is the GitHub `python`, `ui` and `e2e` jobs.

**Integration and network tests are GitHub-only.** `just test-integration` needs a container
socket (testcontainers), and mounting the host's socket into a check container would give agent
code root on the host. `-m network` hits the public internet and is non-blocking even on GitHub.
So GitHub `ci.yml` stays the gate for integration, on every push to `master`, and a red there
is a P0 bug like any other red master (the line does not see it). Revisit if a scoped socket
(or service containers started by the check runner) becomes available.

## The image: `Dockerfile.line`

`just line-image` builds it `FROM` the line's `sweatshop-station` image and bakes: managed Python
3.14 (`UV_PYTHON`), a warmed uv cache for `uv.lock`, a warmed npm cache for `ui/package-lock.json`
and Chromium for the Playwright version in that lockfile. After the build `just ci` needs no
network when the lockfiles are unchanged; when a lockfile changed, the difference is downloaded.
Rebuild the image after a Playwright or Python bump to keep checks fast.

## Gated paths

The constitution gates, to role `gemba` (the owner), the files that define how a check runs, so a
maker cannot weaken its own check in the commit that is judged by it: `justfile`, `Dockerfile.line`,
`.github/**`, `scripts/check_version.py`. Change them directly on the host, not through a maker.

## Autonomous writers

Only line stations push to `master`. The GitHub issue-triage workflow stays: it labels issues and
pushes reproduction tests to `repro/issue-<n>` branches, never `master`. The auto-implement
workflow was removed: it opened implementation PRs for the same code the line owns.
