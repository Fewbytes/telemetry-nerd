#!/usr/bin/env bash
# Deploy one master sha to staging: a podman container `tn-staging` on this host, state in the
# persistent volume `tn-staging-data` (survives redeploys). Run by a host-side unit once the
# watcher suites pass on the sha and andon is clear (docs/staging.md); not run by makers.
#
#   bash deploy/staging/deploy.sh <repo-dir> <sha>
#
# Idempotent: when tn-staging already runs <sha> and reports it on /api/health, it does nothing.
# Roll back by running it again with an earlier sha. On failure it exits non-zero after printing
# the container log tail, and leaves the previous image tagged so a re-run can restore it.
set -euo pipefail

repo=${1:?usage: deploy.sh <repo-dir> <sha>}
sha=${2:?usage: deploy.sh <repo-dir> <sha>}
[[ $sha =~ ^[0-9a-f]{7,40}$ ]] || { echo "deploy: not a hex sha: $sha" >&2; exit 2; }
short=$(git -C "$repo" rev-parse --short=12 "$sha^{commit}")
port=${TN_STAGING_PORT:-7071}
bind=${TN_STAGING_BIND:-127.0.0.1}
name=tn-staging
image=localhost/telemetry-nerd:staging-$short

health_commit() {
    curl -fsS --max-time 3 "http://127.0.0.1:$port/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"])'
}

if podman container exists "$name" && [ "$(health_commit 2>/dev/null)" = "$short" ]; then
    echo "deploy: $name already runs $short"
    exit 0
fi

src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git -C "$repo" archive "$short" | tar -x -C "$src"
podman build --format docker --build-arg "GIT_SHA=$short" -t "$image" "$src"

podman rm -f "$name" >/dev/null 2>&1 || true
podman run -d --name "$name" --label "tn.staging.sha=$short" \
    -p "$bind:$port:7070" -v tn-staging-data:/data \
    -e TN_ALLOWED_HOSTS="${TN_STAGING_ALLOWED_HOSTS:-}" \
    ${TN_STAGING_SOURCE_URL:+-e "TN_SOURCE_URL=$TN_STAGING_SOURCE_URL"} \
    --restart unless-stopped "$image"

for _ in $(seq 60); do
    if [ "$(health_commit 2>/dev/null)" = "$short" ]; then
        echo "deploy: staging runs $short"
        exit 0
    fi
    sleep 1
done
echo "deploy: $name did not report $short on /api/health" >&2
podman logs --tail 50 "$name" >&2 || true
exit 1
