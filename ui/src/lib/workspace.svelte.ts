import {
  fetchWorkspace, fetchWorkspaces, subscribe,
  type Presence, type Snapshot, type WorkspaceEvent, type WorkspaceInfo,
} from "./api";
import type { DaemonState } from "./connection";
import { applyHighlightEvent, expire, nextExpiry, type Highlights } from "./highlights";
import { workspaceChanged } from "./workspaces";

const RELOAD_TYPES = new Set([
  "panel.created", "panel.answered", "finding.created", "finding.verdict",
  "annotation.created", "annotation.deleted", "hypothesis.created",
  "hypothesis.status_changed", "gap.created", "thread.message", "panel.closed", "panel.y_context", "panel.overlays_set", "panel.unit_refreshed",
  "panel.y_view_suggested", "panel.marginal_set", "code.started", "code.finished",
  "panel_group.created", "panel_group.updated", "panel_group.closed",
  // a socket opened just after a switch replays this but never saw the frame
  "workspace.opened",
]);

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15000;

// highlights are transient UI state folded from the stream; they never change the snapshot
export const needsReload = (e: WorkspaceEvent): boolean =>
  !e.type.startsWith("object.") && (e.klass !== "internal" || RELOAD_TYPES.has(e.type));

export function createWorkspace() {
  let snapshot = $state.raw<Snapshot | null>(null);
  let error = $state.raw<string | null>(null);
  let daemon = $state<DaemonState>("connecting");
  let presence = $state.raw<Presence | null>(null);
  let highlights = $state.raw<Highlights>(new Map());
  let catalogSeq = $state(0); // bumps when anything the metric card shows may have changed
  let expiryTimer: ReturnType<typeof setTimeout> | undefined;
  let workspaces = $state.raw<WorkspaceInfo[]>([]);
  let lastSeq = 0;
  // the workspace the latest frame named; null until one arrives and after the socket drops
  // (frames are not replayed, so after an outage only the snapshot knows what is active)
  let target: string | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0;

  const refreshList = () =>
    fetchWorkspaces().then((l) => { workspaces = l.workspaces; }).catch(() => { /* the list is a convenience; the next switch refetches */ });

  // a switch invalidates everything the board shows: highlights belong to the old workspace
  const clearHighlights = () => {
    highlights = new Map();
    armExpiry();
  };

  const load = (): Promise<void> =>
    fetchWorkspace()
      .then((s) => {
        const id = s.workspace.id;
        const shown = snapshot?.workspace.id;
        // Concurrent loads can return out of order; never apply a stale snapshot. last_seq is
        // global and the /ws stream forwards a switch's frame and then the new workspace's
        // events before this fetch lands, so the first snapshot of the frame's workspace wins
        // even when those events raised lastSeq past it. A load that raced a later frame (a
        // switch and a switch back) is for a workspace no longer active: dropped.
        const fresh = target === null
          ? s.last_seq >= lastSeq
          : id === target && (s.last_seq >= lastSeq || shown !== target);
        if (fresh) {
          const first = snapshot === null;
          const switched = workspaceChanged(snapshot, s);
          snapshot = s;
          error = null;
          if (switched) clearHighlights();
          if (switched || first) refreshList();
        } else if (target === null && shown !== undefined && id !== shown) {
          // no frame to go by (e.g. the resync after an outage): the daemon switched, retry
          schedule();
        }
        lastSeq = Math.max(lastSeq, s.last_seq);
        failures = 0;
      })
      .catch((e) => {
        error = String(e);
        // the daemon may be restarting: keep retrying with backoff until a snapshot lands
        clearTimeout(retryTimer);
        retryTimer = setTimeout(load, Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** failures++));
      });

  const armExpiry = () => {
    clearTimeout(expiryTimer);
    const at = nextExpiry(highlights);
    if (at === null) return;
    expiryTimer = setTimeout(() => {
      highlights = expire(highlights, Date.now());
      armExpiry();
    }, Math.max(0, at - Date.now()));
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(load, 100);
  };

  return {
    get snapshot() { return snapshot; },
    get error() { return error; },
    /** UI socket to the daemon */
    get daemon() { return daemon; },
    /** latest presence frame; null until known and while the daemon is unreachable */
    get presence() { return presence; },
    /** a counter that changes whenever catalog claims, relations or bindings change */
    get catalogSeq() { return catalogSeq; },
    /** active highlights (Claude's and the user's), expired ones already dropped */
    get highlights() { return highlights; },
    /** non-archived workspaces, as last fetched */
    get workspaces() { return workspaces; },
    refreshWorkspaces: refreshList,
    reload: load,
    start(): () => void {
      let stopUnsub = () => {};
      let stopped = false;
      load().then(() => {
        if (stopped) return;
        let dropped = false;
        stopUnsub = subscribe((e) => {
          lastSeq = Math.max(lastSeq, e.seq);
          if (/^(catalog|relation|binding)\./.test(e.type)) catalogSeq++;
          const next = applyHighlightEvent(highlights, e, Date.now());
          if (next !== highlights) {
            highlights = next;
            armExpiry();
          }
          if (needsReload(e)) schedule();
        }, () => lastSeq, {
          onPresence: (p) => (presence = p),
          // a rename/archive changes only the list and the shown title; the board is untouched.
          // On a switch, the reloaded snapshot refreshes the list once it lands.
          onWorkspace: (f) => {
            target = f.active.id;
            if (snapshot !== null && f.active.id === snapshot.workspace.id) {
              snapshot = { ...snapshot, workspace: f.active };
              refreshList();
            } else {
              clearHighlights();
              load();
            }
          },
          onOpen: () => {
            daemon = "connected";
            // resync after an outage: the daemon may have restarted with other state
            if (dropped) schedule();
          },
          onClose: () => {
            dropped = true;
            target = null;
            daemon = "reconnecting";
            presence = null;
          },
        });
      });
      return () => { stopped = true; clearTimeout(timer); clearTimeout(retryTimer); clearTimeout(expiryTimer); stopUnsub(); };
    },
  };
}
