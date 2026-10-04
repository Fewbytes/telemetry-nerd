---
title: "Glossary"
description: "The vocabulary Telemetry Nerd uses consistently across panels, skills and docs — what to read when a term shows up in the workspace."
---

Telemetry Nerd uses one precise word for each of these ideas, everywhere: in panel captions, in
the skills Claude follows, in the specs. If you see a different word for the same thing in an
older doc or issue, that's a bug in that text, not a second term. The full reference, including
the exact code identifiers behind each one, is
[`docs/glossary.md`](https://github.com/Fewbytes/telemetry-nerd/blob/master/docs/glossary.md) in
the repository; this page is the subset that matters when you're reading a panel rather than
writing code.

## Time and resolution

- **Series interval** — how often the underlying data actually has a point: the scrape interval
  for a pulled (Prometheus) metric, the push or export interval otherwise. A property of the data
  itself, not something you choose.
- **Query step** — how far apart the points you asked for are. Each one covers a bucket of time
  ending at that point.
- **Query bucket** — one fetched point: the time interval it covers, plus its average, min, max
  and sample count.
- **Display bucket** — after zooming out, several query buckets get merged into one drawn point.
  The merge always keeps the min and max, so a spike doesn't disappear (see "envelope" below).
- **Time range** — the `start..end` a query, dataset or panel covers. Never just "window" — that
  word is reserved for the next two terms.
- **Query window** — the `[5m]` in something like `rate(x[5m])`: how far back each point's
  calculation looks, not the overall time range.
- **Reference time range** — the earlier period a comparison or verdict is judged against (the
  previous day, the same hour last week). Stated explicitly, chosen before the result is seen
  (principle 14).

A step much shorter than the series interval invents detail between real samples; a step very
close to the series interval makes real samples spill across buckets unevenly. Neither is a bug
in the chart — it's what you asked for.

## What you see on a panel

- **Envelope** (min–max band) — the shaded band behind a line, running from each bucket's minimum
  to its maximum. It's drawn so a short spike still shows its true height even after rolling up to
  a wide time range, instead of being averaged away.
- **LOD** (level of detail) — the server-side merge that produces the envelope: about one display
  bucket per pixel, never losing the extremes.
- **Coverage rug** — the strip under a time panel marking which buckets are missing, partial or
  untrusted, each drawn differently. Gaps are shown, never bridged with a straight line
  (principle 11).
- **Settling** — the newest few buckets, which the source may still revise (late-arriving samples,
  backfill). Not an error — just not final yet.
- **Operating profile** — what a signal normally looks like over a long history, broken down by
  hour of the week.
- **Normal band** — the per-series band drawn from the operating profile for the current hour, so
  an unusual value stands out without anyone setting a manual threshold.
- **Ghost** — the same time range from one week earlier, drawn faint and dashed for a quick visual
  comparison.
- **Indexed view** — the Y axis redrawn as a ratio to a baseline (the series' own mean, the
  previous window, last week) on a log scale, so you're looking at relative change rather than
  absolute units.

## Model views

- **Little's law window** — the sub-range Claude averages over when checking L = λW for a service
  (concurrency, throughput, latency); shorter than the full time range by default.

See the [rationale](/rationale/) page for why a model-view verdict always ships with the measured
discrepancy, not just a label, and the [architecture](/architecture/) page for how the pieces that
compute these views fit together.
