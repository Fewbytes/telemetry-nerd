<script lang="ts">
  import type { Snapshot } from "../lib/api";
  import FindingCard from "./FindingCard.svelte";
  import GapList from "./GapList.svelte";
  import HypothesisList from "./HypothesisList.svelte";
  import CodeRuns from "./CodeRuns.svelte";
  import ChatThread from "./ChatThread.svelte";
  import { orphanedAnnotations } from "../lib/orphans";
  import { chatThread } from "../lib/refs";
  import { collapsedSidebar } from "../lib/collapsed.svelte";
  import { mode } from "../lib/mode.svelte";

  let { snapshot, onopencode }: { snapshot: Snapshot | null; onopencode: (id: string) => void } = $props();
  const runs = $derived(snapshot?.code ?? []);

  const hypotheses = $derived(snapshot?.hypotheses ?? []);
  const findings = $derived(snapshot?.findings ?? []);
  const gaps = $derived(snapshot?.gaps ?? []);
  const annotations = $derived(snapshot?.annotations ?? []);
  const orphans = $derived(orphanedAnnotations(annotations, snapshot?.panels ?? []));
  const openFindings = $derived(findings.filter((f) => f.verdict !== "rejected"));
  const rejectedFindings = $derived(findings.filter((f) => f.verdict === "rejected"));

  const chat = $derived(chatThread(snapshot?.threads ?? []));

  let showRejected = $state(false);

  // simple mode: hypotheses and gaps are one-line headers, unfolded by local state so the persisted
  // nerd fold preferences stay untouched; code runs and closed-panel annotations are not rendered
  const simple = $derived(mode.effective === "simple");
  let opened = $state(new Set<string>());
  const toggleOpened = (k: string) => {
    const n = new Set(opened);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    opened = n;
  };
  const localFold = (key: string) => simple && (key === "hypotheses" || key === "gaps");
  const folded = (key: string) => (localFold(key) ? !opened.has(key) : collapsedSidebar.has(key));
  const toggleFold = (key: string) => (localFold(key) ? toggleOpened(key) : collapsedSidebar.toggle(key));

  // each section folds independently (telemetry-nerd-prli), freeing vertical space; once every
  // section present is folded, the sidebar itself hides and the panel grid reclaims its column
  const sections = $derived(["chat", "hypotheses", "findings", ...(orphans.length > 0 ? ["annotations"] : []), ...(runs.length > 0 ? ["code"] : []), "gaps"]);
  const allCollapsed = $derived(!simple && sections.every((s) => collapsedSidebar.has(s)));
</script>

<!-- a section's fold toggle and title; `count` (simple mode) follows the title -->
{#snippet foldHeader(key: string, title: string, count?: number)}
  <header>
    <button
      type="button" class="collapse" aria-pressed={folded(key)} aria-expanded={!folded(key)}
      title={folded(key) ? "Expand" : "Collapse"} aria-label={folded(key) ? `Expand ${title}${count === undefined ? "" : ` (${count})`}` : `Collapse ${title}`}
      onclick={() => toggleFold(key)}
    >{folded(key) ? "▸" : "▾"}</button>
    <h2>{title}{#if count !== undefined}{" "}({count}){/if}</h2>
  </header>
{/snippet}

<aside class="sidebar" class:all-collapsed={allCollapsed}>
  <section>
    {@render foldHeader("chat", "Chat")}
    {#if !folded("chat")}<ChatThread thread={chat} />{/if}
  </section>

  <section>
    {@render foldHeader("hypotheses", "Hypotheses", simple ? hypotheses.length : undefined)}
    {#if !folded("hypotheses")}
      {#if hypotheses.length === 0}
        <p class="none">No hypotheses yet.</p>
      {:else}
        <HypothesisList {hypotheses} {findings} />
      {/if}
    {/if}
  </section>

  <section>
    {@render foldHeader("findings", "Findings")}
    {#if !folded("findings")}
      {#if findings.length === 0}
        <p class="none">No findings yet.</p>
      {:else}
        {#each openFindings as finding (finding.id)}
          <FindingCard {finding} {annotations} panels={snapshot?.panels ?? []} code={runs} />
        {/each}
        {#if rejectedFindings.length > 0}
          <button
            type="button"
            class="toggle-rejected"
            aria-expanded={showRejected}
            onclick={() => (showRejected = !showRejected)}
          >
            {showRejected ? "hide" : "show"} rejected ({rejectedFindings.length})
          </button>
          {#if showRejected}
            {#each rejectedFindings as finding (finding.id)}
              <FindingCard {finding} {annotations} panels={snapshot?.panels ?? []} code={runs} />
            {/each}
          {/if}
        {/if}
      {/if}
    {/if}
  </section>

  {#if !simple && orphans.length > 0}
    <section>
      {@render foldHeader("annotations", "Annotations on closed panels")}
      {#if !folded("annotations")}
        <ul class="orphans">
          {#each orphans as a (a.id)}
            <li id="annotation-{a.id}">
              <span class="obj-id">{a.id}</span> {a.kind}: {a.label}
              <span class="chip">{a.panel}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}

  {#if !simple && runs.length > 0}
    <section aria-label="Code runs">
      {@render foldHeader("code", "Code runs")}
      {#if !folded("code")}<CodeRuns {runs} onopen={onopencode} />{/if}
    </section>
  {/if}

  <section>
    {@render foldHeader("gaps", "Gaps", simple ? gaps.length : undefined)}
    {#if !folded("gaps")}
      {#if gaps.length === 0}
        <p class="none">No gaps yet.</p>
      {:else}
        <GapList {gaps} />
      {/if}
    {/if}
  </section>
</aside>
