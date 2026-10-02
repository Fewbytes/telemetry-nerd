# Binding workflow: details

## What a suggestion contains

`binding_suggest` returns ranked `suggestions`. Per suggestion:

- `id` (`RED:otel_http`, `USE:node_cpu`, `littles_law:otel_http`): what `binding_accept` and
  `show_binding(suggestion=...)` take. `kind`, `key` (`http.server`), `scope`, `confidence`.
- `roles`: `{role: metric | null}`; `null` roles are listed in `unfilled`, each with the
  instrumentation that would fill it.
- `detail[role]`: `confidence`, `basis` (a list of `pack`, `naming`, `relation`), `form`
  (`counter_rate`, `histogram_count`, `label_split`, `histogram`, ...), an `expr` hint,
  `alternatives` (each with its own confidence and expr) and `ambiguous`.
- `join_on` with `join_on_basis`: label names that tie the roles to one entity. These are
  conventions (`service_name`, `instance`, `device`); the catalog holds no label data.

## Review checklist before `binding_accept`

1. **Ambiguous roles** (`ambiguous_roles`). Typical: `rate` and `errors` both pointing at one
   request counter (errors are a label split of it, form `label_split`), or `rate` choosing between
   the counter and the histogram `_count`. Both are fine when the split label exists; confirm the
   label with `query` on a few series before accepting. When the alternative is the better fit,
   pass `overrides={"rate": "<alternative metric>"}`.
2. **Errors without a status label.** The expr hint uses the semconv label
   (`http_response_status_code=~"5.."`). Where the label differs, accept anyway and pass
   `error_matcher='status_code=~"5.."'` to `show_binding` / `binding_verdict`; or, for
   a dedicated error counter that is not among the `alternatives`, bind by hand with
   `catalog_bind` (`overrides` only swaps in a listed alternative).
3. **Latency.** Accept only a histogram (or summary with `_sum` / `_count`) base name. A role
   filled by a precomputed percentile gauge is a poor binding: the verdicts need the distribution.
   Without a histogram, leave the role `null` and let it be a gap.
4. **Join labels.** `join_on` must exist on every role's series with the same values; otherwise
   members will not match across roles and `show_binding` roles show `error` or empty members.
   Override with `join_on=[...]` when the real label is `pod` or `job`.
5. **Scope.** One binding per entity key. For a service with many instances, bind the service
   key and use `matchers` or `by`; do not bind each instance separately.
6. **Existing claims.** `catalog_relations(source, kind="RED")` shows bindings already stored,
   including contested ones. A user-authored binding outranks a suggestion; do not override it.

`basis` on accept is mandatory text saying what was checked, e.g. "series have service_name; 5xx
split verified with query; latency is a classic histogram". Confidence defaults to the
suggestion's and is capped at 0.9: only a user confirmation is higher.

## Look before confirming

`show_binding(suggestion="RED:otel_http", range="1h")` draws an unconfirmed suggestion, so
wrong picks are seen before they are stored. It returns `roles[...]` with `notes` and `error`
entries; an empty or errored role means a wrong metric or label: fix the binding, not the chart.

## Common errors

- "no confirmed RED binding" for a suggestion id: the key is the binding key (`http.server`), not the
  suggestion id. The message lists the bound keys.
- "panel_group ... not found": pass the id returned by `show_binding` for this workspace.
- A role `error` in `show_binding`: read its message (a missing label, no data in range) before
  concluding anything about the service.
- Many members: roles with more than 5 members render as fleets; judge them with `fleet`.

## From gap to instrumentation

An unfilled role is not a failure to hide. State what is missing and what the user could add:

| role | typical missing signal | what to add |
|---|---|---|
| duration / latency | only a percentile gauge | a histogram with buckets around the SLO edge |
| errors | no status label, no error counter | a status-labelled request counter |
| concurrency | no in-flight gauge | an in-flight / active requests gauge |
| saturation | nothing queue-like | queue depth, run-queue / pressure stall, pool wait time |
| utilization | busy time not exported | a busy-seconds counter or utilization gauge with a known bound |

Record with `gap_create` when the answer to the user's question depends on it.
