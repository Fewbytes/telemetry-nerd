<script lang="ts">
  import { postJSON, type Annotation, type CodeBrief, type Finding, type Panel } from "../lib/api";
  import { evidenceViews, scopeFields, verdictText } from "../lib/findings";
  import PinButton from "./PinButton.svelte";
  import ObjectLinks from "./ObjectLinks.svelte";

  let {
    finding,
    annotations = [],
    panels = [],
    code = [],
  }: { finding: Finding; annotations?: Annotation[]; panels?: Panel[]; code?: CodeBrief[] } = $props();

  let comment = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const evidence = $derived(evidenceViews(finding, { panels, code, annotations }));
  const scope = $derived(scopeFields(finding.scope));
  const flagged = $derived(evidence.filter((e) => e.flags.length > 0).length);

  const verdict = (v: "accepted" | "rejected" | "needs-more") => {
    busy = true;
    error = null;
    const body: Record<string, unknown> = { verdict: v };
    if (comment.trim()) body.comment = comment.trim();
    postJSON(`/api/findings/${finding.id}/verdict`, body)
      .then(() => (comment = ""))
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };
  const VERDICTS = [
    ["accepted", "Accept"],
    ["rejected", "Reject"],
    ["needs-more", "Needs more"],
  ] as const;
</script>

<article class="finding" id="finding-{finding.id}" data-verdict={finding.verdict ?? "none"} aria-labelledby="claim-{finding.id}">
  <h3 class="claim" id="claim-{finding.id}"><span class="obj-id">{finding.id}</span> {finding.claim} <PinButton object={finding.id} /></h3>

  <div class="verdict-state" data-verdict={finding.verdict ?? "none"}>
    <span class="chip verdict-chip"><span class="sr-only">Verdict: </span>{verdictText(finding.verdict)}</span>
    {#if flagged > 0}<span class="chip uncertainty-flag">{flagged} of {evidence.length} evidence flagged</span>{/if}
    {#if finding.verdict_comment}<span class="verdict-comment">“{finding.verdict_comment}”</span>{/if}
  </div>

  <dl class="scope" aria-label="Scope of {finding.id}">
    {#each scope as f (f.label)}
      <div><dt>{f.label}</dt><dd>{f.value}</dd></div>
    {/each}
  </dl>

  {#if evidence.length > 0}
    <h4 class="sub">Evidence</h4>
    <ul class="evidence" aria-label="Evidence for {finding.id}">
      {#each evidence as e (e.index)}
        <li data-kind={e.kind} data-uncertainty={e.stat?.uncertainty}>
          {#if e.stat}
            <span class="stat-name">{e.label}</span>
            <span class="stat-value">{e.stat.value}</span>
            <span class="stat-unc {e.stat.uncertainty}">{e.stat.uncertaintyText}</span>
            <span class="stat-method">{e.stat.method}</span>
          {:else}
            <span class="stat-name">{e.label}</span>
            {#if e.note}<span class="basis">{e.note}</span>{/if}
          {/if}
          {#if e.source}<span class="chip source" data-source={e.source.code} title="source of variation (spec §5.4)">{e.source.text}</span>{/if}
          {#each e.flags as f (f.flag)}
            <span class="chip uncertainty-flag" data-flag={f.flag} title={f.message}>{f.label}</span>
          {/each}
          {#if e.links.length > 0}<span class="links"><ObjectLinks links={e.links} /></span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if finding.caveats.length > 0}
    <div class="caveats">
      {#each finding.caveats as caveat, i (i)}<span class="chip caveat">{caveat}</span>{/each}
    </div>
  {/if}

  <div class="meta">
    <span class="badge author {finding.author}">{finding.author}</span>
    {#if finding.hypothesis}
      <a class="obj-link" href="#hypothesis-{finding.hypothesis}">{finding.stance ?? "relates to"} {finding.hypothesis}</a>
    {/if}
  </div>

  <div class="verdict-row" role="group" aria-label="Verdict for {finding.id}">
    {#each VERDICTS as [v, label] (v)}
      <button type="button" disabled={busy} aria-pressed={finding.verdict === v} onclick={() => verdict(v)}>{label}</button>
    {/each}
    <input bind:value={comment} placeholder="Optional comment" aria-label="Verdict comment for {finding.id}" />
  </div>
  {#if error}<div class="error" role="alert">{error}</div>{/if}
</article>
