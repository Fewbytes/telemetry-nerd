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
  let timer: ReturnType<typeof setTimeout> | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0;

  const refreshList = () =>
    fetchWorkspaces().then((l) => { workspaces = l.workspaces; }).catch(() => { /* the list is a convenience; the next switch refetches */ });

  // a switch invalidates everything the board shows: highlights belong to the old workspace
  const onSwitched = (reload: boolean) => {
    highlights = new Map();
    armExpiry();
    refreshList();
    if (reload) load();
  };

  const load = (): Promise<void> =>
    fetchWorkspace()
      .then((s) => {
        // concurrent loads can return out of order; never apply a stale snapshot
        if (s.last_seq >= lastSeq) {
          const switched = workspaceChanged(snapshot, s);
          snapshot = s;
          error = null;
          if (switched) onSwitched(false);
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
        refreshList();
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
          // a rename/archive of another workspace changes only the list; the board is untouched
          onWorkspace: (f) => (f.active.id === snapshot?.workspace.id ? refreshList() : onSwitched(true)),
          onOpen: () => {
            daemon = "connected";
            // resync after an outage: the daemon may have restarted with other state
            if (dropped) schedule();
          },
          onClose: () => {
            dropped = true;
            daemon = "reconnecting";
            presence = null;
          },
        });
      });
      return () => { stopped = true; clearTimeout(timer); clearTimeout(retryTimer); clearTimeout(expiryTimer); stopUnsub(); };
    },
  };
}
