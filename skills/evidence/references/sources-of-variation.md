# Sources of variation per op

Principle 8 (`docs/principles.md`); spec §5.4. Each op labels what it reports; the label lives in `source` on items and on `evidence`
statistics and in the result's `variation` list (`{source, finding}`). Measurement-system items
come from the result's caveats.

| Op | `common_cause` | `special_cause` | `measurement_system` | `undetermined` |
|---|---|---|---|---|
| `analyze` (SPC) | control limits, centre and sigma from the baseline; stable or periodic structure; wandering or heavy-tailed noise; SPC signals on an in-control chart (no more than its false alarms) | signals of a significant deciding detector (Poisson-tested count); points after a material level shift on an out-of-control chart; level shifts; drift; variance change | gaps, partial / missing / untrusted data, post-gap spikes, coarsening, unknown input uncertainty | run rules on an out-of-control chart without a significant detector of their own |
| `compare_seasonal` | the cycle-to-cycle band; a usual window | an unusual level, extremes, too many points outside the band; atypical reference cycles | cycles excluded as missing; data caveats | cycles the user excluded; histograms: a distribution shape further from the reference than every previous cycle (descriptive, chance 1/(k+1)) |
| `fleet` | the SPC band (median ± 2σ/3σ, the tests' reference) and the per-step spread; points beyond 3σ no test flagged; behaviour groups (systemic strata) | outlying members (persistent, shifted, drifting, transient) | unknown spans, missing / skipped members, members the source marked stale (`state: ended`) | a member with no samples since T (gone or sick; never "left", principle 9); an outlier episode on partial buckets |
| `binding_verdict` | a role with no change against its reference | a changed role and its onset | data caveats per role | a change on data with measurement-system caveats |
| `check_littles_law` | the small-system envelope; windows inside it | transient windows beyond interval and envelope (say "at a load peak" when `phase` is peak) | the measurement interval; a systematic offset; the whole-range discrepancy when consistent or systematic | the whole-range discrepancy when only transient windows carry it (the transients carry their own labels) |

## Reporting each

- **Common cause.** Give the envelope (limits, band, spread) and where now sits in it. The only
  lever is changing the system (capacity, configuration, code). Do not open hypotheses about
  individual points inside the envelope.
- **Special cause.** Give the size, interval, onset interval and scope; open or test hypotheses
  about what was assigned to it; check measurement first if any caveat is present.
- **Measurement system.** Record it as its own finding; qualify every claim that depends on the
  instrument; propose the fix (a gap if a signal is missing).
- **Undetermined.** State both readings and the observation that would separate them (a
  re-fetch after the partial window, a second instrument, a longer baseline, the silent
  member's logs).

## Why the labels matter in triage

Chasing common-cause variation produces false root causes and tampering; ignoring a
measurement-system issue produces confident findings about the instrument rather than the
service. The label decides the next action, so it is never guessed: when an op gives none,
say the source is not stated.
