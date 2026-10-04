<script lang="ts">
  import { getContext } from "svelte";
  import { fetchCard, type MetricCard } from "../lib/api";
  import { cardSummary, fmtDuration, fmtValue, qualityRows } from "../lib/card";
  import MetricSection from "./MetricSection.svelte";

  let { panelId }: { panelId: string } = $props();
  const catalogSeq = getContext<(() => number) | undefined>("catalogSeq") ?? (() => 0);

  let open = $state(false);
  let card = $state.raw<MetricCard | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(false);

  const load = () => {
    loading = true;
    fetchCard(panelId)
      .then((c) => { card = c; error = null; })
      .catch((e) => (error = String(e)))
      .finally(() => (loading = false));
  };
  // nothing is fetched until the card is opened; an open card follows catalog changes
  $effect(() => {
    void catalogSeq();
    if (open) load();
  });

</script>

<details class="metric-card" data-metric-card bind:open>
  <summary>
    metric card
    {#if card}<span class="summary-line" data-card-summary>{cardSummary(card)}</span>{/if}
  </summary>
  {#if error}<div class="error">{error}</div>{/if}
  {#if loading && !card}<p class="none">loading…</p>{/if}
  {#if card}
    {#if card.produced_by}
      <p class="none" data-card-code>Output {card.produced_by.output} of code node {card.produced_by.node}{#if card.produced_by.parents.length}, from {card.produced_by.parents.join(", ")}{/if}: no catalog metrics, profile or series interval behind it.</p>
    {:else if !card.learned}
      <p class="none">This source has not been learned yet, so the catalog has nothing to show for these metrics. Ask Claude to run <code>source_learn</code>.</p>
    {/if}
    {#each card.metrics as m (m.metric)}
      <MetricSection source={card.source} {m} onchange={load} />
    {/each}
    <section class="card-profile" data-card-profile>
      <h4>Operating profile</h4>
      {#if card.profile.available}
        <p>
          {fmtDuration(card.profile.window_ms ?? 0)} time range · {card.profile.series_total} series
          {#if card.profile.stale}· <em>stale, a refresh is due</em>{/if}
          {#if card.profile.seasonal}· seasonal ({card.profile.seasonal.period}){/if}
          <br />typical range {fmtValue(card.profile.range?.p005)} – {fmtValue(card.profile.range?.p995)}
        </p>
      {:else}
        <p class="none">{card.profile.reason}</p>
      {/if}
    </section>
    <section class="card-quality" data-card-quality>
      <h4>Data quality</h4>
      <table>
        <tbody>
          {#each qualityRows(card.quality) as r (r.label)}
            <tr><th>{r.label}</th><td>{r.value}{#if r.note} <span class="basis">— {r.note}</span>{/if}</td></tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/if}
</details>
