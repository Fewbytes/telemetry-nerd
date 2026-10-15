<script lang="ts">
  // the panel's notes and caveats; a note with a where drives the focus band on the plot (setFocus)
  import type { Where } from "../../lib/api";
  import type { Note } from "../../lib/panelNotes";
  import { sourceText } from "../../lib/sources";
  import { hasFocus } from "../../chart/focus";

  let { notes, activeNotes, setFocus, compact = false }: {
    notes: Note[]; activeNotes: string[]; setFocus: (w: Where | null) => void;
    /** simple mode: one ellipsised line per note, click opens it in place */
    compact?: boolean;
  } = $props();
  let openKeys = $state<string[]>([]);
  const toggle = (key: string) => {
    openKeys = openKeys.includes(key) ? openKeys.filter((k) => k !== key) : [...openKeys, key];
  };
  const focusOn = (note: Note) => setFocus(hasFocus(note) ? note.where ?? null : null);
  const kindLabel = (note: Note) => (note.kind === "caveat" ? "Caveat" : "Note");
</script>

{#if notes.length > 0}
  <ul class="notes" aria-label="Notes and warnings">
    {#each notes as note (note.key)}
      {#if compact}
        {@const open = openKeys.includes(note.key)}
        {@const src = note.source ? sourceText(note.source) : ""}
        <li
          class="note compact {note.kind}" class:active={activeNotes.includes(note.key)} class:open data-note={note.key}
          onmouseenter={() => focusOn(note)} onmouseleave={() => setFocus(null)}
        >
          <button
            type="button" class="note-row" aria-expanded={open} title={open ? src || undefined : src ? `${note.text} (${src})` : note.text}
            onclick={() => toggle(note.key)}
            onfocus={() => focusOn(note)} onblur={() => setFocus(null)}
          >
            <span class="note-icon" aria-hidden="true">{note.kind === "caveat" ? "⚠" : "ⓘ"}</span>
            <span class="sr-only">{kindLabel(note)}</span>
            <span class="note-text">{note.text}</span>
          </button>
          {#if open && src}<span class="note-source">{src}</span>{/if}
        </li>
      {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (notes with a where take focus so keyboard users get the same graph highlight as hover) -->
      <li
        class="note {note.kind}" class:active={activeNotes.includes(note.key)} data-note={note.key}
        tabindex={hasFocus(note) ? 0 : undefined}
        onmouseenter={() => focusOn(note)} onmouseleave={() => setFocus(null)}
        onfocus={() => focusOn(note)} onblur={() => setFocus(null)}
      >
        <span class="tag">{kindLabel(note)}</span>
        {#if note.source}<span class="tag source" title="source of variation (spec §5.4)">{sourceText(note.source)}</span>{/if}
        <span class="note-text">{note.text}</span>
      </li>
      {/if}
    {/each}
  </ul>
{/if}
