<script lang="ts">
  import type { Snapshot } from "../lib/api";
  import FindingCard from "./FindingCard.svelte";
  import GapList from "./GapList.svelte";
  import HypothesisList from "./HypothesisList.svelte";

  let { snapshot }: { snapshot: Snapshot | null } = $props();

  const hypotheses = $derived(snapshot?.hypotheses ?? []);
  const findings = $derived(snapshot?.findings ?? []);
  const gaps = $derived(snapshot?.gaps ?? []);
  const annotations = $derived(snapshot?.annotations ?? []);
  const openFindings = $derived(findings.filter((f) => f.verdict !== "rejected"));
  const rejectedFindings = $derived(findings.filter((f) => f.verdict === "rejected"));

  let showRejected = $state(false);
</script>

<aside class="sidebar">
  <section>
    <h2>Hypotheses</h2>
    {#if hypotheses.length === 0}
      <p class="none">No hypotheses yet.</p>
    {:else}
      <HypothesisList {hypotheses} />
    {/if}
  </section>

  <section>
    <h2>Findings</h2>
    {#if findings.length === 0}
      <p class="none">No findings yet.</p>
    {:else}
      {#each openFindings as finding (finding.id)}
        <FindingCard {finding} {annotations} />
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
            <FindingCard {finding} {annotations} />
          {/each}
        {/if}
      {/if}
    {/if}
  </section>

  <section>
    <h2>Gaps</h2>
    {#if gaps.length === 0}
      <p class="none">No gaps yet.</p>
    {:else}
      <GapList {gaps} />
    {/if}
  </section>
</aside>
