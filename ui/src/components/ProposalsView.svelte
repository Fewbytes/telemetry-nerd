<script lang="ts">
  import { tick } from "svelte";
  import type { Proposals } from "../lib/api";
  import { lessonPending, nextFocus, proposalPending } from "../lib/proposals";
  import ProposalCard from "./ProposalCard.svelte";

  let { data, active, error = null, onreload }: {
    data: Proposals | null;
    /** the workspace on screen */
    active: string | null;
    error?: string | null;
    onreload: () => Promise<void>;
  } = $props();

  let showDecided = $state(false);
  let status = $state("");
  let heading = $state<HTMLElement>();
  let now = $state(Date.now());

  const items = $derived([
    ...(data?.lessons ?? []).map((lesson) => ({ kind: "lesson" as const, lesson, id: lesson.id, pending: lessonPending(lesson) })),
    ...(data?.catalog ?? []).map((proposal) => ({ kind: "catalog" as const, proposal, id: proposal.id, pending: proposalPending(proposal) })),
  ]);
  const pendingLessons = $derived(items.filter((i) => i.kind === "lesson" && i.pending));
  const pendingCatalog = $derived(items.filter((i) => i.kind === "catalog" && i.pending));
  const decided = $derived(items.filter((i) => !i.pending));

  // a decided item leaves the pending list: keep the keyboard user's place
  const decidedOne = async (id: string) => {
    const order = [...pendingLessons, ...pendingCatalog].map((i) => i.id);
    const next = nextFocus(order, id);
    await onreload();
    now = Date.now();
    status = `${id} saved`;
    await tick();
    const target = next ? document.getElementById(`proposal-title-${next}`) : heading;
    target?.focus();
  };
</script>

<section class="proposals-view" data-proposals-view aria-labelledby="proposals-heading">
  <header class="proposals-head">
    <h2 id="proposals-heading" tabindex="-1" bind:this={heading}>Proposals</h2>
    <p class="none">
      What Claude proposes at the end of an investigation (<code>/telemetry-nerd:wrap</code>). Nothing reaches the
      catalog or a later session until you approve it. Lessons surface only where their scope matches.
    </p>
  </header>
  <p class="sr-only" role="status" aria-live="polite">{status}</p>
  {#if error}<div class="error" role="alert">{error}</div>{/if}
  {#if data === null && !error}<p class="none">loading…</p>{/if}

  {#if data}
    <section aria-labelledby="lessons-heading">
      <h3 id="lessons-heading">Lessons awaiting review ({pendingLessons.length})</h3>
      {#each pendingLessons as i (i.id)}
        <ProposalCard item={i} {active} {now} ondecided={decidedOne} />
      {:else}<p class="none">No lessons awaiting review.</p>{/each}
    </section>

    <section aria-labelledby="catalog-proposals-heading">
      <h3 id="catalog-proposals-heading">Catalog proposals awaiting review ({pendingCatalog.length})</h3>
      {#each pendingCatalog as i (i.id)}
        <ProposalCard item={i} {active} {now} ondecided={decidedOne} />
      {:else}<p class="none">No catalog proposals awaiting review.</p>{/each}
    </section>

    {#if decided.length > 0}
      <button type="button" class="toggle-decided" aria-expanded={showDecided} aria-controls="decided-list" onclick={() => (showDecided = !showDecided)}>
        {showDecided ? "hide" : "show"} decided ({decided.length})
      </button>
      <div id="decided-list" hidden={!showDecided}>
        {#each decided as i (i.id)}
          <ProposalCard item={i} {active} {now} ondecided={decidedOne} />
        {/each}
      </div>
    {/if}
  {/if}
</section>
