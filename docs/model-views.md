# Model views: USE, RED and Little's law

Telemetry Nerd can look at a service or resource through a **binding**: which metrics play which
role of a model.

| model | roles | question |
|---|---|---|
| RED | rate, errors, duration | is this request path healthy? |
| USE | utilization, saturation, errors | is this resource busy or queueing? |
| Little's law | arrival_rate, latency, concurrency | do in-flight requests, throughput and mean latency agree? |

Tools (MCP): `binding_suggest` proposes bindings from names, knowledge packs and relations;
`binding_accept` / `catalog_bind` confirm one; `show_binding` draws it as one linked panel group;
`binding_verdict` says which signals moved against a stated reference, when, and which first;
`check_littles_law` compares measured concurrency with throughput x mean latency.

What the numbers mean and how they are reported:

- Verdicts compare against reference windows (previous windows, same hour on previous days or
  weeks, or the operating profile) and control the false-alarm rate over all roles at once
  (default 5%). Ordering "A moved first" is stated only when the onset intervals do not overlap.
- Latency is judged as the share of requests above a stated bucket edge, never as an averaged
  percentile. Errors are a share with a Wilson interval on effective sample size.
- Little's law uses the MEAN latency (`_sum` / `_count`); percentile-only latency is refused. The
  discrepancy L - lambda W (absolute and relative, with its measurement interval) is always shown.
  Each variation is labelled by source: measurement system (the interval; a systematic offset,
  e.g. L / (lambda W) above 1 in most windows = time the latency timer does not cover), common
  cause (small-system fluctuation at this traffic, the windows' own spread) or special cause
  (transient windows, e.g. at a load peak leaving steady state). Without a concurrency gauge the
  check says it cannot be done.
- Every statistic is citable evidence; unknown input uncertainty is flagged, not hidden.

For Claude: the `model-views` skill (`skills/model-views/`) holds the workflow, reporting rules and
executed examples (`tests/unit/test_model_views_skill.py`). Design notes:
`docs/superpowers/specs/2026-10-02-littles-law-design.md`,
`docs/superpowers/specs/2026-10-02-binding-verdicts-design.md`.
