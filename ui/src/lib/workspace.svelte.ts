import { fetchWorkspace, subscribe, type Snapshot, type WorkspaceEvent } from "./api";

const RELOAD_TYPES = new Set([
  "panel.created", "panel.answered", "finding.created", "finding.verdict",
  "annotation.created", "annotation.deleted", "hypothesis.created",
  "hypothesis.status_changed", "gap.created", "thread.message", "panel.closed",
]);

export const needsReload = (e: WorkspaceEvent): boolean =>
  e.klass !== "internal" || RELOAD_TYPES.has(e.type);

export function createWorkspace() {
  let snapshot = $state.raw<Snapshot | null>(null);
  let error = $state.raw<string | null>(null);
  let lastSeq = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const load = () =>
    fetchWorkspace()
      .then((s) => {
        // concurrent loads can return out of order; never apply a stale snapshot
        if (s.last_seq >= lastSeq) {
          snapshot = s;
          error = null;
        }
        lastSeq = Math.max(lastSeq, s.last_seq);
      })
      .catch((e) => (error = String(e)));

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(load, 100);
  };

  return {
    get snapshot() { return snapshot; },
    get error() { return error; },
    reload: load,
    start(): () => void {
      let stopUnsub = () => {};
      let stopped = false;
      load().then(() => {
        if (stopped) return;
        stopUnsub = subscribe((e) => {
          lastSeq = Math.max(lastSeq, e.seq);
          if (needsReload(e)) schedule();
        }, () => lastSeq);
      });
      return () => { stopped = true; clearTimeout(timer); stopUnsub(); };
    },
  };
}
