<script lang="ts">
  import { postClaim, type CardField, type CardMetric } from "../lib/api";
  import { editControl, fmtValue, isPinned, originLabel } from "../lib/card";
  import RefText from "./RefText.svelte";

  /** One metric's catalog section: every field with its competing claims, editable in place.
   *  Shared by the panel's metric card and the catalog view. */
  let { source, m, onchange }: { source: string; m: CardMetric; onchange: () => void } = $props();

  let editing = $state<{ field: string; draft: string } | null>(null);
  let rowError = $state<{ field: string; text: string } | null>(null);

  const write = (f: CardField, value: unknown) => {
    rowError = null;
    postClaim(source, m.metric, f.field, value)
      .then(() => { editing = null; onchange(); })
      .catch((e) => (rowError = { field: f.field, text: String(e) }));
  };
  const startEdit = (f: CardField) =>
    (editing = { field: f.field, draft: f.value === null || f.value === undefined ? "" : String(f.value) });
  const isEditing = (f: CardField) => editing?.field === f.field;
</script>

      <section class="card-metric" data-card-metric={m.metric}>
        <h4><code>{m.metric}</code>{#if !m.present} <span class="chip">no longer in the source</span>{/if}</h4>
        {#if m.family?.role === "family"}
          <p class="card-rel" data-card-family>A <strong>name family</strong>: {m.family.members.toLocaleString("en-US")} metrics whose names differ only where <code>*</code> stands ({m.family.status}). Claims here apply to all of them.</p>
        {:else if m.family?.role === "member"}
          <p class="card-rel" data-card-family>Member of the name family <code>{m.family.template}</code>{#if m.family.dimension}, dimension <code>{m.family.dimension}</code>{/if}.{#if m.family.inherited} Claims shown are the family's; setting one here overrides them for this metric.{/if}</p>
        {/if}
        <table class="card-fields">
          <tbody>
            {#each m.fields as f (f.field)}
              {@const ctl = editControl(f)}
              <tr data-field={f.field} class:conflict={f.conflict}>
                <th>{f.field.replace("_", " ")}</th>
                <td>
                  {#if isEditing(f)}
                    {#if ctl?.kind === "select"}
                      <select bind:value={editing!.draft} aria-label="Edit {f.field}">
                        <option value="">—</option>
                        {#each ctl.options as o (o)}<option value={o}>{o}</option>{/each}
                      </select>
                    {:else}
                      <input type="text" bind:value={editing!.draft} aria-label="Edit {f.field}"
                        onkeydown={(e) => { if (e.key === "Enter" && editing!.draft.trim()) write(f, editing!.draft.trim()); if (e.key === "Escape") editing = null; }} />
                    {/if}
                    <button type="button" disabled={!editing!.draft.trim()} onclick={() => write(f, editing!.draft.trim())}>Save</button>
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
                        <button type="button" class="confirm" data-confirm={f.field} title="Pin this value as yours: it will not change when the source is re-learned or Claude writes" onclick={() => write(f, f.value)}>Confirm</button>
                      {/if}
                      <button type="button" class="edit" data-edit={f.field} onclick={() => startEdit(f)}>{f.origin ? "Edit" : "Set"}</button>
                    {/if}
                    {#if rowError?.field === f.field}<span class="error">{rowError.text}</span>{/if}
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
