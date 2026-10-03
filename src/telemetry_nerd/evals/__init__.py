"""Scenario evals (spec §10, bead d77.3): score a Telemetry Nerd workspace against ground truth.

- `truth`: one view over the demo scenarios' ground truth (scripts/scenario.py, schema v1) and the
  queue-sim ground truth (devtools.queue_sim.ground_truth).
- `score`: pure scoring of a workspace snapshot (GET /api/workspace plus enrichments) and an
  optional final transcript against a `Truth`; `report` renders it as JSON and markdown.
- `questions`: the neutral symptom question asked per scenario (never names the cause).
- `live`: side effects (daemon, headless Claude Code, snapshot collection) for scripts/eval_scenario.py.
"""
