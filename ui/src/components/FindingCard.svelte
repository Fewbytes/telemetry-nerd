<script lang="ts">
  import { postJSON, type Annotation, type Finding } from "../lib/api";
  import { refLabel, scopeLine, statLine } from "../lib/format";

  let {
    finding,
    annotations = [],
  }: { finding: Finding; annotations?: Annotation[] } = $props();

  let comment = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const annPanels = $derived(new Map(annotations.map((a) => [a.id, a.panel])));

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
</script>

<article class="finding" id="finding-{finding.id}" data-verdict={finding.verdict ?? "none"}>
  <h3 class="claim"><span class="obj-id">{finding.id}</span> {finding.claim}</h3>
  <p class="scope">{scopeLine(finding.scope)}</p>

  {#if finding.evidence.length > 0}
    <ul class="evidence">
      {#each finding.evidence as ref, i (i)}
        <li>
          {#if ref.kind === "panel"}
            <a href="#/panel/{ref.panel}">{refLabel(ref)}</a>
          {:else if ref.kind === "annotation"}
            {@const panel = annPanels.get(ref.annotation)}
            {#if panel}
              <a href="#/panel/{panel}">{refLabel(ref)}</a>
            {:else}
              {refLabel(ref)}
            {/if}
          {:else}
            {statLine(ref)}
          {/if}
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
    {#if finding.verdict}
      <span class="chip verdict-chip">{finding.verdict}</span>
      {#if finding.verdict_comment}<span class="verdict-comment">“{finding.verdict_comment}”</span>{/if}
    {/if}
  </div>

  <div class="verdict-row">
    <button type="button" disabled={busy} onclick={() => verdict("accepted")}>Accept</button>
    <button type="button" disabled={busy} onclick={() => verdict("rejected")}>Reject</button>
    <button type="button" disabled={busy} onclick={() => verdict("needs-more")}>Needs more</button>
    <input
      bind:value={comment}
      placeholder="Optional comment"
      aria-label="Verdict comment for {finding.id}"
    />
  </div>
  {#if error}<div class="error">{error}</div>{/if}
</article>
