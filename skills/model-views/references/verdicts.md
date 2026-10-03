# Reading and reporting `binding_verdict`

## Call

`binding_verdict(source, group="pgN" | kind + key | suggestion, range | start/end, step,
reference="auto", tz="UTC", matchers, error_matcher, alpha=0.05)`.

With `group`, the group's window, step and matchers are used and its roles get verdict badges and
a first-mover line in the UI. The window should be long enough to hold several steps per
detector block; the verdict is computed on the shown range, so choose the range to contain the
episode and some quiet time before it.

## References (what "changed" means)

| `reference` | reference windows | when |
|---|---|---|
| `previous` | the 4 windows just before | no daily rhythm, or a short incident window |
| `day` | the same window on 7 previous days | diurnal traffic |
| `week` | the same window on 4 previous weeks | weekly rhythm |
| `profile` | `day` or `week`, chosen by the cached operating profile | `operating_profile` on each role's expr (from the suggestion `detail` or the group) first; refused without a seasonal profile |
| `auto` | `profile` if cached, else `previous` | default; state which one was used |

`tz` sets the calendar for `day` / `week` shifts (local days, 23/25 h across DST). A profile
without seasonality makes `auto` fall back to `previous`. Fewer than 3 usable cycles gives `insufficient`. State `reference.label` and `chosen`. If `previous`
was used on a service with a daily cycle, say the comparison may flag the daily rhythm or hide a
change that matches it.

## Per-role fields

- `status`: `changed` | `no_change` | `insufficient` | `gap` | `error`. Only `changed` is a
  finding; `no_change` means no detected change at this power; the others are about the data.
- `direction`: higher / lower than the reference.
- `pattern`: `level` (window differs, no onset inside it), `shift` (one change point), `burst`
  (an episode that ended), `blip` (a short episode that ended), `sustained` (still open at the
  end).
- `onset`: `at` plus `interval` and `basis` (an episode's CUSUM, or a change point with an interval
  from the data). The interval is the claim; `at` alone is not. `level` pattern has no onset.
- `level`: now vs reference with intervals. `threshold` (latency): the bucket edge, the
  reference share and the share now. `members` (utilization, saturation): judged per member; the
  role reports the worst. `near_bound` / `at_capacity` (utilization): runs near the natural
  bound. `model_check` (concurrency in a Little's law binding): the `check_littles_law` result — its
  `summary`, `discrepancy` (report it first), verdict, `classification` and `warnings`;
  `status: not_possible` when the binding has no concurrency signal.
- `evidence`: statistics for `finding_create`, citing the role dataset.
- `caveats`: `overdispersed`, `heavy_tails`, `noisier_than_reference`, input uncertainty flags.

## Ordering

`summary.moved` lists changed roles by onset; `summary.first` names one only when its onset
interval does not overlap the next one. Otherwise `summary.text` says simultaneous within a
stated margin. A role with pattern `level` has no onset and cannot be ordered: "first" means
first among roles with onsets, so a changed `level` role may have moved earlier. The family alpha
controls false flags, not ordering or onset claims, and onset intervals are approximate (Bai's
interval for a shift covering most of the window runs below its nominal 95%).

Wording: "Duration moved first (onset 10:00Z, interval 09:56-10:02Z); errors followed (10:20Z,
10:17-10:22Z): the intervals do not overlap." Or: "Utilization and saturation moved together
within +-2 min; no ordering is supported." Order of detection is not cause.

## What the roles measure

- **errors**: the error share (errors / requests) on effective sample size (overdispersion and
  autocorrelation reduce n), Wilson / binomial intervals. A burst of 10x on 0.2% is reported as a
  share, not as "5 more errors".
- **duration**: the share of requests above the bucket edge where the reference share above is
  nearest 5% (the reference's ~p95 edge; 1.2% in the worked example because buckets are coarse),
  from the histogram.
  A shift of the whole distribution shows as that share rising; a change entirely below the edge
  does not. When latency matters elsewhere, follow with `query_distribution` / `fraction_over` /
  `show(mark="histogram", windows=[...])` for the full distribution.
- **rate, concurrency, utilization, saturation**: the per-step value against the reference's
  spread. Rate drops matter as much as rises.
- **USE errors**: a rate of events, quasi-Poisson.

## Template for the answer

"Against <reference label> (<k> windows), at <alpha> family-wise over <m> signals: <role> <direction>
(<pattern>, onset <approximate interval> | no onset inside the window), <statistic with
interval and n, e.g. 1.2% [CI] of n=... to 5.4% [CI] of n=...>.
<ordering sentence>. <caveats>. Not tested: <gap / insufficient roles>." Then `finding_create`
with the evidence objects exactly as returned, and say when a flag such as `input_uncertainty`
makes the interval a lower bound.

## Do not

- Say "healthy" for `no_change`; say "no change detected against <reference>".
- Claim an order from overlapping intervals, or a cause from an order.
- Quote latency as a percentile or a mean here; quote the share above the stated edge.
- Treat `at_capacity` or `model_check` as test results.
- Re-run with different `alpha`, references or ranges until something flags: the family budget is
  per call, and the range is chosen before seeing the verdict.
