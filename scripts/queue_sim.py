# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pyyaml"]
# ///
"""Run a queue-sim scenario as a Prometheus exporter (bead telemetry-nerd-1h9.17).

  uv run scripts/queue_sim.py <scenario|path.yaml> [--port 9201] [--truth out.json] [--speed 1]

See telemetry_nerd.devtools.queue_sim for the model, knobs and the scenario format.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telemetry_nerd.devtools.queue_sim import main

if __name__ == "__main__":
    main()
