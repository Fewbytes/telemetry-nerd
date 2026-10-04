<script lang="ts">
  // A model view (bead czt.3): the panels of one USE / RED / Little's law binding, stacked on one
  // time axis with a linked crosshair and selection; missing roles are gap cards.
  import { setContext } from "svelte";
  import PanelView from "../Panel.svelte";
  import { closeGroup, reframeGroup, type Annotation, type Panel, type PanelGroup, type Thread } from "../lib/api";
  import { basisText, FORM_LABELS, groupDomain, KIND_LABELS, orderedRoles, roleName, roleTitle, verdictBadge } from "../lib/groups";
  import { GroupLink } from "../lib/groupLink.svelte";
  import { fmtStep } from "../lib/format";

  let { group, members, annotations = [], threads = [] }: {
    group: PanelGroup; members: Record<string, Panel>; annotations?: Annotation[]; threads?: Thread[];
  } = $props();

  // one link per group, created once: the group's window never changes (a reframe is a new group)
  // svelte-ignore state_referenced_locally
  const link = new GroupLink(group.id, groupDomain(group));
  setContext("panelGroup", link);

  const roles = $derived(orderedRoles(group, members));
  const byRole = $derived(new Map(group.roles.map((r) => [r.role, r])));
  const fmtTime = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace("T", " ") + "Z";
  let error = $state<string | null>(null);
  let busy = $state(false);

  const close = () => closeGroup(group.id).catch((e) => (error = String(e)));
  const zoom = () => {
    const b = link.brush;
    if (!b) return;
    busy = true;
    reframeGroup(group.id, Math.round(b.x0Ms), Math.round(b.x1Ms))
      .then(() => link.clear())
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };
</script>

<section class="panel-group" id="group-{group.id}" data-group-id={group.id} data-group-kind={group.kind}
  aria-label="{KIND_LABELS[group.kind] ?? group.kind} view of {group.key}">
  <header class="group-head">
    <a class="obj-id" href="#/group/{group.id}" title="Panel group {group.id}">{group.id}</a>
    <span class="kind" data-group-kind-badge>{KIND_LABELS[group.kind] ?? group.kind}</span>
    <span class="key" data-group-key>{group.key}</span>
    <span class="roles-list">{roles.map(roleTitle).join(" · ")}</span>
    {#if link.brush}
      <button type="button" class="zoom" data-group-zoom disabled={busy}
        title="Show every role of this group over the selected time range (a new group; this one stays)"
        onclick={zoom}>show group over selection</button>
      <button type="button" class="zoom" onclick={() => link.clear()} aria-label="Clear the linked selection">clear</button>
    {/if}
    <button class="close" type="button" aria-label="Close group" title="Close the group and all its panels" onclick={close}>×</button>
  </header>
  <p class="group-meta">
    {basisText(group)} · {fmtTime(group.start_ms)} – {fmtTime(group.end_ms)} · step {fmtStep(group.step_ms)}
    · one time axis; hover or drag on any panel to see it on all
    {#if group.join_on.length} · members by {group.join_on.join(", ")}{/if}
    {#if Object.keys(group.matchers).length} · {Object.entries(group.matchers).map(([k, v]) => `${k}="${v}"`).join(", ")}{/if}
    {#if group.reframed_from} · reframed from {group.reframed_from}{/if}
  </p>
  {#if group.verdict}
    <p class="group-verdict" data-group-verdict data-first-mover={group.verdict.first ?? ""}>
      {group.verdict.text}
      <span class="verdict-basis">vs {group.verdict.reference} · family-wise α {group.verdict.alpha}</span>
    </p>
  {/if}
  {#if error}<div class="error">{error}</div>{/if}
  {#each roles as role (role)}
    {@const r = byRole.get(role)}
    {@const p = members[role]}
    {@const badge = verdictBadge(r?.verdict)}
    <div class="role" data-role={role} data-role-view={r?.view ?? (p ? "panel" : "pending")}>
      <div class="role-label">
        <span class="role-name">{roleTitle(role)}</span>
        {#if r?.metric}<code>{r.metric}</code>{/if}
        {#if r?.form}<span class="form">{FORM_LABELS[r.form] ?? r.form}</span>{/if}
        {#if r?.members}<span class="members">{r.members} member{r.members === 1 ? "" : "s"}{r.view === "fleet" ? " · fleet" : ""}</span>{/if}
        {#if badge}
          <span class="verdict {badge.tone}" data-role-verdict={r?.verdict?.status} title={badge.title}>{badge.label}</span>
        {/if}
      </div>
      {#if r?.notes?.length}
        <ul class="role-notes">{#each r.notes as n (n)}<li>{n}</li>{/each}</ul>
      {/if}
      {#if p && !p.closed}
        <PanelView panel={p} {annotations} {threads} />
      {:else if r?.view === "gap"}
        <div class="gap-card" data-gap-card={role}>
          <div class="gap-title">No {roleName(role)} signal for {group.key}</div>
          {#if r.suggestion}
            <div>Instrument <code>{r.suggestion.name}</code> ({r.suggestion.type}{r.suggestion.labels.length ? `, labels ${r.suggestion.labels.join(", ")}` : ""}){r.why ? `: ${r.why}` : ""}.</div>
          {/if}
          {#if r.gap}<div class="gap-ref">gap <a class="obj-id" href="#gap-{r.gap}">{r.gap}</a></div>{/if}
        </div>
      {:else if r?.view === "error"}
        <div class="gap-card error-card" data-role-error={role}>
          <div class="gap-title">Could not draw {roleName(role)}</div>
          <div>{r.error}</div>
        </div>
      {:else if p?.closed}
        <div class="gap-card closed-card">{roleName(role)}: panel {p.id} closed</div>
      {:else}
        <div class="gap-card pending-card">{roleName(role)}: drawing…</div>
      {/if}
    </div>
  {/each}
</section>

<style>
  .panel-group { border: 1px solid var(--border); border-left: 4px solid var(--ann-claude); border-radius: 8px; padding: 10px 12px 2px; margin-bottom: 16px; }
  .group-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-weight: 600; }
  .kind { font-size: 12px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--ann-claude); color: var(--fg); }
  .key { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .roles-list { flex: 1; font-weight: 400; color: var(--muted); font-size: 13px; }
  .group-meta { margin: 4px 0 8px; font-size: 12px; color: var(--muted); }
  .close, .zoom { border: 1px solid var(--border); background: var(--bg); color: var(--fg); border-radius: 4px; cursor: pointer; font: inherit; font-size: 12px; padding: 1px 8px; }
  .close { font-size: 16px; line-height: 1; }
  .role { margin-bottom: 4px; }
  .role-label { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-size: 13px; margin: 2px 0 4px; }
  .role-name { font-weight: 700; }
  .role-name::first-letter { text-transform: uppercase; }
  .role-label code { font-size: 12px; color: var(--muted); }
  .form, .members { font-size: 12px; color: var(--muted); }
  .form::before, .members::before { content: "· "; }
  .role-notes { margin: 0 0 4px; padding-left: 16px; font-size: 12px; color: var(--muted); }
  .gap-card { border: 1px dashed var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; font-size: 13px; background: var(--badge); color: var(--fg); }
  .gap-title { font-weight: 600; margin-bottom: 4px; }
  .error-card { border-color: var(--error); }
  .pending-card, .closed-card { color: var(--muted); background: transparent; }
  .gap-ref { margin-top: 4px; font-size: 12px; color: var(--muted); }
  .error { color: var(--error); font-size: 12px; }
  .group-verdict { margin: 0 0 8px; font-size: 13px; color: var(--fg); }
  .verdict-basis { font-size: 12px; color: var(--muted); }
  .verdict-basis::before { content: "· "; }
  .verdict { font-size: 12px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--border); color: var(--muted); }
  .verdict.moved { border-color: var(--ann-claude); color: var(--fg); font-weight: 600; }
</style>
