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
  const folded = (key: string) => (simple ? !opened.has(key) && (key === "hypotheses" || key === "gaps") : collapsedSidebar.has(key));

  // each section folds independently (telemetry-nerd-prli), freeing vertical space; once every
  // section present is folded, the sidebar itself hides and the panel grid reclaims its column
  const sections = $derived(["chat", "hypotheses", "findings", ...(orphans.length > 0 ? ["annotations"] : []), ...(runs.length > 0 ? ["code"] : []), "gaps"]);
  const allCollapsed = $derived(!simple && sections.every((s) => collapsedSidebar.has(s)));
</script>

<aside class="sidebar" class:all-collapsed={allCollapsed}>
  <section>
    <header>
      <button
        type="button" class="collapse" aria-pressed={collapsedSidebar.has("chat")} aria-expanded={!collapsedSidebar.has("chat")}
        title={collapsedSidebar.has("chat") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("chat") ? "Expand Chat" : "Collapse Chat"}
        onclick={() => collapsedSidebar.toggle("chat")}
      >{collapsedSidebar.has("chat") ? "▸" : "▾"}</button>
      <h2>Chat</h2>
    </header>
    {#if !collapsedSidebar.has("chat")}<ChatThread thread={chat} />{/if}
  </section>

  <section>
    <header>
      <button
        type="button" class="collapse" aria-pressed={folded("hypotheses")} aria-expanded={!folded("hypotheses")}
        title={folded("hypotheses") ? "Expand" : "Collapse"} aria-label={folded("hypotheses") ? `Expand Hypotheses${simple ? ` (${hypotheses.length})` : ""}` : "Collapse Hypotheses"}
        onclick={() => (simple ? toggleOpened("hypotheses") : collapsedSidebar.toggle("hypotheses"))}
      >{folded("hypotheses") ? "▸" : "▾"}</button>
      <h2>Hypotheses{#if simple}{" "}({hypotheses.length}){/if}</h2>
    </header>
    {#if !folded("hypotheses")}
      {#if hypotheses.length === 0}
        <p class="none">No hypotheses yet.</p>
      {:else}
        <HypothesisList {hypotheses} {findings} />
      {/if}
    {/if}
  </section>

  <section>
    <header>
      <button
        type="button" class="collapse" aria-pressed={collapsedSidebar.has("findings")} aria-expanded={!collapsedSidebar.has("findings")}
        title={collapsedSidebar.has("findings") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("findings") ? "Expand Findings" : "Collapse Findings"}
        onclick={() => collapsedSidebar.toggle("findings")}
      >{collapsedSidebar.has("findings") ? "▸" : "▾"}</button>
      <h2>Findings</h2>
    </header>
    {#if !collapsedSidebar.has("findings")}
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
      <header>
        <button
          type="button" class="collapse" aria-pressed={collapsedSidebar.has("annotations")} aria-expanded={!collapsedSidebar.has("annotations")}
          title={collapsedSidebar.has("annotations") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("annotations") ? "Expand Annotations on closed panels" : "Collapse Annotations on closed panels"}
          onclick={() => collapsedSidebar.toggle("annotations")}
        >{collapsedSidebar.has("annotations") ? "▸" : "▾"}</button>
        <h2>Annotations on closed panels</h2>
      </header>
      {#if !collapsedSidebar.has("annotations")}
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
      <header>
        <button
          type="button" class="collapse" aria-pressed={collapsedSidebar.has("code")} aria-expanded={!collapsedSidebar.has("code")}
          title={collapsedSidebar.has("code") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("code") ? "Expand Code runs" : "Collapse Code runs"}
          onclick={() => collapsedSidebar.toggle("code")}
        >{collapsedSidebar.has("code") ? "▸" : "▾"}</button>
        <h2>Code runs</h2>
      </header>
      {#if !collapsedSidebar.has("code")}<CodeRuns {runs} onopen={onopencode} />{/if}
    </section>
  {/if}

  <section>
    <header>
      <button
        type="button" class="collapse" aria-pressed={folded("gaps")} aria-expanded={!folded("gaps")}
        title={folded("gaps") ? "Expand" : "Collapse"} aria-label={folded("gaps") ? `Expand Gaps${simple ? ` (${gaps.length})` : ""}` : "Collapse Gaps"}
        onclick={() => (simple ? toggleOpened("gaps") : collapsedSidebar.toggle("gaps"))}
      >{folded("gaps") ? "▸" : "▾"}</button>
      <h2>Gaps{#if simple}{" "}({gaps.length}){/if}</h2>
    </header>
    {#if !folded("gaps")}
      {#if gaps.length === 0}
        <p class="none">No gaps yet.</p>
      {:else}
        <GapList {gaps} />
      {/if}
    {/if}
  </section>
</aside>
