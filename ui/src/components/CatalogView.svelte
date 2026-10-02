<script lang="ts">
  import { getContext, untrack } from "svelte";
  import {
    decideFamily, fetchCatalog, fetchCatalogMetric, fetchSources,
    type CardMetric, type CatalogPage, type CatalogRow, type SourceInfo,
  } from "../lib/api";
  import { originLabel, fmtValue } from "../lib/card";
  import { PAGE, activeFilters, catalogParams, defaults, pageInfo, rowBadges, type CatalogFilters } from "../lib/catalogQuery";
  import MetricSection from "./MetricSection.svelte";

  const catalogSeq = getContext<(() => number) | undefined>("catalogSeq") ?? (() => 0);

  let sources = $state<SourceInfo[]>([]);
  let f = $state<CatalogFilters>(defaults());
  let data = $state.raw<CatalogPage | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(false);
  let openMetric = $state<string | null>(null);
  let detail = $state.raw<CardMetric | null>(null);
  let qDraft = $state("");

  $effect(() => {
    fetchSources()
      .then((r) => {
        sources = r.sources;
        if (!f.source && r.sources.length) f.source = (r.sources.find((s) => s.name === "default") ?? r.sources[0]).name;
      })
      .catch((e) => (error = String(e)));
  });

  // typing waits a beat before it queries; every other control queries at once
  $effect(() => {
    const v = qDraft;
    const t = setTimeout(() => untrack(() => { if (f.q !== v) { f.q = v; f.page = 0; } }), 250);
    return () => clearTimeout(t);
  });

  let seq = 0;
  const load = () => {
    if (!f.source) return;
    const mine = ++seq;
    loading = true;
    fetchCatalog(catalogParams(f))
      .then((p) => { if (mine === seq) { data = p; error = null; } })
      .catch((e) => { if (mine === seq) error = String(e); })
      .finally(() => { if (mine === seq) loading = false; });
  };
  $effect(() => {
    void JSON.stringify(f);
    void catalogSeq(); // an edit by anyone (here, the card, Claude) refreshes the list
    untrack(load);
  });

  const loadDetail = () => {
    if (!openMetric) return;
    const m = openMetric;
    fetchCatalogMetric(f.source, m)
      .then((d) => { if (openMetric === m) detail = d; })
      .catch((e) => (error = String(e)));
  };
  $effect(() => {
    void catalogSeq();
    if (openMetric) untrack(loadDetail);
    else detail = null;
  });
  const toggle = (r: CatalogRow) => { openMetric = openMetric === r.metric ? null : r.metric; detail = null; };
  const set = <K extends keyof CatalogFilters>(k: K, v: CatalogFilters[K]) => { f[k] = v; if (k !== "page") f.page = 0; };
  const decide = (r: CatalogRow, action: "confirm" | "split") =>
    decideFamily(f.source, r.metric, action)
      .then(() => { if (action === "split") openMetric = null; load(); })
      .catch((e) => (error = String(e)));
  const showMembers = (r: CatalogRow) => { f.family = r.metric; f.page = 0; openMetric = null; };
  const clear = () => { const s = f.source; f = defaults(s); qDraft = ""; };
  const pg = $derived(pageInfo(data?.total ?? 0, f.page));
  const cells = ["type", "unit", "role", "bounds"] as const;
</script>

<section class="catalog-view" data-catalog-view aria-label="Catalog">
  <header class="catalog-head">
    <h2>Catalog</h2>
    <label>source
      <select aria-label="Source" value={f.source} onchange={(e) => { f.source = e.currentTarget.value; f.page = 0; openMetric = null; }}>
        {#each sources as s (s.name)}<option value={s.name}>{s.name}</option>{/each}
      </select>
    </label>
    {#if data}
      <span class="catalog-summary" data-catalog-summary>
        {data.summary.metrics} metrics ·
        {data.summary.reviewed} reviewed ·
        <button type="button" class="linklike" onclick={() => set("conflicts", !f.conflicts)}>{data.summary.conflicts} with conflicts</button> ·
        <button type="button" class="linklike" onclick={() => set("findings", !f.findings)}>{data.summary.findings} with findings</button>
        {#if data.summary.families}
          · <span data-catalog-families title="Names that encode a dimension are grouped; members are hidden behind their family">{data.summary.families} families covering {data.summary.family_members.toLocaleString("en-US")} metrics</span>
        {/if}
      </span>
    {/if}
  </header>

  <div class="catalog-filters" role="group" aria-label="Catalog filters">
    <input type="search" placeholder="search name or description" aria-label="Search" bind:value={qDraft} />
    <input type="text" placeholder="name prefix, e.g. node_cpu" aria-label="Prefix" value={f.prefix} onchange={(e) => set("prefix", e.currentTarget.value)} />
    <select aria-label="Winning origin" value={f.origin} onchange={(e) => set("origin", e.currentTarget.value as CatalogFilters["origin"])}>
      <option value="">any origin</option>
      {#each ["user", "claude", "stats", "pack", "metadata", "rule"] as o (o)}<option value={o}>{originLabel(o)}</option>{/each}
    </select>
    <select aria-label="Reviewed" value={f.reviewed} onchange={(e) => set("reviewed", e.currentTarget.value as CatalogFilters["reviewed"])}>
      <option value="">reviewed or not</option><option value="no">needs review</option><option value="yes">reviewed</option>
    </select>
    <label><input type="checkbox" checked={f.weak} onchange={(e) => set("weak", e.currentTarget.checked)} /> weak (&lt; 0.6)</label>
    <label><input type="checkbox" checked={f.conflicts} onchange={(e) => set("conflicts", e.currentTarget.checked)} /> conflicts</label>
    <label><input type="checkbox" checked={f.findings} onchange={(e) => set("findings", e.currentTarget.checked)} /> findings</label>
    <label><input type="checkbox" checked={f.removed} onchange={(e) => set("removed", e.currentTarget.checked)} /> removed</label>
    <label title="Show the metrics inside each name family too"><input type="checkbox" checked={f.members} onchange={(e) => set("members", e.currentTarget.checked)} /> family members</label>
    <select aria-label="Sort" value={f.sort} onchange={(e) => set("sort", e.currentTarget.value as CatalogFilters["sort"])}>
      <option value="name">by name</option><option value="weakest">weakest first</option><option value="conflicts">most conflicts first</option>
    </select>
    {#if activeFilters(f)}<button type="button" onclick={clear}>clear {activeFilters(f)} filter{activeFilters(f) > 1 ? "s" : ""}</button>{/if}
  </div>

  {#if error}<div class="error">{error}</div>{/if}
  {#if f.family}
    <p class="family-crumb" data-family-crumb>
      Members of <code>{f.family}</code> —
      <button type="button" class="linklike" onclick={() => set("family", "")}>back to all metrics</button>
    </p>
  {/if}
  {#if data && data.summary.metrics === 0 && !activeFilters(f)}
    <p class="none">This source has no catalog entries yet. Ask Claude to run <code>source_learn</code> on it.</p>
  {:else if data}
    <table class="catalog-table" data-catalog-total={data.total}>
      <thead><tr><th>metric</th>{#each cells as c (c)}<th>{c}</th>{/each}<th></th></tr></thead>
      <tbody>
        {#each data.rows as r (r.metric)}
          <tr class="catalog-row" class:open={openMetric === r.metric} class:conflict={r.conflicts.length > 0} data-catalog-row={r.metric}>
            <td>
              <button type="button" class="linklike metric" aria-expanded={openMetric === r.metric} onclick={() => toggle(r)}><code>{r.metric}</code></button>
              {#if r.dimension}<div class="dimension" data-dimension title="the part of the name that varies">dimension: <code>{r.dimension}</code></div>{/if}
              {#if r.is_family}
                <div class="family-actions">
                  <button type="button" data-family-members={r.metric} onclick={() => showMembers(r)}>show members</button>
                  {#if r.family_info?.status !== "confirmed"}
                    <button type="button" data-family-confirm={r.metric} title="These really are one metric with a dimension in its name" onclick={() => decide(r, "confirm")}>confirm</button>
                  {/if}
                  <button type="button" data-family-split={r.metric} title="These are unrelated metrics: dissolve the family for good" onclick={() => decide(r, "split")}>split</button>
                </div>
              {/if}
            </td>
            {#each cells as c (c)}
              <td data-cell={c}>
                {#if r[c] !== null}
                  {fmtValue(r[c])}
                  <span class="chip origin {r.origins[c]}" title="{originLabel(r.origins[c])}, confidence {r.confidences[c]?.toFixed(2)}">{originLabel(r.origins[c])}</span>
                {:else}<span class="none">—</span>{/if}
              </td>
            {/each}
            <td>
              {#each rowBadges(r) as b (b.key)}<span class="chip {b.tone}">{b.text}</span>{/each}
              {#each r.findings as fd (fd.id)}<a class="obj-id" href="#finding-{fd.id}">{fd.id}</a>{/each}
            </td>
          </tr>
          {#if openMetric === r.metric}
            <tr class="catalog-detail"><td colspan={cells.length + 2}>
              {#if detail}<MetricSection source={f.source} m={detail} onchange={() => { load(); loadDetail(); }} />{:else}<p class="none">loading…</p>{/if}
            </td></tr>
          {/if}
        {/each}
        {#if data.rows.length === 0}<tr><td colspan={cells.length + 2} class="none">no metric matches these filters</td></tr>{/if}
      </tbody>
    </table>
    <nav class="catalog-pager" aria-label="Pages">
      <button type="button" disabled={!pg.prev} onclick={() => set("page", f.page - 1)}>← prev</button>
      <span data-catalog-range>{pg.from}–{pg.to} of {data.total}{loading ? " …" : ""}</span>
      <button type="button" disabled={!pg.next} onclick={() => set("page", f.page + 1)}>next →</button>
      <span class="none">page {f.page + 1} of {pg.pages} ({PAGE} per page)</span>
    </nav>
  {:else if loading}<p class="none">loading…</p>{/if}
</section>
