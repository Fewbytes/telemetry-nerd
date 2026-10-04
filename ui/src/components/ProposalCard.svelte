<script lang="ts">
  import { tick } from "svelte";
  import { decideProposal, refuteLesson, type CatalogProposalItem, type Decision, type LessonItem } from "../lib/api";
  import { STATE_TEXT, evidenceRefs, expiresText, parseEdit, scopeText, valueText } from "../lib/proposals";

  let {
    item, active, now, ondecided,
  }: {
    item: { kind: "lesson"; lesson: LessonItem } | { kind: "catalog"; proposal: CatalogProposalItem };
    /** the workspace on screen: evidence elsewhere is named, not linked */
    active: string | null;
    now: number;
    /** after a decision lands: the parent reloads and moves focus */
    ondecided: (id: string) => void;
  } = $props();

  const lesson = $derived(item.kind === "lesson" ? item.lesson : null);
  const proposal = $derived(item.kind === "catalog" ? item.proposal : null);
  const base = $derived(item.kind === "lesson" ? item.lesson : item.proposal);
  const id = $derived(base.id);
  const phase = $derived(lesson ? lesson.state : (proposal?.status ?? "proposed"));
  const pending = $derived(phase === "proposed");
  const refs = $derived(evidenceRefs(base.evidence, base.evidence_where, active));

  let mode = $state<"view" | "edit" | "refute">("view");
  let comment = $state("");
  let draftText = $state("");
  let draftExpires = $state("");
  let draftValue = $state("");
  let reason = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);
  let editButton = $state<HTMLButtonElement>();
  let refuteButton = $state<HTMLButtonElement>();
  let firstField = $state<HTMLElement>();

  const send = (action: () => Promise<unknown>) => {
    if (busy) return;
    busy = true;
    error = null;
    action()
      .then(() => { mode = "view"; ondecided(id); })
      .catch((e) => (error = e instanceof Error ? e.message : String(e)))
      .finally(() => (busy = false));
  };
  const decide = (decision: Decision["decision"], extra: Partial<Decision> = {}) => {
    const body: Decision = { decision, ...extra };
    if (comment.trim()) body.comment = comment.trim();
    send(() => decideProposal(id, body));
  };

  const startEdit = async () => {
    draftText = lesson?.text ?? "";
    draftExpires = "";
    draftValue = proposal ? valueText(proposal.value) : "";
    mode = "edit";
    await tick();
    firstField?.focus();
  };
  const cancel = async () => {
    const wasRefute = mode === "refute";
    mode = "view";
    error = null;
    await tick(); // the buttons re-render on leaving the form: read the bindings after
    (wasRefute ? refuteButton : editButton)?.focus();
  };
  const saveEdit = (e: SubmitEvent) => {
    e.preventDefault();
    if (lesson) {
      if (!draftText.trim()) { error = "a lesson needs its text"; return; }
      decide("approve", { text: draftText.trim(), ...(draftExpires.trim() ? { expires: draftExpires.trim() } : {}) });
      return;
    }
    try {
      decide("approve", { value: parseEdit(proposal?.value, draftValue) });
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  };
  const startRefute = async () => {
    reason = "";
    mode = "refute";
    await tick();
    firstField?.focus();
  };
  const saveRefute = (e: SubmitEvent) => {
    e.preventDefault();
    if (!reason.trim()) { error = "say why the lesson no longer holds"; return; }
    send(() => refuteLesson(id, reason.trim()));
  };
  // Escape cancels and returns focus to the button that opened the form; Enter saves
  // (native for inputs; in the lesson text, Shift+Enter is a new line)
  const onFieldKey = (e: KeyboardEvent & { currentTarget: HTMLInputElement | HTMLTextAreaElement }) => {
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
    if (e.key === "Enter" && !e.shiftKey && e.currentTarget instanceof HTMLTextAreaElement) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  };
</script>

<article class="proposal" id="proposal-{id}" data-proposal={id} data-state={phase} aria-labelledby="proposal-title-{id}">
  <h4 class="proposal-title" id="proposal-title-{id}" tabindex="-1">
    <span class="obj-id">{id}</span>
    {#if lesson}{lesson.text}{:else if proposal}<code>{proposal.metric}</code> {proposal.field} = <code>{valueText(proposal.value)}</code>{/if}
    <span class="chip state" data-state={phase}><span class="sr-only">Status: </span>{STATE_TEXT[phase]}</span>
  </h4>

  <dl class="proposal-meta">
    {#if lesson}
      <div><dt>scope</dt><dd data-scope>{scopeText(lesson.scope)}</dd></div>
      {#if lesson.proposed_text}<div><dt>proposed as</dt><dd>{lesson.proposed_text}</dd></div>{/if}
      <div><dt>covered by</dt><dd>{lesson.scope_check.covered_by.join(", ")}{#if lesson.scope_check.partial.length}<span class="basis"> · partial: {lesson.scope_check.partial.map((p) => `${p.id} ${p.why}`).join("; ")}</span>{/if}</dd></div>
      {#if phase === "approved" || phase === "proposed"}<div><dt>expiry</dt><dd>{expiresText(lesson.expires_at_ms, now)}</dd></div>{/if}
      {#if lesson.refute_reason}<div><dt>refuted</dt><dd>{lesson.refute_reason}{#if lesson.refuted_by.length} ({lesson.refuted_by.join(", ")}){/if}</dd></div>{/if}
    {:else if proposal}
      <div><dt>source</dt><dd>{proposal.source}</dd></div>
      <div><dt>basis</dt><dd>{proposal.basis} <span class="basis">(confidence {proposal.confidence.toFixed(2)})</span></dd></div>
      {#if proposal.edited}<div><dt>approved as</dt><dd><code>{valueText(proposal.decided_value)}</code> (your value)</dd></div>{/if}
    {/if}
    {#if base.comment}<div><dt>comment</dt><dd>“{base.comment}”</dd></div>{/if}
    <div>
      <dt>evidence</dt>
      <dd class="evidence-refs">
        {#each refs as r (r.id)}
          {#if r.href}<a class="obj-link" href={r.href}>{r.id}</a>{:else}<span class="obj-link plain" title="evidence {r.note}">{r.id} ({r.note})</span>{/if}
        {:else}<span class="none">none cited</span>{/each}
      </dd>
    </div>
  </dl>

  {#if mode === "edit"}
    <form class="proposal-form" aria-label="Edit {id}" onsubmit={saveEdit}>
      {#if lesson}
        <label>Lesson text
          <textarea bind:this={firstField} bind:value={draftText} onkeydown={onFieldKey} rows="3" maxlength="500"></textarea>
        </label>
        <label>Expires (e.g. 90d or 2027-03-01; empty keeps {expiresText(lesson.expires_at_ms, now)})
          <input type="text" bind:value={draftExpires} onkeydown={onFieldKey} />
        </label>
      {:else}
        <label>Value
          <input type="text" bind:this={firstField} bind:value={draftValue} onkeydown={onFieldKey} />
        </label>
      {/if}
      <div class="proposal-actions">
        <button type="submit" disabled={busy}>Save and approve</button>
        <button type="button" onclick={cancel}>Cancel</button>
      </div>
    </form>
  {:else if mode === "refute"}
    <form class="proposal-form" aria-label="Refute {id}" onsubmit={saveRefute}>
      <label>Why it no longer holds
        <input type="text" bind:this={firstField} bind:value={reason} onkeydown={onFieldKey} />
      </label>
      <div class="proposal-actions">
        <button type="submit" disabled={busy}>Refute</button>
        <button type="button" onclick={cancel}>Cancel</button>
      </div>
    </form>
  {:else if pending}
    <div class="proposal-actions" role="group" aria-label="Decision on {id}">
      <button type="button" class="approve" disabled={busy} onclick={() => decide("approve")}>Approve</button>
      <button type="button" bind:this={editButton} disabled={busy} onclick={startEdit}>Edit</button>
      <button type="button" class="reject" disabled={busy} onclick={() => decide("reject")}>Reject</button>
      <input bind:value={comment} placeholder="Optional comment" aria-label="Comment on {id}" />
    </div>
  {:else if lesson && phase === "approved"}
    <div class="proposal-actions" role="group" aria-label="Actions on {id}">
      <button type="button" bind:this={refuteButton} onclick={startRefute}>Refute</button>
    </div>
  {/if}
  {#if error}<div class="error" role="alert">{error}</div>{/if}
</article>
