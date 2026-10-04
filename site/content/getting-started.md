---
title: "Getting Started"
description: "Install Telemetry Nerd, ask Claude your first question, and see what an evidence-first workspace shows you that a dashboard doesn't."
---

This page gets you from nothing to a working investigation in a few minutes, then walks through
the questions that show what Telemetry Nerd does differently. You don't need your own metrics to
follow along: every example works against public demo sources.

## Install and run

You need [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/).

1. Install Telemetry Nerd:

   ```bash
   uv tool install git+https://github.com/Fewbytes/telemetry-nerd
   ```

2. Add the plugin in Claude Code:

   ```
   /plugin marketplace add Fewbytes/telemetry-nerd
   /plugin install telemetry-nerd@telemetry-nerd
   ```

3. Open the workspace at <http://127.0.0.1:7070>. The plugin starts the local daemon on demand,
   so once Claude Code is running with the plugin, the workspace is there.

That's it. There is nothing to configure before your first question: Claude can connect to
several public demo sources, including Grafana Play (a hosted OpenTelemetry demo shop, among
others), Wikimedia's Thanos (a year of history from a very large production fleet) and the
VictoriaMetrics playground.

**Prefer a container?** Images are published for `linux/amd64` and `linux/arm64`, with a slim
variant and a `-full` variant that adds scientific Python libraries for Claude's analysis code.
You run the container, add the plugin as above, and point Claude Code at the container's daemon.
The install docs in the repository cover the exact command, data volumes, upgrades and the network
security notes (the workspace has no authentication, so it should only ever listen on loopback).

> **Status:** Telemetry Nerd is early. It handles metrics only for now (logs and traces are
> planned), and its interfaces are still changing.

## Your first question

With the workspace open in your browser, ask Claude something in plain language, in Claude Code:

> Connect to grafana-play and show me whether checkout latency got worse in the last 3 hours.

Here is what happens:

1. **Claude connects the source.** `grafana-play` is one of the built-in public sources, so there
   is no URL or credential to supply.
2. **Claude queries the metrics.** It finds the checkout service's latency histogram and pulls
   the last three hours, together with an earlier stretch to compare against.
3. **A panel appears in the workspace.** The panel's header states the one question it answers,
   for example "Did checkout latency get worse in the last 3 hours?". It is drawn for that
   question, not as a general-purpose view of the metric.
4. **Claude writes down what it thinks.** If something looks off, it records a hypothesis, and
   any conclusion becomes a finding that links to the panel and the statistic behind it.

You and Claude are looking at the same workspace. Anything Claude draws shows up in your browser
straight away, and anything you do in the browser is sent back to Claude.

If you'd rather have Claude drive a whole investigation, there is a command for that:

```
/telemetry-nerd:investigate is checkout slower than usual right now?
```

It scopes the question, records a starting hypothesis, and works through it, with every claim tied
to evidence.

## The workspace basics

A new workspace has only a few kinds of objects, and they all point at each other.

### Panels

Each panel answers **one explicit question**, written in its header. Below the graph is a
provenance footer that tells you what was queried, at what resolution, and how it was aggregated,
plus any caveats (missing data, low sample counts, and so on). A clean panel has no caveat
clutter. Caveats only appear when there is something to say.

### Hypotheses and findings

A **hypothesis** is something Claude (or you) thinks might explain what you're seeing, such as
"payment errors are driving checkout failures". A hypothesis can be supported, refuted or
inconclusive, and refuted ones move to a "ruled out" list instead of disappearing.

A **finding** is a scoped claim with **evidence links**: the panel and the statistic it rests on,
plus the exact source, selector, time range and aggregation it applies to. When Claude says
something, you can click through to see exactly why.

### Annotations

Annotations mark events and regions on the shared time axis (a deploy, the start of an
incident, a window you want to compare), so every panel lines up against "what else happened then?".

### Talking back to Claude from a graph

Select any part of a graph and you get a small menu:

- **Ask Claude…** about that selection ("why does this bump start here?"). The question arrives
  with the panel and time range attached, so you don't need to describe it.
- **Mark region** or **Mark event** to annotate it.
- **Focus** to zoom the investigation onto that window.
- **Distribution here**, on latency-type data, to see the full distribution for the selected span.

You can also mark any hypothesis supported or refuted yourself. Your verdict counts. Claude sees
it and works from it.

## What you'd see that a dashboard wouldn't show you

This is where Telemetry Nerd differs from the graphs you're used to. Each example below is
something you can type, followed by what you get back.

### Peaks that survive zooming out

> Show me checkout p99 latency on grafana-play over the last 7 days.

On most dashboards a 5-minute spike disappears once the graph averages it into 30-minute points.
Here every rolled-up line carries its **min–max envelope**, so a short spike still reaches its real
height on a week-long view. Lines are drawn stepped, because each point stands for a whole bucket of
time and connecting them would suggest a smoothness that was never measured.

### Missing data that is shown, not hidden

> Show me request rate per Wikimedia site for the last 24 hours, and tell me if any data is missing.

Gaps aren't bridged with a straight line, and they aren't filled with zeros. When buckets are
empty, partial, or couldn't be fetched, a **rug** under the graph marks them, with a different pattern
for each case, and a caveat names them. Claude's summaries report coverage, the longest gap and any
series that went silent. This matters because the series that stops reporting is often the sick
one.

### "Is this unusual for this time of day?"

> Is the current request rate on wikimedia-thanos unusual for this time of day, or is it the normal
> daily pattern?

A seasonal comparison checks the current window against the **same phase of previous cycles**: the
same hours on each of the last seven days, the same window over the last four weeks, and the
immediately preceding windows. The normal band comes from the spread *across* those cycles, not from
one noisy reference week, and the answer is a verdict (usual, unusual and in which direction, or "not
enough history") with the reference it used stated. Holidays and atypical cycles can be
excluded, and each dropped cycle is listed with its reason.

Even without asking, every graph knows roughly what normal looks like: a 30-day operating profile
gives each metric a reference range and a normal band for the current hour of the week.

### Finding cycles with periodograms and spectrograms

> Does this error rate have any regular cycles, like a cron job or a GC pause? Show me a periodogram.

Claude computes a **periodogram** of the series without interpolating across gaps, and reports
only peaks that are statistically significant, each with its significance level. A **spectrogram**
shows how those cycles change over time, for example a 15-minute job that started yesterday.
Periods too short for the data's resolution to show are cut off and the panel says so, so the
graph can't present an artifact as a real cycle.

### Distributions, not averaged percentiles

> What fraction of checkout requests took longer than 500 ms in the last hour, compared to the same
> hour yesterday?

A p99 averaged across hosts is not anyone's p99, and a p99 line over time gives a quiet minute the
same visual weight as a busy one. Telemetry Nerd keeps latency as a distribution:

- **Heatmaps** show the shape of latency over time, with low-sample cells faded.
- **ECDFs and histograms** compare the shape of a window now against a reference window.
- **Fraction over threshold** answers the question you actually care about ("how many requests
  were too slow?") by summing counts. When 500 ms falls exactly on a histogram bucket edge,
  the answer is exact. When it falls between edges, you get an honest `[low, high]` range instead
  of an interpolated guess.

Percentile lines are still available, but a quantile is only drawn where there are enough samples
to support it.

### A fleet, not a hundred overlapping lines

> Show me CPU usage across all the nodes in this cluster and tell me which ones are outliers.

Instead of a pile of lines, a **fleet** view draws the members' spread as a shaded band with the
median on top. Only the outliers are drawn as individual lines, labelled with what's wrong:
"+38% since 09:10", "drifting +2%/h", or a spike with its time and size. The panel summarises
the group in a phrase like "44 pods · 3 outliers · 2 silent". Members that stopped reporting
are listed separately, because silence is not health. Outliers are flagged by robust statistical
tests calibrated for the size of the fleet, so with a hundred members you aren't buried in
chance alarms.

### It knows what your metrics are

> Show me node memory on vm-playground.

You didn't say that available memory is a gauge, that it should be drawn against total memory, or
that a counter must be shown as a rate. You don't have to. Telemetry Nerd keeps a **metric catalog**
that records each metric's type (counter, gauge, histogram), unit, natural bounds, whether it can be
summed across series or over time, and its role (utilization, latency, errors, and so on). Those facts
come from the source's own metadata, naming conventions, curated knowledge packs for common exporters,
and the sample data itself. Each fact records where it came from and how confident it is.

That context changes what you see:

- Counters are drawn as rates, never as an ever-growing total.
- A ratio is drawn on 0 to 1 and a percentage on 0 to 100, so a 3% wiggle doesn't look like a crisis.
- Physical limits are drawn: available memory against total memory, available replicas against all
  replicas.
- Analysis that would be wrong is refused, with a hint. Percentiles are never averaged, and raw
  counters are never fed to a periodogram.

You can confirm or correct any fact from the metric card under a graph, and your word always wins.
To have Claude read through a source's metrics with you and fill in the catalog, run:

```
/telemetry-nerd:learn
```

## Using your own metrics

Telemetry Nerd works with any Prometheus-compatible source: Prometheus, VictoriaMetrics, Thanos,
Mimir and Grafana datasource proxies. There are two ways to connect yours:

- Set `TN_SOURCE_URL` to your Prometheus or VictoriaMetrics URL before starting Claude Code:

  ```bash
  export TN_SOURCE_URL=http://127.0.0.1:8428
  ```

- Or just ask Claude to connect it, in plain language or with the start command, which connects,
  learns the metrics and prints the workspace URL in one go:

  ```
  /telemetry-nerd:start http://prometheus.internal:9090
  ```

Telemetry Nerd only **reads** metrics. It never changes your infrastructure. For sources that need
credentials, keep secrets out of the chat: Claude can read them from a file or an environment variable
instead of having you paste them.

## Where to go next

Deeper write-ups on the statistical models behind these views (seasonal baselines, fleet outlier
tests, threshold fractions and the rest) are coming. Until then:

- The [rationale](/rationale/) explains *why* Telemetry Nerd draws telemetry this way, and why each
  of these design choices is deliberate.
- The [architecture](/architecture/) page explains how the plugin, the MCP bridge and the daemon
  fit together, and how to point Telemetry Nerd at a daemon running on another machine.
- The [GitHub repository](https://github.com/Fewbytes/telemetry-nerd) has the install docs, the
  design principles, and the graphing guide the panels are built on.
