<script lang="ts">
  // Read-only view of a tier-2 code node (spec §5.2): the code, how the run ended, what went in and
  // came out, and a re-run (a NEW node: finished nodes are immutable). No editor on purpose.
  import { fetchCode, rerunCode, type CodeBrief, type CodeNode, type Panel } from "../lib/api";
  import {
    boundText, type BoundedText, durationText, highlightPython, inputLinks, outputLinks, statusView,
  } from "../lib/codeView";

  let { id, runs, panels, onclose, onopen }: {
    id: string; runs: CodeBrief[]; panels: Panel[]; onclose: () => void; onopen: (id: string) => void;
  } = $props();

  let node = $state.raw<CodeNode | null>(null);
  let loadError = $state<string | null>(null);
  let busy = $state(false); // a re-run started from this view is in flight
  let rerunResult = $state.raw<CodeNode | null>(null);
  let rerunError = $state<string | null>(null);
  let dialog: HTMLDialogElement | undefined = $state();

  const brief = $derived(runs.find((r) => r.id === id));
  // refetch when the snapshot says this node's state moved (running -> finished)
  const stamp = $derived(`${id}:${brief?.status ?? ""}:${brief?.finished_at_ms ?? ""}`);

  $effect(() => {
    void stamp;
    const want = id;
    loadError = null;
    fetchCode(want)
      .then((n) => { if (want === id) node = n; })
      .catch((e) => { if (want === id) { node = null; loadError = String(e); } });
  });
  $effect(() => {
    if (dialog && !dialog.open) dialog.showModal();
  });
  $effect(() => { // another node: forget the previous re-run's result
    void id;
    rerunResult = null;
    rerunError = null;
  });

  const running = $derived(busy || (node?.status ?? brief?.status) === "running");
  const status = $derived(node ? statusView(node) : null);
  const tokens = $derived(node ? highlightPython(node.code) : []);
  const out = $derived(node && node.stdout ? boundText(node.stdout) : null);
  const err = $derived(node && node.stderr ? boundText(node.stderr) : null);
  const trace = $derived(node?.traceback ? boundText(node.traceback, 60, 8000) : null);
  const ins = $derived(node ? inputLinks(node, panels) : []);
  const outs = $derived(node ? outputLinks(node.outputs.map((o) => o.dataset), panels) : []);

  async function rerun() {
    if (!node || running) return;
    busy = true; rerunError = null; rerunResult = null;
    try {
      rerunResult = await rerunCode(node.id);
    } catch (e) {
      rerunError = String(e);
    } finally {
      busy = false;
    }
  }
  const panelHref = (p: string) => `#/panel/${p}`;
</script>

{#snippet stream(label: string, b: BoundedText, extra: Record<string, string> = {})}
  {#if b.hiddenLines}<p class="muted">… {b.hiddenLines} earlier lines hidden</p>{/if}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (scrollable region: keyboard users must be able to scroll it) -->
  <pre class="stream" tabindex="0" aria-label={label} {...extra}>{b.text}</pre>
{/snippet}

<dialog bind:this={dialog} class="code-view" aria-labelledby="code-view-title" onclose={onclose}
  onclick={(e) => { if (e.target === dialog) dialog?.close(); }}>
  <div class="cv-body" data-code-view={id}>
    <header>
      <h2 id="code-view-title"><span class="obj-id">{id}</span> code run</h2>
      <button type="button" class="close" onclick={() => dialog?.close()} aria-label="Close code view">✕</button>
    </header>

    {#if loadError}
      <p class="cv-error" role="alert">Could not load {id}: {loadError}</p>
    {:else if !node}
      <p class="muted">loading…</p>
    {:else}
      <p class="meta">
        <span class="status {status?.tone}" data-code-status>{status?.glyph} {status?.text}</span>
        {#if node.duration_s != null}<span>· {durationText(node.duration_s)}</span>{/if}
        <span>· by {node.author}</span>
        {#if node.timeout_s}<span>· timeout {node.timeout_s} s</span>{/if}
        {#if node.rerun_of}
          <span>· re-run of <button type="button" class="ref-chip obj-id" onclick={() => onopen(node!.rerun_of!)}>{node.rerun_of}</button></span>
        {/if}
      </p>
      {#if node.restarted}
        <p class="note warn" data-code-restarted>Kernel restarted: variables and imports from earlier runs are gone. Datasets are safe.</p>
      {/if}

      <h3>Code</h3>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (scrollable region: keyboard users must be able to scroll it) -->
      <pre class="src" tabindex="0" aria-label="Code of {id}"><code>{#each tokens as t}{#if t.kind === "plain"}{t.text}{:else}<span class="tok-{t.kind}">{t.text}</span>{/if}{/each}</code></pre>

      <h3>Inputs</h3>
      {#if ins.length === 0}
        <p class="muted">none declared</p>
      {:else}
        <ul class="links">
          {#each ins as l (l.dataset)}
            <li><span class="obj-id">{l.dataset}</span>
              {#if l.panel}<a href={panelHref(l.panel)} onclick={() => dialog?.close()}>shown in {l.panel}</a>{:else}<span class="muted">not on a panel</span>{/if}</li>
          {/each}
        </ul>
      {/if}

      <h3>Outputs</h3>
      {#if node.outputs.length === 0}
        <p class="muted">none{node.status === "failed" ? " (the run failed)" : ""}</p>
      {:else}
        <ul class="links">
          {#each node.outputs as o, i (o.dataset)}
            <li><span class="obj-id">{o.dataset}</span> {o.name} · {o.representation}, {o.rows} rows
              {#if outs[i]?.panel}<a href={panelHref(outs[i].panel!)} onclick={() => dialog?.close()}>shown in {outs[i].panel}</a>{/if}
              {#if !o.evidence_ok}<span class="badge" title="not usable as finding evidence">not evidence{o.caveats.length ? `: ${o.caveats.join(", ")}` : ""}</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
      {#if node.issues.length}
        <ul class="notes">
          {#each node.issues as i}<li class="note caveat">{i.name}: {i.code} — {i.message}</li>{/each}
        </ul>
      {/if}

      {#if node.status === "failed"}
        <h3>Error</h3>
        <p class="cv-error" data-code-error>{node.error}</p>
        {#if trace}{@render stream("Traceback", trace, { class: "stream trace", "data-code-traceback": "" })}{/if}
      {/if}
      {#if out}
        <h3>stdout{node.truncated ? " (truncated by the kernel)" : ""}</h3>
        {@render stream("stdout", out, { "data-code-stdout": "" })}
      {/if}
      {#if err}
        <h3>stderr</h3>
        {@render stream("stderr", err)}
      {/if}
      {#if node.result}
        <h3>Result</h3>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (scrollable region: keyboard users must be able to scroll it) -->
        <pre class="stream" tabindex="0" aria-label="Result">{boundText(node.result, 20, 2000).text}</pre>
      {/if}

      <footer>
        <button type="button" class="rerun" disabled={running} onclick={rerun} data-code-rerun>
          {running ? "running…" : "Re-run"}
        </button>
        <span class="muted">runs the same code on the same inputs as a new node</span>
      </footer>
      <div aria-live="polite">
        {#if rerunResult}
          {@const r = statusView(rerunResult)}
          <p class="rerun-result" data-code-rerun-result>
            Re-run finished as <button type="button" class="ref-chip obj-id" onclick={() => onopen(rerunResult!.id)}>{rerunResult.id}</button>:
            {r.glyph} {r.text}{rerunResult.duration_s != null ? `, ${durationText(rerunResult.duration_s)}` : ""}
          </p>
        {/if}
        {#if rerunError}<p class="cv-error" role="alert">Re-run failed: {rerunError}</p>{/if}
      </div>
    {/if}
  </div>
</dialog>

<style>
  dialog.code-view {
    width: min(860px, 94vw); max-height: 88vh; padding: 0; border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg); color: var(--fg);
  }
  dialog.code-view::backdrop { background: rgb(0 0 0 / 0.55); }
  .cv-body { padding: 12px 16px 16px; font-size: 13px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  h2 { margin: 0; font-size: 16px; }
  h3 { margin: 14px 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .close { border: 1px solid var(--border); background: var(--bg); color: var(--fg); border-radius: 4px; cursor: pointer; padding: 2px 8px; }
  .meta { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; margin: 8px 0; }
  .status { font-weight: 600; }
  .status.ok { color: var(--ok); }
  .status.failed { color: var(--error); }
  .status.running { color: var(--warn); }
  .muted { color: var(--muted); }
  pre { margin: 0; padding: 8px 10px; background: var(--code-bg); border: 1px solid var(--border); border-radius: 4px; overflow: auto; max-height: 340px; font-size: 12px; line-height: 1.45; tab-size: 4; }
  pre.trace { border-left: 3px solid var(--error); }
  .links { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
  .links a { color: var(--fg); }
  .cv-error { color: var(--error); margin: 4px 0; font-weight: 600; word-break: break-word; }
  .note { margin: 6px 0; padding: 4px 8px; border-left: 3px solid var(--warn); background: var(--badge); border-radius: 0 4px 4px 0; }
  ul.notes { list-style: none; padding: 0; margin: 6px 0; }
  footer { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
  .rerun { cursor: pointer; padding: 4px 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--fg); font: inherit; }
  .rerun:disabled { cursor: not-allowed; opacity: 0.6; }
  .ref-chip { cursor: pointer; }
  .rerun-result { margin: 8px 0 0; font-weight: 600; }
  .src code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .src { white-space: pre; }
</style>
