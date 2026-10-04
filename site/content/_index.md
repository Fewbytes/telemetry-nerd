---
title: "Telemetry Nerd"
description: "Evidence-first telemetry analysis for you and your agent."
---

Telemetry Nerd is a workspace where you and Claude investigate your metrics together. Claude
queries your datasource (Prometheus, VictoriaMetrics, Thanos, Mimir and more), draws graphs into
a shared browser workspace, and writes down what it thinks is going on as hypotheses and
findings. Every finding points at the evidence behind it. You see the same graphs, ask about any
part of them, and mark hypotheses supported or refuted.

![The workspace: a latency panel with its min–max envelope and missing-data rug, and Claude's hypotheses in the sidebar](/img/workspace.png)

## It is, and it isn't

It **is** an analysis workspace for people who need to understand what their systems are doing:
during an incident, in a capacity review, or when a graph looks wrong.

It **isn't** an "AI SRE" that takes actions on your behalf, a dashboarding tool, or an alerting
system. It reads metrics; it never changes your infrastructure. Read the [rationale](/rationale/)
for why that distinction, and the rest of the design, is deliberate.

## What you can do with it

- Ask questions in plain language and get graphs that each answer one stated question.
- See whether a metric is unusual for this time of day or week (seasonal comparison).
- Find cycles in a series with periodograms and spectrograms.
- Compare distributions across time windows, and count exactly how many requests crossed a
  latency threshold.
- Analyse many series of one metric as a fleet: the spread across hosts, and which members are
  outliers, instead of a hundred overlapping lines.
- Keep track of the investigation: hypotheses, findings, open questions and annotations, all in
  one place that both you and Claude can see.

It works with any Prometheus-compatible source. No setup needed to try it — Claude can connect to
several public demo sources (Grafana Play, Wikimedia, the VictoriaMetrics playground and more).

[Get started →](/getting-started/)
