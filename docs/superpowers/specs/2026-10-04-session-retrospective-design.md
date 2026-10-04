# Session retrospective: catalog proposals and scoped lessons (bead 3fs.4)

## Problem

An investigation teaches two kinds of things that today die with the session:

- **catalog knowledge** ("`checkout_latency_seconds` is in seconds", "`queue_depth` is a gauge
  bounded by `queue_capacity`"). Claude can already write these (`catalog_write`), but at the end
  of a session the user never sees what was learned, and nothing asks them to confirm it;
- **methodology lessons** ("for checkout, split latency by region before reading p95: one region
  dominates"). There is no place for them at all.

The epic's target flow ends with "Claude proposes catalog improvements and scoped lessons for
future sessions". 3fs.4 acceptance: *a finished investigation yields reviewable proposals;
approved lessons appear in a later session with matching scope and not otherwise.*

Principles in play (`docs/principles.md`): 1 evidence first, 2 scoped claims, 13 hypotheses not
verdicts (humans conclude), 15 learned facts carry their origin, 16 results are models not facts.
A lesson is the most dangerous kind of claim the system holds: it is meant to be *re-applied* in
a later session, where nobody remembers the evidence. So it is held to the strictest scoping.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | Trigger: the command **`/telemetry-nerd:wrap`**. No SessionEnd hook. | Claude Code's `SessionEnd` hook runs as the session closes: it cannot make Claude act and its output never reaches the model, so it cannot propose anything. A command at a natural stopping point is explicit and reviewable. The SessionStart hook reminds of proposals still awaiting review (R9). |
| R2 | Claude **proposes**, the user **decides**. Two new MCP tools write proposals only: `catalog_propose` and `lesson_propose`. Nothing reaches the catalog or a later session until the user approves in the UI. | Principle 13: Claude assists, humans conclude. A proposal costs nothing to reject. |
| R3 | Proposals and lessons are **global** (not workspace-scoped), in two new tables of the one SQLite file; each row records the workspace it came from. | Lessons must outlive the workspace (that is their point); catalog proposals target the global catalog (workspace spec D9). Ids are global (D3), so evidence refs `f3`/`p7` stay unambiguous from any workspace. |
| R4 | A lesson **requires evidence** (findings `f…` and/or panels `p…`, at least one) and a **scope** `{source, service?, metric_family?, labels?}`; `source` is mandatory. A lesson without evidence or without a source is refused. | Principles 1 and 2. |
| R5 | **Scope guard: a lesson is no broader than its evidence.** At least one evidence item must cover the lesson's whole scope (below); otherwise refused with the scope the evidence supports. Rejected findings are not evidence. Evidence whose selector cannot be read is *undetermined* and covers nothing. | Two services observed is not "the source"; one window's quirk is not "always". Reuses the selector reading of `core/claim_scope.py` (`read_expr`), the same code that checks finding scopes. |
| R6 | Lessons **expire** (default 180 days; `expires` = a duration such as `90d` or an ISO date, at most 2 years) and are **refutable**: `lesson_refute(lesson, evidence=[f…], reason)` by Claude or a refute from the UI marks it `refuted`, citing a finding whose scope overlaps the lesson's. | Systems change; a lesson is a model of how they behaved (principle 16). Expiry and refutation keep it from becoming folklore. |
| R7 | Approving a catalog proposal writes a claim with **origin `claude`, `verified_by: "user"`** and the citation `<basis> (proposal cpN; evidence …)`. Editing the value before approving writes an **origin `user`** claim (the user's own value, confidence 1). | Principle 15: the origin says who stated the value; `verified_by` records that the user checked it. An edited value is the user's statement. |
| R8 | **Surfacing is scoped, never global.** `lessons_for(source, services?, metric_families?)` returns approved, unexpired, unrefuted lessons whose scope matches; a lesson naming a service is returned only when that service is named in the call. `/telemetry-nerd:start` and `/telemetry-nerd:investigate` call it once the source (and services) are known. | Acceptance: appear with matching scope and not otherwise. |
| R9 | The **SessionStart hook** (`ensure`) adds at most two short lines: how many lessons are on file per connected source (counts only, never the text; the command then asks `lessons_for`), and how many proposals await review in the UI. | The hook cannot know the investigation's services; printing lesson text there would be unscoped. |

## Data model

```sql
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,            -- ls1, ls2 ... (counter prefix 'ls')
    workspace TEXT NOT NULL,        -- where it was proposed
    data TEXT NOT NULL              -- Lesson JSON
);
CREATE TABLE IF NOT EXISTS catalog_proposals (
    id TEXT PRIMARY KEY,            -- cp1, cp2 ... (counter prefix 'cp')
    workspace TEXT NOT NULL,
    data TEXT NOT NULL              -- CatalogProposal JSON
);
```

`Lesson`: `text`, `scope {source, service?, metric_family?, labels{}}`, `evidence [ids]`,
`status` (`proposed | approved | rejected | refuted`; `expired` is derived on read from
`expires_at_ms`), `author`, `created_at_ms`, `expires_at_ms`, `decided_at_ms?`, `comment?`,
`refuted_by [finding ids]`, `refute_reason?`, `scope_check` (what the guard found: the evidence
item(s) that cover the scope, and those that only cover part of it).

`CatalogProposal`: `source, metric, field, value, confidence (≤ 0.9), basis, evidence [ids]`,
`status` (`proposed | approved | rejected`), `decided_value?` (when edited), `comment?`.

Events (in the workspace active at the time): `lesson.proposed`, `lesson.decided`,
`lesson.refuted`, `proposal.created`, `proposal.decided`. User decisions are intentional (the
channel tells Claude "user approved ls3"); the UI refetches proposals on any of them.

## Scope guard (R5)

An evidence item contributes `(source, alternatives)`: a finding its `scope.source` and
`read_expr(scope.selector)`; a panel its datasets' source and `read_expr(expr)` per dataset
(code outputs and filters: undetermined). Each alternative is a list of label matchers its
series satisfy.

The lesson scope **pins**: every service label (`KIND_LABELS["service"]`: `service_name`,
`service`, `app`, `job`, ...) to `service` when given; each `labels` key to its value. An
alternative **covers** the lesson when

1. the evidence's source is the lesson's source;
2. every restricting matcher on an entity label (`ENTITY_LABELS` ∪ service labels) is satisfied
   by the value the lesson pins for that label, and the lesson pins one (an evidence restricted
   to `service_name="checkout"` does not cover a source-wide lesson). Matchers that keep every
   non-empty value (`!=""`, `=~".+"`, `=~".*"`) do not restrict;
3. with `metric_family` given, the alternative names a metric (`__name__ =`) that is in the
   family (prefix, or `*` glob).

Non-entity labels (`code`, `le`, `method`) select a signal, not a population: they never restrict.
An item covers when any of its alternatives does; the lesson is accepted when any item covers.
Items that do not cover are kept and listed under `scope_check.partial` (they relate to the
lesson but do not carry it).

Without `metric_family`, a lesson applies across the metrics of its scope: methodology ("split
by region") is about a population, not one metric. A lesson about one metric names its family.

## Matching (R8)

`lessons_for(source, services=[], metric_families=[])` → lessons with status approved, not
expired, `scope.source == source`, and: `scope.service` absent or in `services`;
`scope.metric_family` absent or in `metric_families`; `scope.labels` empty (label-scoped
lessons are returned only through `labels`, a dict argument, matched exactly). Each row carries
its text, scope, evidence refs (with the workspace they live in) and expiry. `held` counts the
approved lessons on that source the call did not match, so Claude can ask with the services.

## UI

A **Proposals** view (header nav, `#/proposals`, with a count badge of items awaiting review):
catalog proposals and lessons in two lists. Each card shows the statement, scope, evidence chips
(links to `#/panel/p…`, `#/finding/f…` when in the active workspace, else "in wN"), and the guard
result. Actions: **Approve**, **Edit** (lesson text and expiry; a catalog value) then save as
approved, **Reject** with an optional comment; approved lessons get **Refute** (reason required).
Buttons are real `<button>`s in a labelled `role="group"`; the edit form is a `<form>` (Enter
saves, Escape cancels and returns focus to Edit); decided items move to a collapsed "decided"
list. Theme tokens only, so both themes work.

## Commands and skills

- `commands/wrap.md`: review `workspace_get` (findings with verdicts, hypotheses, gaps) and
  `proposals_list`; propose catalog updates the investigation established (with evidence); propose
  at most a few lessons, each scoped to what its evidence covers; never re-propose what is on
  file; point the user to the Proposals view.
- `commands/start.md`: Grafana URLs go through the front door: `source_discover_grafana(url)`,
  pick a supported Prometheus-type datasource (ask when several), `source_connect(name,
  grafana=, uid=)`; then `lessons_for(source)`.
- `commands/investigate.md`: after scoping, `lessons_for(source, services)`; state the lessons
  that apply and still check them (a lesson is a prior, not evidence).
- Skills `evidence` and `triage`: one short section each (wrap at the end; lessons are priors with
  scope; refute when a finding contradicts one).

## Out of scope (follow-ups)

Relations and bindings as proposals (`catalog_relate`/`catalog_bind` stay direct); editing a
lesson's scope in the UI (narrowing only); lessons shared across data dirs; automatic refutation
suggestions when a new finding contradicts an approved lesson.
