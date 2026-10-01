import { fetchWorkspace, subscribe, type Presence, type Snapshot, type WorkspaceEvent } from "./api";
import type { DaemonState } from "./connection";

const RELOAD_TYPES = new Set([
  "panel.created", "panel.answered", "finding.created", "finding.verdict",
  "annotation.created", "annotation.deleted", "hypothesis.created",
  "hypothesis.status_changed", "gap.created", "thread.message", "panel.closed",
]);

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 15000;

export const needsReload = (e: WorkspaceEvent): boolean =>
  e.klass !== "internal" || RELOAD_TYPES.has(e.type);

export function createWorkspace() {
  let snapshot = $state.raw<Snapshot | null>(null);
  let error = $state.raw<string | null>(null);
  let daemon = $state<DaemonState>("connecting");
  let presence = $state.raw<Presence | null>(null);
  let lastSeq = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0;

  const load = (): Promise<void> =>
    fetchWorkspace()
      .then((s) => {
        // concurrent loads can return out of order; never apply a stale snapshot
        if (s.last_seq >= lastSeq) {
          snapshot = s;
          error = null;
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
    reload: load,
    start(): () => void {
      let stopUnsub = () => {};
      let stopped = false;
      load().then(() => {
        if (stopped) return;
        let dropped = false;
        stopUnsub = subscribe((e) => {
          lastSeq = Math.max(lastSeq, e.seq);
          if (needsReload(e)) schedule();
        }, () => lastSeq, {
          onPresence: (p) => (presence = p),
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
      return () => { stopped = true; clearTimeout(timer); clearTimeout(retryTimer); stopUnsub(); };
    },
  };
}
