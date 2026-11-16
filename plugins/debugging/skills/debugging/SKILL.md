---
name: debugging
description: Use when diagnosing any malfunction in a running system or program — a crash, error, failing command, test failure, latency or error spike, resource growth, an alert, "it's broken", "why is this slow", "find the root cause", or a recurring issue that gets restarted away. A hypothesis-driven procedure with gates against the ways an LLM debugs badly (plausible cause from background knowledge, anchoring, unread tool output, workaround-as-fix, over-claiming). Standalone; inside Telemetry Nerd it is the method and `triage` is the tool procedure.
---

# Debugging

Theory first, then data. Every query, log read, trace or experiment tests a stated theory
against a result predicted before the call. Data gathered without a theory finds unrelated
anomalies and misleads.

## Your failure modes

You produce fluent explanations from background knowledge faster than you test them. Expect,
and guard against:

- **Pattern-matched cause**: the symptom resembles a known failure, so you assert that cause.
  It is a hypothesis until a test that could refute it has passed.
- **Unread refutation**: you run the right check, then report a theory its output contradicts.
  Read every result against the written prediction.
- **Workaround as fix**: the symptom disappears after a restart, fresh file, reinstall, cache
  clear, rollback or retry, and you report success. That discriminates nothing.
- **Anchoring**: later data is bent to fit the first theory.
- **Over-claiming**: "the cause is X" when the evidence shows "removing X removes the symptom".
- **Defend or cave**: when the user objects, neither argue for the old theory nor adopt theirs
  unverified. Re-test.

## Procedure

Keep a log in your working notes as you go: symptom definition, model and its premises,
theories, and for each check the prediction and refuting result written **before** running it.
Count the checks.

### 0. Triage

- Impact: who is hurt, how much, since when. Is it outside normal variation?
- Handle now? If mitigation must come first, capture state before mitigating (profile, thread
  or heap dump, logs, failing inputs, file listings with timestamps) and label the action
  mitigation, not fix.

### 1. Operational symptom

Write the symptom as a check that returns true or false: what, when, where, for whom, under
which conditions, and what should happen instead.
- "`GET /x` returns 500 for 5% of requests, all regions, any headers, since 10:00."
- "`npm run dev` exits with `disk I/O error` at import of module M; a fresh shell does the same."

Reproduce it if possible; vary one condition at a time to find which conditions matter.
**Gate:** evidence counts only if it matches this definition (same endpoint, users, time,
process). Something present near the failure is not evidence until shown in its causal path.

Before theorising, scan what you already have for **what doesn't fit**: the value, timestamp,
size, owner, member or shape that deviates from its peers. Note the shape: a resource that
rises in steps and never falls is accumulating stuck work, not load.

### 2. Explicit model

Write the components, data/request path, queues, pools, timeouts, retries, caches, files and
locks involved, and how each behaves on overload, timeout and crash.

List the **premises** the model rests on, including your own background knowledge and the
user's or team's assertions ("there are no cycles", "X can't handle concurrent access", "the
timeout frees the work"). Mark each **checked** or **assumed**. Check load-bearing assumed
premises against the component's documented design or the data, cheapest first.

When the data doesn't make sense: data is missing, data is wrong, or the model is wrong. Try the
model first.

### 3. Deconstruct and theorise

- Split the system into sub-systems with inputs and outputs. Draw it (text diagram).
- Locate the fault: a sub-system, or a connection between two (timeouts, retries,
  serialization, pool limits, version skew, shared files).
- State each theory as a falsifiable sentence with a concrete subject and mechanism.
- Hold at least two competing theories.

### 4. Test

For each test, before running it, write: what it measures, the result if the theory holds, the
result that refutes it.

- **Discriminate**: prefer tests whose outcomes differ between competing theories. Change one
  thing at a time. Include a control that should succeed.
- **Observe directly**: use the instrument that sees the mechanism (profiler, `lsof`/`strace`,
  a query of the suspect input, a minimal reproduction) over another metric that co-moves.
- **Interventions**: before any change, name the theory it tests. A change that would remove
  the symptom under every theory is mitigation; do not count it as support.
- **Honour refutation**: a refuting result kills the theory. No reinterpretation.

### 5. Reconstruct

- Check invariants: Little's law L = λ·W; conservation (in = out + queued + dropped); counts
  that must match across layers. A mismatch means work or data is hiding somewhere.
- The theory must explain onset, scope, shape, persistence (why it did not self-recover) and
  every control result. **An unexplained residual means the theory is incomplete.**
- Reproduce on demand when feasible; after a fix, show the step-1 check now returns false.

### 6. Iterate

- Narrowed to a sub-system: it is the new system, go to 1.
- Theory refuted, residual unexplained, user objects, or you are surprised: the model is
  suspect, go to 2.

## Statistical traps

- **Base rate**: large systems always contain errors and outliers. If an anomaly is common when
  healthy, finding one during an incident says little. Before citing an anomaly, check its rate
  in a healthy baseline.
- **Co-movement**: during incidents many signals move together (shared load, retries, deploys,
  cron). Co-movement and ordering are findings; causation is a hypothesis.
- **Transience**: a transient trigger can leave persistent bad state. The trigger may be gone
  when you look.

## Choosing what to look at

First ask what you are looking for, then what you are looking at: client- or server-side,
aggregation and window, sampling, counter resets, gaps.

| Looking for | Use |
|---|---|
| Outliers | percentiles, max, most-deviant members, top-K |
| Utilization, totals | averages, sums |
| Errors | error rate vs. throughput, vs. baseline; significance |
| Baseline | same window earlier (day, week), median |
| Trend | moving average |
| High cardinality | top-K, most deviant, unique counts; never scan all series |
| Resources | USE: utilization, saturation (queue, wait, shed), errors |

Latency: percentiles for client impact, averages for Little's law/USE, bounds (timeouts) shape
the distribution, always read throughput alongside. Never average percentiles; merge histograms.

## Reporting

Three separate parts: **established** (observations and intervention results, e.g. "removing
X removes the symptom"), **theory** (mechanism, with what supports and what would refute it),
**unexplained** (residuals, open premises). State how many checks were run. Never present a
mitigation as a root-cause fix or commit it as one.

Stop when: localised with competitors refuted and no residual; within normal variation (cite
the baseline); cannot tell (name the missing signal or access); or the user's question is
answered.

## In Telemetry Nerd

This skill is the method; `triage` runs it with tools.

| Step | Telemetry Nerd |
|---|---|
| 0–1 Triage, symptom | `triage` §1: measurable question, time range with onset inside, reference chosen up front |
| Base rate | `compare_seasonal`, `operating_profile`, `fleet` SPC band, `binding_verdict(reference=…)` |
| 2–3 Model, deconstruct | `entities`, `catalog_relations`, `binding_suggest` (RED per service, USE per resource) |
| Theories | `hypothesis_create` with a competitor, before the test (principles 13, 14) |
| 4 Test | `binding_verdict`, `analyze`, `split_outcome`, `fraction_over`; findings per `evidence` |
| 5 Invariants | `check_littles_law` (`model-views`) |
| Looking at data | `charting` |
| Report | `triage` stop conditions and report |
