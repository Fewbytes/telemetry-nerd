<script lang="ts">
  import { postJSON, type Finding, type Hypothesis } from "../lib/api";
  import { supportingCaveats } from "../lib/caveats";
  import { isSendKey } from "../lib/keys";
  import PinButton from "./PinButton.svelte";

  let { hypotheses = [], findings = [] }: { hypotheses?: Hypothesis[]; findings?: Finding[] } = $props();

  let notes = $state<Record<string, string>>({});
  let busy = $state<string | null>(null);
  let error = $state<string | null>(null);

  const active = $derived(hypotheses.filter((h) => h.status !== "refuted"));
  const ruledOut = $derived(hypotheses.filter((h) => h.status === "refuted"));

  const setStatus = (h: Hypothesis, status: Hypothesis["status"]) => {
    busy = h.id;
    error = null;
    const body: Record<string, unknown> = { status };
    const note = (notes[h.id] ?? "").trim();
    if (note) body.note = note;
    postJSON(`/api/hypotheses/${h.id}/status`, body)
      .then(() => (notes[h.id] = ""))
      .catch((e) => (error = String(e)))
      .finally(() => (busy = null));
  };
</script>

{#snippet item(h: Hypothesis)}
  <li class="hypothesis" id="hypothesis-{h.id}" data-status={h.status}>
    <div class="statement"><span class="obj-id">{h.id}</span> {h.statement} <PinButton object={h.id} /></div>
    <div class="evidence">
      <span class="for">
        for {h.evidence_for.length}
        {#each h.evidence_for as fid (fid)}<a href="#finding-{fid}">{fid}</a>{/each}
      </span>
      <span class="against">
        against {h.evidence_against.length}
        {#each h.evidence_against as fid (fid)}<a href="#finding-{fid}">{fid}</a>{/each}
      </span>
    </div>
    {#each supportingCaveats(h, findings) as c (c.finding + c.caveat)}
      <div class="caveats"><span class="chip caveat">{c.finding}: {c.caveat}</span></div>
    {/each}
    <div class="controls">
      <span class="chip status-chip {h.status}">{h.status}</span>
      <select
        value={h.status}
        disabled={busy === h.id}
        aria-label="Status for {h.id}"
        onchange={(e) => setStatus(h, e.currentTarget.value as Hypothesis["status"])}
      >
        <option value="proposed">proposed</option>
        <option value="supported">supported</option>
        <option value="refuted">refuted</option>
        <option value="inconclusive">inconclusive</option>
      </select>
      <input
        bind:value={notes[h.id]}
        placeholder="Optional note"
        aria-label="Status note for {h.id}"
        onkeydown={(e) => {
          // Cmd/Ctrl+Enter applies the currently selected status with the note
          if (isSendKey(e) && busy !== h.id) {
            e.preventDefault();
            setStatus(h, h.status);
          }
        }}
      />
      <span class="badge author {h.author}">{h.author}</span>
    </div>
  </li>
{/snippet}

<ul class="hypotheses">
  {#each active as h (h.id)}
    {@render item(h)}
  {/each}
</ul>

{#if ruledOut.length > 0}
  <h3 class="ruled-out">Ruled out</h3>
  <ul class="hypotheses ruled-out-list">
    {#each ruledOut as h (h.id)}
      {@render item(h)}
    {/each}
  </ul>
{/if}
{#if error}<div class="error">{error}</div>{/if}
