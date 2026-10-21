# Staging

A podman container `tn-staging` on `sweatshop-1`, so the king and Gemba can use the current
master build. Decided by the king (2026-10-21, card `telemetry-nerd-sini`):

- **Trigger:** a sha is deployed only when the slower watcher suites pass on that landed sha
  and andon is clear. Not every landing.
- **State:** persistent, in the podman volume `tn-staging-data`; it survives redeploys.

## Deploy

`deploy/staging/deploy.sh` does one deploy; run it with `bash`, since the merge guard rejects
executables in the repo:

```
bash deploy/staging/deploy.sh <repo-dir> <sha>
```

It builds the `Dockerfile` image from `git archive <sha>` with `--build-arg GIT_SHA`, replaces
the container and waits until `/api/health` reports that sha. Running it again for the sha
already deployed does nothing. A failed deploy exits non-zero with the container log tail; it
touches nothing on the line, so landings and the andon cord are unaffected.

Settings (environment): `TN_STAGING_PORT` (default 7071), `TN_STAGING_BIND` (default
127.0.0.1; the daemon has no authentication, never bind a public interface),
`TN_STAGING_ALLOWED_HOSTS` (the Host names people use, see `Dockerfile`),
`TN_STAGING_SOURCE_URL`.

## Which sha is running

`curl http://127.0.0.1:7071/api/health` on `sweatshop-1` returns `"commit": "<12-char sha>"`
(from `TN_COMMIT`, baked in by the build arg). `podman inspect tn-staging` shows the same in the
label `tn.staging.sha`.

## Roll back

Run the deploy again with an earlier sha. The data volume is not rolled back, so a build that
migrates the data format in a way an older build cannot read needs the volume restored by hand
(`podman volume export tn-staging-data` beforehand).

## Who triggers it (needs the king)

Stations hold only a station token and cannot reach the podman socket, and a maker must not.
The trigger therefore has to be line infrastructure or a host-side unit on `sweatshop-1` that:
waits for the watcher suites to pass on the new master sha, checks andon is clear, then runs the
script as the user that owns the podman socket. Which identity that is, and the watcher or
constitution change that fires it, is not decided and is sovereign-gated: card
`telemetry-nerd-sini` carries the proposal. Until then the script is run by hand.
