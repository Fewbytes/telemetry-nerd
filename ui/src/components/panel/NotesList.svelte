<script lang="ts">
  // the panel's notes and caveats; a note with a where drives the focus band on the plot (setFocus)
  import type { Where } from "../../lib/api";
  import type { Note } from "../../lib/panelNotes";
  import { sourceText } from "../../lib/sources";
  import { hasFocus } from "../../chart/focus";

  let { notes, activeNotes, setFocus }: {
    notes: Note[]; activeNotes: string[]; setFocus: (w: Where | null) => void;
  } = $props();
</script>

{#if notes.length > 0}
  <ul class="notes" aria-label="Notes and warnings">
    {#each notes as note (note.key)}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (notes with a where take focus so keyboard users get the same graph highlight as hover) -->
      <li
        class="note {note.kind}" class:active={activeNotes.includes(note.key)} data-note={note.key}
        tabindex={hasFocus(note) ? 0 : undefined}
        onmouseenter={() => setFocus(hasFocus(note) ? note.where ?? null : null)} onmouseleave={() => setFocus(null)}
        onfocus={() => setFocus(hasFocus(note) ? note.where ?? null : null)} onblur={() => setFocus(null)}
      >
        <span class="tag">{note.kind === "caveat" ? "Caveat" : "Note"}</span>
        {#if note.source}<span class="tag source" title="source of variation (spec §5.4)">{sourceText(note.source)}</span>{/if}
        <span class="note-text">{note.text}</span>
      </li>
    {/each}
  </ul>
{/if}
