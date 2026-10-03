<script lang="ts">
  import type { Snapshot } from "../lib/api";
  import FindingCard from "./FindingCard.svelte";
  import GapList from "./GapList.svelte";
  import HypothesisList from "./HypothesisList.svelte";
  import CodeRuns from "./CodeRuns.svelte";
  import ChatThread from "./ChatThread.svelte";
  import { orphanedAnnotations } from "../lib/orphans";
  import { chatThread } from "../lib/refs";

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
</script>

<aside class="sidebar">
  <section>
    <h2>Chat</h2>
    <ChatThread thread={chat} />
  </section>

  <section>
    <h2>Hypotheses</h2>
    {#if hypotheses.length === 0}
      <p class="none">No hypotheses yet.</p>
    {:else}
      <HypothesisList {hypotheses} {findings} />
    {/if}
  </section>

  <section>
    <h2>Findings</h2>
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
  </section>

  {#if orphans.length > 0}
    <section>
      <h2>Annotations on closed panels</h2>
      <ul class="orphans">
        {#each orphans as a (a.id)}
          <li id="annotation-{a.id}">
            <span class="obj-id">{a.id}</span> {a.kind}: {a.label}
            <span class="chip">{a.panel}</span>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if runs.length > 0}
    <section aria-label="Code runs">
      <h2>Code runs</h2>
      <CodeRuns {runs} onopen={onopencode} />
    </section>
  {/if}

  <section>
    <h2>Gaps</h2>
    {#if gaps.length === 0}
      <p class="none">No gaps yet.</p>
    {:else}
      <GapList {gaps} />
    {/if}
  </section>
</aside>
