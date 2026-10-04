"""The e2e fixture source's data (bead y7hb): what the shared dev VictoriaMetrics used to hold.

* the synthetic demo series (`devtools.synthetic.demo_text`: latency gauge with a spike, request
  counters, a latency histogram, series with holes) over `hours` before the anchor, with the
  sine phase and holes anchored there too, so values depend only on the time since the anchor;
* `up{job="node-exporter"}` = 1 every 15s from the start of history to a day past the anchor,
  standing in for the dev stack's scrape target (the daemon learns the 15s resolution from it,
  and the proposals spec reads it as a continuously scraped service). Its future samples are
  invisible until the server's clock reaches them (engine.Engine.visible, server listings), so it
  behaves as a target that keeps being scraped, and no read or listing ever sees future data.

Anchor/now drift: the demo series end at the anchor (the server's start, rounded down to a
whole minute), while the specs ask for windows relative to the daemon's "now", which moves on
as the suite runs: a now-relative window slides over the data by whole steps as time passes, so
specs whose assertions are statistical import their own series on a fixed step grid and query
exactly that range (spc, fleet, seasonal; bead ax1s). Specs read up to
`now-10m` (`now-5m` for their own imported series), so the suite must finish within ~9 minutes
of the fixture's start (10, less up to a minute of anchor rounding) for those windows to stay
inside the seeded data (it takes ~1.5 min).

Specs that need more add their own uniquely named series through the import endpoint.
"""

from __future__ import annotations

from telemetry_nerd.devtools.promfixture.store import Store
from telemetry_nerd.devtools.synthetic import demo_text

SCRAPE_MS = 15_000
HOUR_MS = 3_600_000
#: the anchor is a whole minute (bead ax1s): the daemon aligns query ranges to whole steps of
#: absolute time, so with the anchor on that grid a 1m (or 15s/30s) bucket covers the same
#: samples, relative to the anchor, in every run. Off the minute, which 15s samples share a
#: bucket depended on the second the fixture started, and so did every statistic on them.
ANCHOR_MS = 60_000


def seed(store: Store, now: int, hours: int = 6) -> int:
    """Fill `store`; returns the anchor (now rounded down to a whole minute)."""
    anchor = now // ANCHOR_MS * ANCHOR_MS
    start = anchor - hours * HOUR_MS
    store.import_text(demo_text(start, anchor, origin_ms=anchor), anchor)
    store.add(
        {"__name__": "up", "job": "node-exporter", "instance": "node-exporter:9100"},
        ((t, 1.0) for t in range(start, anchor + 24 * HOUR_MS, SCRAPE_MS)),
    )
    return anchor
