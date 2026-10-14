# Simple / Nerd modes — design

Epic: `telemetry-nerd-2knd`. Related: `telemetry-nerd-1a8w` (short series labels).

## Goal

A toggle at the top of the workspace switches between **nerd** (everything, as today) and
**simple** (minimal panels, much less text, closer to a traditional graphing system). Simple mode
keeps the graph essentials: the question, the plot, rugs (missing data), annotations, views,
legend, notes/caveats, and a compact set of controls.

## Decisions (from brainstorm, 2026-10-14)

| Topic | Decision |
|---|---|
| Principles | **Same truth, less chrome.** Simple mode never drops a caveat or uncertainty that changes the reading (bands, rugs, scoped-claim notes). It hides controls, methodology text and secondary readouts only. |
| Audience | The same user for a quick glance, and users who prefer a simple UI. **Default mode is simple**, changeable in user settings. |
| Scope of state | Two layers. **Default mode** (`simple` unless changed) is a user setting persisted in `localStorage` (`tn-mode-default`). The header toggle is a **session override** (`sessionStorage`, `tn-mode`): effective mode = override ?? default. Modelled on the theme store. Per-panel **peek**: a simple panel can expand to nerd rendering without changing the global mode (session-only). |
| Beyond panels | Panels, groups, **Sidebar and HighlightStrip** get a slim form. Catalog and Proposals views unchanged. PDF export unchanged (always full). |
| Mechanism | Mode store + conditional rendering + extraction (approach B). Not CSS-only (hidden parts would still compute/fetch; no compact toolbar possible). Not a separate SimplePanel (would duplicate plot wiring and drift). |

## Panel anatomy in simple mode

| Part | Simple mode |
|---|---|
| Header: collapse, question, status, loading, refresh, close | Keep |
| Header: obj-id, pin | Hide |
| Header: PNG / PDF export | One overflow ("⋯") menu |
| Plot: series, bands, rug, annotations, crosshair, linked brush | Keep |
| Plot: `y ≠ 0` badge | Keep (it changes the reading) |
| Plot: marginal histogram, resize handle | Hide |
| Controls | Compact `SimpleToolbar` above the plot (below) |
| Legend | Keep, short labels (`1a8w`) |
| "Shown" block (what/where/step/provenance), Query `<details>` | Hide; one line `range · step` |
| Notes and caveats | Keep, compact (icon + short text, full on hover/focus, graph highlight preserved) |
| MetricCard | Hide |
| Threads | One-line collapsed summary, expandable |

### SimpleToolbar

Slim, Grafana-style, above the plot. Left: time-range presets (15m/1h/6h/24h/7d). Right: compact
dropdowns for **y-view**, **reference layers**, **series filter**. It reuses the handlers and
state that drive today's control rows; no logic is duplicated. Disabled states keep their
explanatory titles. Hidden in simple mode: reframe, follow-up, marginal reference picker.

## Architecture

- `ui/src/lib/mode.svelte.ts` — `ModeStore` like `ThemeStore`: `setting: "simple" | "nerd"`,
  `localStorage` `tn-mode-default` (default `simple`) plus `sessionStorage` `tn-mode` override, DOM guards for node-env vitest, sets
  `data-mode` on `<html>` (for CSS on trivially-hidden chrome and for e2e/screenshot hooks).
- `App.svelte` header-controls: the toggle (sets the session override).
- `SettingsMenu.svelte` (new; no settings surface exists today): a header popover with a "Default mode" select, the first user setting. Theme could move there later; out of scope.
- `Panel.svelte` (1180 lines) is refactored **first, with no behaviour change**: the control rows
  under the plot, and the shown/query/notes block, are extracted into focused components
  (`NerdControls`, `ShownBlock`, `NotesList`) with explicit props and handler callbacks. The
  refactor is valuable on its own: Panel shrinks and the simple/nerd branches become small
  `{#if}`s at component boundaries instead of 20 scattered conditionals.
- `SimpleToolbar.svelte` consumes the same handlers as `NerdControls`.
- Effective mode per panel = `peeked(panel) ? "nerd" : mode.setting`.
- Hidden parts are not rendered (`{#if}`), so they neither compute nor fetch (e.g. marginal
  reference fetch is skipped).

## Out of scope

Per-workspace or daemon-synced mode; PDF export following the mode; Catalog/Proposals changes;
changing what nerd mode shows; moving the theme select into settings.

## Testing

- Unit: mode store (default simple, override beats default, persistence, bad stored value, no-window), toolbar handler wiring,
  compact notes.
- UI (vitest): both modes per component.
- e2e (Playwright on the fixture PromQL source): toggle persists across reload; nerd unchanged;
  simple hides exactly the listed parts and keeps notes/rugs/views; peek works.
- Screenshots in light mode only, both modes.
- The refactor bead gates on the existing suites passing untouched.

## Work breakdown (children of `2knd`)

1. `.1` Mode store (default + session override) + header toggle + `data-mode`
   `.10` User settings menu with default-mode select — needs .1
2. `.2` Refactor: extract NerdControls / ShownBlock / NotesList from Panel (Opus)
3. `.3` Simple panel chrome (header, hidden blocks, range·step line) — needs .1, .2
4. `.4` SimpleToolbar — needs .2
5. `.5` Compact notes/caveats + collapsed threads — needs .2, `1a8w`
6. `.6` Per-panel nerd peek — needs .3
7. `.7` Slim Sidebar + HighlightStrip — needs .1
8. `.8` PanelGroup + non-time kinds (heatmap, fleet, spectrum, spectrogram, spc, littles,
   seasonal, histogram): per-kind inventory, then rendering — needs .3
9. `.9` e2e, screenshots, docs/glossary — needs .8

## Open items for planning

- Per-kind simple inventory (`.8`) needs its own short design pass once `.3` lands.
- Overflow menu component: reuse an existing menu if one exists (`SelectionMenu` is
  selection-specific); otherwise a minimal one.
