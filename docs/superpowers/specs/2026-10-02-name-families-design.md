# Name-template families (2as.16)

Wikimedia: 327k metric names, 291k of them Airflow statsd-exporter names with a dag, task and state
encoded in the name. A family is a template with a fixed prefix and suffix and one slot; the text
in the slot is the dimension. One catalog entry per family replaces tens of thousands of names.

## Detection (`catalog/families.py`, pure, deterministic)
Names are split on `_`; the first token is a namespace and never a slot. Within a namespace, walk
inward from both ends. A position's **frequent values** are those with >= 20 names. Then:
- one frequent value covering >= 95%: **constant**, extend the template (rare names are left out);
- 2-4 frequent values covering >= 95%: **split** (a small closed set: kinds, sites), one family each;
- 5-16 frequent values covering >= 95% **and** informative about the opposite end (normalised mutual
  information >= 0.1): split (e.g. `ti | dag | task` differ in their suffix vocabularies);
- otherwise **slot**. Both ends slots: finalise.
A family needs >= 20 members, >= 20 distinct dimensions, >= 2 prefix tokens and >= 3 fixed tokens
(a lone `node_*_total` would merge unrelated metrics), and a slot of >= 2 tokens for most members
(single-token slot values are distinct statistics such as `node_netstat_Tcp_InSegs`, not ids).

## Result
Live names: 327,383 -> 140 families covering 302,566 names in ~1.6 s (Airflow: 291k names -> 95
templates, `airflow_ti_finish_*` alone 226,841). Hand-labelled sample (`tests/fixtures/families`: 16,945
names stratified from the live list, 120 labelled): **precision 0.980, recall 0.923, dimension
agreement 1.000**. False positive: `mysql_global_status_wsrep_thread_count` (distinct statistic
names under a shared prefix look like a family). Missed: single-token-per-name encodings
(`cassandra_..._10_192_48_197_7000_75p`), `java_lang_<MemoryPool>_...`, `machinetranslation_mt_it_nso`.
Labels were written by the detector's author from the names alone (not an independent review), and
I had seen the detector's family list for the full corpus. `scripts/eval_families.py` reruns it.

Tried and rejected: relaxing the 95% coverage for the position right under a namespace so that a
family inside a mixed namespace is found: precision fell to 0.89 (gnmi_*, kafka_* ...). Known
limitation: a family that is a minority of its namespace (< 95% at the first position, and not a
small closed set) is not found.

## Catalog
A family is a pseudo-metric named by its template (`catalog_metrics.is_family`); members carry
`family` and `dimension`. Family claims: a description (origin rule, 0.5) and the type/unit at least
95% of members declare (origin metadata). Members get no claims of their own; they inherit the
family's (and the name rules still apply), and a claim written on a member overrides. Learning is
idempotent; a family is never marked removed. Listings (`list_entries`, `catalog_search`, the SQL
browse) show families and hide members; `members=1` / `family=<template>` show them.
**Confirm** pins a family against re-detection (survives a listing that no longer shows it).
**Split** dissolves it for good (the template is recorded as rejected; members get their own T0
claims on the next learn). Nobody overrides a family the user confirmed. MCP `catalog_family`, HTTP
`POST /api/catalog/families`, `GET /api/catalog/{source}/families/{template}/members`, buttons in
the catalog view; user decisions are intentional events.

## Not done
Exposing the dimension as a series label so `fleet(by=[...])` works for name-encoded families (the
query path; follow-up bead); splitting a slot into sub-dimensions (underscores inside identifiers
make it ambiguous); templates in other separators.
