"""Hatch build hook: bundle the built Svelte UI into the wheel as telemetry_nerd/ui_dist.

Choice (bead ijg.3): the UI is built at wheel-build time, never committed. If ui/dist is
already there (Docker's node stage, a dev checkout) it is used as is; otherwise the hook runs
`npm ci && npm run build` in ui/. So `uv tool install git+...` needs node/npm on the
machine; a wheel built once (or from the Docker image) does not. Set TN_SKIP_UI_BUILD=1 to
build a UI-less wheel (the daemon then serves the API only and warns).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class UiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel" or os.environ.get("TN_SKIP_UI_BUILD"):
            return
        ui = Path(self.root) / "ui"
        dist = ui / "dist"
        if not (dist / "index.html").exists():
            npm = shutil.which("npm")
            if npm is None or not (ui / "package.json").exists():
                raise RuntimeError(
                    "telemetry-nerd wheel needs the built UI: install node/npm (the hook runs "
                    "`npm ci && npm run build` in ui/), pre-build ui/dist, or set "
                    "TN_SKIP_UI_BUILD=1 for an API-only wheel"
                )
            subprocess.run([npm, "ci"], cwd=ui, check=True)
            subprocess.run([npm, "run", "build"], cwd=ui, check=True)
        build_data["force_include"][str(dist)] = "telemetry_nerd/ui_dist"
