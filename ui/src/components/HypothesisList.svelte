<script lang="ts">
  import { postJSON, setHypothesisHidden, type Finding, type Hypothesis } from "../lib/api";
  import { isSendKey } from "../lib/keys";
  import { hypothesisScopeText, hypothesisView, STATUS_FLOW, visibleHypotheses, type FindingEntry } from "../lib/findings";
  import PinButton from "./PinButton.svelte";

  let { hypotheses = [], findings = [] }: { hypotheses?: Hypothesis[]; findings?: Finding[] } = $props();

  let notes = $state<Record<string, string>>({});
  // hide has its own reason, separate from the status note above (fygk): a housekeeping note
  // about putting a hypothesis aside must never land in `status_reason` (an evidence claim)
  // just because the same button area was used, and vice versa
  let hideReasons = $state<Record<string, string>>({});
  let busy = $state<string | null>(null);
  let error = $state<string | null>(null);
  // hidden hypotheses (fygk) are put aside, not deleted; off by default so they don't clutter
  // the board, shown again with this toggle (still citable, never a verdict)
  let showHidden = $state(false);

  const view = $derived(visibleHypotheses(hypotheses, showHidden, findings));
  const active = $derived(view.active);
  const ruledOut = $derived(view.ruledOut);
  const hiddenCount = $derived(view.hiddenCount);

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

  const setHidden = (h: Hypothesis, hidden: boolean) => {
    busy = h.id;
    error = null;
    const reason = (hideReasons[h.id] ?? "").trim();
    setHypothesisHidden(h.id, hidden, hidden && reason ? reason : undefined)
      // only clear the field that was actually used: unhiding never reads a reason, so any
      // half-typed text in it (for a later hide) is left alone
      .then(() => { if (hidden) hideReasons[h.id] = ""; })
      .catch((e) => (error = String(e)))
      .finally(() => (busy = null));
  };
</script>

{#snippet group(h: Hypothesis, kind: "for" | "against" | "linked", label: string, entries: FindingEntry[])}
  <div class="ev-group {kind}">
    <h4 class="sub">{label} <span class="count">{entries.length}</span></h4>
    {#if entries.length === 0}
      <p class="none">none</p>
    {:else}
      <ul aria-label="{label} {h.id}">
        {#each entries as e (e.id)}
          <li class:rejected={e.rejected}>
            <a class="obj-id" href="#finding-{e.id}" aria-label="Finding {e.id}: {e.claim}">{e.id}</a>
            <span class="claim-text">{e.claim}</span>
            <span class="chip verdict-chip">{e.verdict}</span>
            {#if e.flagged > 0}<span class="chip uncertainty-flag">{e.flagged} flagged</span>{/if}
            {#each e.caveats as c (c)}<span class="chip caveat">{c}</span>{/each}
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/snippet}

{#snippet item(h: Hypothesis)}
  {@const v = hypothesisView(h, findings)}
  {@const scope = hypothesisScopeText(h.scope)}
  <li class="hypothesis" id="hypothesis-{h.id}" data-status={h.status} aria-labelledby="statement-{h.id}">
    <div class="statement" id="statement-{h.id}"><span class="obj-id">{h.id}</span> {h.statement} <PinButton object={h.id} /></div>
    {#if scope}
      <div class="scope-line" class:text-scope={!!h.scope?.text}>
        <span class="sub">Scope</span> <span class="scope-text">{scope}</span>
        {#if h.scope?.text}<span class="chip caveat" title="given as prose: shown, not checked">as text</span>{/if}
      </div>
    {/if}
    <div class="status-line">
      <span class="chip status-chip {h.status}"><span class="sr-only">Status: </span>{h.status}</span>
      <span class="badge author {h.author}">{h.author}</span>
    </div>
    {#if h.status_reason && (h.status === "refuted" || h.status === "inconclusive")}
      <p class="status-reason"><span class="sub">Why</span> {h.status_reason}</p>
    {/if}
    {#if h.hidden}
      <div class="status-line">
        <span class="chip hidden-chip">hidden</span>
        {#if h.hidden_reason}<span class="hidden-reason">{h.hidden_reason}</span>{/if}
      </div>
    {/if}
    <div class="evidence-groups">
      {@render group(h, "for", "For", v.for)}
      {@render group(h, "against", "Against", v.against)}
      {#if v.linked.length > 0}{@render group(h, "linked", "Linked", v.linked)}{/if}
    </div>
    <div class="controls" role="group" aria-label="Set status for {h.id}">
      {#each STATUS_FLOW as s (s)}
        <button
          type="button"
          class="step {s}"
          disabled={busy === h.id}
          aria-pressed={h.status === s}
          aria-label="Mark {h.id} {s}"
          onclick={() => h.status !== s && setStatus(h, s)}>{s}</button>
      {/each}
      <button
        type="button"
        class="step hide"
        disabled={busy === h.id}
        aria-pressed={h.hidden}
        aria-label="{h.hidden ? 'Unhide' : 'Hide'} {h.id}"
        onclick={() => setHidden(h, !h.hidden)}>{h.hidden ? "Unhide" : "Hide"}</button>
      {#if !h.hidden}
        <input
          bind:value={hideReasons[h.id]}
          placeholder="Reason for hiding (optional)"
          aria-label="Hide reason for {h.id}"
          class="hide-reason-input"
        />
      {/if}
      <input
        bind:value={notes[h.id]}
        placeholder="Optional note"
        aria-label="Status note for {h.id}"
        onkeydown={(e) => {
          // Cmd/Ctrl+Enter re-applies the current status with the note
          if (isSendKey(e) && busy !== h.id) {
            e.preventDefault();
            setStatus(h, h.status);
          }
        }}
      />
    </div>
  </li>
{/snippet}

<label class="show-hidden">
  <input type="checkbox" bind:checked={showHidden} />
  Show hidden{#if hiddenCount} ({hiddenCount}){/if}
</label>
<ul class="hypotheses" aria-label="Hypotheses">
  {#each active as h (h.id)}
    {@render item(h)}
  {/each}
</ul>

{#if ruledOut.length > 0}
  <h3 class="ruled-out">Ruled out</h3>
  <ul class="hypotheses ruled-out-list" aria-label="Ruled-out hypotheses">
    {#each ruledOut as h (h.id)}
      {@render item(h)}
    {/each}
  </ul>
{/if}
{#if error}<div class="error" role="alert">{error}</div>{/if}
