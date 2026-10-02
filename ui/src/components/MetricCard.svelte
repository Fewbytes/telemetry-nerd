<script lang="ts">
  import { getContext } from "svelte";
  import { fetchCard, postClaim, type CardField, type CardMetric, type MetricCard } from "../lib/api";
  import { cardSummary, editControl, fmtDuration, fmtValue, isPinned, originLabel, qualityRows } from "../lib/card";
  import RefText from "./RefText.svelte";

  let { panelId }: { panelId: string } = $props();
  const catalogSeq = getContext<(() => number) | undefined>("catalogSeq") ?? (() => 0);

  let open = $state(false);
  let card = $state.raw<MetricCard | null>(null);
  let error = $state<string | null>(null);
  let loading = $state(false);
  let editing = $state<{ metric: string; field: string; draft: string } | null>(null);
  let rowError = $state<{ metric: string; field: string; text: string } | null>(null);

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

  const write = (m: CardMetric, f: CardField, value: unknown) => {
    rowError = null;
    postClaim(card!.source, m.metric, f.field, value)
      .then(() => { editing = null; load(); })
      .catch((e) => (rowError = { metric: m.metric, field: f.field, text: String(e) }));
  };
  const startEdit = (m: CardMetric, f: CardField) =>
    (editing = { metric: m.metric, field: f.field, draft: f.value === null || f.value === undefined ? "" : String(f.value) });
  const isEditing = (m: CardMetric, f: CardField) => editing?.metric === m.metric && editing.field === f.field;
</script>

<details class="metric-card" data-metric-card bind:open>
  <summary>
    metric card
    {#if card}<span class="summary-line" data-card-summary>{cardSummary(card)}</span>{/if}
  </summary>
  {#if error}<div class="error">{error}</div>{/if}
  {#if loading && !card}<p class="none">loading…</p>{/if}
  {#if card}
    {#if !card.learned}
      <p class="none">This source has not been learned yet, so the catalog has nothing to show for these metrics. Ask Claude to run <code>source_learn</code>.</p>
    {/if}
    {#each card.metrics as m (m.metric)}
      <section class="card-metric" data-card-metric={m.metric}>
        <h4><code>{m.metric}</code>{#if !m.present} <span class="chip">no longer in the source</span>{/if}</h4>
        <table class="card-fields">
          <tbody>
            {#each m.fields as f (f.field)}
              {@const ctl = editControl(f)}
              <tr data-field={f.field} class:conflict={f.conflict}>
                <th>{f.field.replace("_", " ")}</th>
                <td>
                  {#if isEditing(m, f)}
                    {#if ctl?.kind === "select"}
                      <select bind:value={editing!.draft} aria-label="Edit {f.field}">
                        <option value="">—</option>
                        {#each ctl.options as o (o)}<option value={o}>{o}</option>{/each}
                      </select>
                    {:else}
                      <input type="text" bind:value={editing!.draft} aria-label="Edit {f.field}"
                        onkeydown={(e) => { if (e.key === "Enter" && editing!.draft.trim()) write(m, f, editing!.draft.trim()); if (e.key === "Escape") editing = null; }} />
                    {/if}
                    <button type="button" disabled={!editing!.draft.trim()} onclick={() => write(m, f, editing!.draft.trim())}>Save</button>
                    <button type="button" onclick={() => (editing = null)}>Cancel</button>
                  {:else}
                    <span class="val" data-value>{f.field === "description" ? "" : fmtValue(f.value)}</span>
                    {#if f.field === "description" && f.value}<span class="val"><RefText text={String(f.value)} /></span>{/if}
                    {#if f.origin}
                      <span class="chip origin {f.origin}" data-origin title={f.basis ?? `${originLabel(f.origin)} claim`}>
                        {originLabel(f.origin)}{#if f.confidence !== null && f.origin !== "user"} {f.confidence.toFixed(2)}{/if}
                      </span>
                    {:else}
                      <span class="chip none">no claim</span>
                    {/if}
                    {#if f.conflict}<span class="chip conflict" title="a lower-ranked claim disagrees">conflict</span>{/if}
                    {#if f.editable}
                      {#if f.origin && !isPinned(f)}
                        <button type="button" class="confirm" data-confirm={f.field} title="Pin this value as yours: it will not change when the source is re-learned or Claude writes" onclick={() => write(m, f, f.value)}>Confirm</button>
                      {/if}
                      <button type="button" class="edit" data-edit={f.field} onclick={() => startEdit(m, f)}>{f.origin ? "Edit" : "Set"}</button>
                    {/if}
                    {#if rowError?.metric === m.metric && rowError.field === f.field}<span class="error">{rowError.text}</span>{/if}
                    {#if f.claims.length > 1}
                      <details class="claims"><summary>{f.claims.length} claims</summary>
                        <ul>
                          {#each f.claims as c, i (i)}
                            <li><span class="chip origin {c.origin}">{originLabel(c.origin)} {c.confidence.toFixed(2)}</span> {fmtValue(c.value)}{#if c.basis} <span class="basis">— {c.basis}</span>{/if}</li>
                          {/each}
                        </ul>
                      </details>
                    {/if}
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if m.relations.length}
          <p class="card-rel"><strong>Related:</strong>
            {#each m.relations as r, i (i)}
              <span class="rel">{r.subject === m.metric ? "" : r.subject + " "}{r.kind.replace("_", " ")} {r.object === m.metric ? "" : r.object}{#if r.contested} <em>(contested)</em>{/if}</span>
            {/each}
          </p>
        {/if}
        {#if m.bindings.length}
          <p class="card-rel"><strong>Models:</strong>
            {#each m.bindings as b (b.kind + b.key)}
              <span class="rel">{b.kind} {b.key}: {Object.entries(b.roles).map(([role, x]) => `${role}=${x ?? "?"}`).join(", ")}</span>
            {/each}
          </p>
        {/if}
        {#if m.gaps.length}
          <p class="card-rel"><strong>Gaps:</strong>
            {#each m.gaps as g (g.id)}<span class="rel"><span class="obj-id">{g.id}</span> {g.role} ({g.binding})</span>{/each}
          </p>
        {/if}
      </section>
    {/each}
    <section class="card-profile" data-card-profile>
      <h4>Operating profile</h4>
      {#if card.profile.available}
        <p>
          {fmtDuration(card.profile.window_ms ?? 0)} window · {card.profile.series_total} series
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
