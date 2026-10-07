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

  // each section folds independently (telemetry-nerd-prli), freeing vertical space; once every
  // section present is folded, the sidebar itself hides and the panel grid reclaims its column
  const sections = $derived(["chat", "hypotheses", "findings", ...(orphans.length > 0 ? ["annotations"] : []), ...(runs.length > 0 ? ["code"] : []), "gaps"]);
  const allCollapsed = $derived(sections.every((s) => collapsedSidebar.has(s)));
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
        type="button" class="collapse" aria-pressed={collapsedSidebar.has("hypotheses")} aria-expanded={!collapsedSidebar.has("hypotheses")}
        title={collapsedSidebar.has("hypotheses") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("hypotheses") ? "Expand Hypotheses" : "Collapse Hypotheses"}
        onclick={() => collapsedSidebar.toggle("hypotheses")}
      >{collapsedSidebar.has("hypotheses") ? "▸" : "▾"}</button>
      <h2>Hypotheses</h2>
    </header>
    {#if !collapsedSidebar.has("hypotheses")}
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

  {#if orphans.length > 0}
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

  {#if runs.length > 0}
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
        type="button" class="collapse" aria-pressed={collapsedSidebar.has("gaps")} aria-expanded={!collapsedSidebar.has("gaps")}
        title={collapsedSidebar.has("gaps") ? "Expand" : "Collapse"} aria-label={collapsedSidebar.has("gaps") ? "Expand Gaps" : "Collapse Gaps"}
        onclick={() => collapsedSidebar.toggle("gaps")}
      >{collapsedSidebar.has("gaps") ? "▸" : "▾"}</button>
      <h2>Gaps</h2>
    </header>
    {#if !collapsedSidebar.has("gaps")}
      {#if gaps.length === 0}
        <p class="none">No gaps yet.</p>
      {:else}
        <GapList {gaps} />
      {/if}
    {/if}
  </section>
</aside>
