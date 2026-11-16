# Charts workspace: ad-hoc graphs apart from investigations

Card telemetry-nerd-oqj3. "Show me a graph of X" is not an investigation, yet every panel lands
in the investigation UI (hypotheses, findings, gaps, chat sidebar). This adds one minimal
Charts page. Principles ([../../principles.md](../../principles.md)): 5 every graph answers an
explicit question, 7 the workspace is the single source of truth, 18 same truth, less chrome.

## Decisions (user, 2026-11-16)

- `workspaces.kind` is `investigation` (default; every existing workspace) or `charts`.
- One persistent Charts workspace, created on first ad-hoc use, titled "Charts".
- Ad-hoc covers `plot_data`, `plot_csv` and source metrics (`show`).

## Tool argument: `target`

`show`, `plot_data` and `plot_csv` take `target: "charts" | None` (default None).

- None: the panel goes to the active workspace, as before.
- `"charts"`: the panel is drawn in the Charts workspace (created if absent) while the active
  workspace stays as it is. The whole call runs pinned to the Charts workspace
  (`ActiveWorkspace.using`), so panel, events and the y-context lookup all land there.
- The reply's `url` is `<ui>/#/charts/<panel>`. The UI opens the Charts workspace when that
  hash is visited (the user's own navigation, so switching there is theirs to do).
- Creating the Charts workspace never makes it the active one (`opened_at_ms = 0`).

## Charts workspace rules

- Panels only: the UI hides the sidebar and the Catalog/Proposals nav. Each panel keeps its
  question, description, caveats, notes, coverage, provenance and uncertainty (principle 18:
  same truth; simple mode default, nerd mode available).
- Newest panel first (the panel list order), panels closable.
- `hypothesis_create`, `finding_create` and `gap_create` refuse in a charts workspace with a
  hint to promote the panel. The Charts workspace cannot be archived.
- `reply` threads stay on panels; only the sidebar is hidden.
- Workspace menu: Charts is listed with the others, under a "Charts" label, and the header
  has a "Charts" link.

## Promote

`workspace_promote(panel)` (MCP) / `POST /api/panels/{id}/promote` (UI "Investigate this")
opens a new investigation workspace and switches to it. The new workspace's question is the
panel's question; the panel is copied in (same spec and datasets, new id); the original stays on
Charts. Source-metric panels carry their dataset, so they stay re-queryable. Plot-mode panels
carry over as illustrations; that they can never be cited as evidence is enforced by
telemetry-nerd-wjbg, not here. Only panels of a charts workspace can be promoted.
