import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchWorkspace, fetchWorkspaces, subscribe, type Snapshot, type WorkspaceEvent, type WorkspaceInfo } from "./api";
import { createWorkspace, needsReload } from "./workspace.svelte";

vi.mock("./api", async (orig) => ({ ...(await orig<typeof import("./api")>()), fetchWorkspace: vi.fn(), fetchWorkspaces: vi.fn() }));

const ev = (type: string, klass: WorkspaceEvent["klass"]): WorkspaceEvent => ({
  seq: 1, ts_ms: 0, actor: "claude", type, object_id: null, klass, payload: {}, workspace: "w1",
});

describe("needsReload", () => {
  it("reloads for any non-internal event", () => {
    expect(needsReload(ev("whatever", "ambient"))).toBe(true);
    expect(needsReload(ev("whatever", "intentional"))).toBe(true);
  });
  it("ignores unknown internal events", () => {
    expect(needsReload(ev("cache.warm", "internal"))).toBe(false);
  });
  it("never reloads for highlight events, whatever their class", () => {
    expect(needsReload(ev("object.highlighted", "intentional"))).toBe(false);
    expect(needsReload(ev("object.unhighlighted", "ambient"))).toBe(false);
  });
  it("reloads for listed types even when internal", () => {
    for (const t of ["panel.created", "panel.answered", "finding.created", "finding.verdict",
      "annotation.created", "annotation.deleted", "hypothesis.created",
      "hypothesis.status_changed", "gap.created", "thread.message", "panel.closed", "panel.y_context", "panel.overlays_set", "panel.unit_refreshed",
      "workspace.opened"]) {
      expect(needsReload(ev(t, "internal"))).toBe(true);
    }
  });
});

describe("subscribe", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("builds /ws?since=<lastSeq> and re-reads since on reconnect", () => {
    const urls: string[] = [];
    const sockets: FakeWS[] = [];
    class FakeWS {
      onmessage: ((m: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor(url: string) {
        urls.push(url);
        sockets.push(this);
      }
      close() {}
    }
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWS);
    vi.stubGlobal("location", { protocol: "http:", host: "h:1" });
    let seq = 12;
    const stop = subscribe(() => {}, () => seq);
    expect(urls[0]).toBe("ws://h:1/ws?since=12");
    seq = 20;
    // reconnect path: onclose schedules a new connect() which re-reads sinceRef
    sockets[0].onclose!();
    vi.advanceTimersByTime(1000);
    expect(urls[1]).toBe("ws://h:1/ws?since=20");
    stop();
    vi.useRealTimers();
  });
});

describe("subscribe frame routing", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("routes presence frames apart from events and reports open/close", () => {
    const sockets: FakeWS[] = [];
    class FakeWS {
      onopen: (() => void) | null = null;
      onmessage: ((m: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() { sockets.push(this); }
      close() {}
    }
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWS);
    vi.stubGlobal("location", { protocol: "http:", host: "h:1" });
    const events: unknown[] = [];
    const presences: unknown[] = [];
    const states: string[] = [];
    const stop = subscribe((e) => events.push(e), () => 0, {
      onPresence: (p) => presences.push(p),
      onOpen: () => states.push("open"),
      onClose: () => states.push("close"),
    });
    sockets[0].onopen!();
    sockets[0].onmessage!({ data: JSON.stringify({ kind: "presence", status: "live" }) });
    sockets[0].onmessage!({ data: JSON.stringify({ seq: 3, type: "panel.created" }) });
    expect(presences).toEqual([{ kind: "presence", status: "live" }]);
    expect(events).toEqual([{ seq: 3, type: "panel.created" }]);
    sockets[0].onclose!();
    expect(states).toEqual(["open", "close"]);
    stop();
    vi.useRealTimers();
  });
  it("routes seq-less workspace control frames apart from events", () => {
    const sockets: FakeWS[] = [];
    class FakeWS {
      onmessage: ((m: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() { sockets.push(this); }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeWS);
    vi.stubGlobal("location", { protocol: "http:", host: "h:1" });
    const events: unknown[] = [];
    const switches: unknown[] = [];
    const frame = { kind: "workspace", active: { id: "w2", title: "t" } };
    const stop = subscribe((e) => events.push(e), () => 0, { onWorkspace: (w) => switches.push(w) });
    sockets[0].onmessage!({ data: JSON.stringify(frame) });
    expect(events).toEqual([]);
    expect(switches).toEqual([frame]);
    stop();
    // without a handler the frame is dropped, never mistaken for an event
    const bare = subscribe((e) => events.push(e));
    sockets[1].onmessage!({ data: JSON.stringify(frame) });
    expect(events).toEqual([]);
    bare();
  });
});

describe("snapshot load retry", () => {
  afterEach(() => vi.useRealTimers());
  it("retries with backoff after a failed load and recovers", async () => {
    vi.useFakeTimers();
    const load = vi.mocked(fetchWorkspace);
    load.mockReset();
    load.mockRejectedValueOnce(new Error("down")).mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue({ last_seq: 3, workspace: { id: "w1" } } as Snapshot);
    vi.mocked(fetchWorkspaces).mockResolvedValue({ active: "w1", workspaces: [], more: 0 });
    const ws = createWorkspace();
    await ws.reload();
    expect(ws.error).toContain("down");
    await vi.advanceTimersByTimeAsync(1000); // 1st retry fails, next backoff doubles
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1999);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(3);
    expect(ws.error).toBeNull();
    expect(ws.snapshot?.last_seq).toBe(3);
    await vi.advanceTimersByTimeAsync(60000); // success stops the retries
    expect(load).toHaveBeenCalledTimes(3);
  });
});

describe("workspace frames in the store", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  const snap = (id: string, last_seq: number) => ({ last_seq, workspace: { id } }) as unknown as Snapshot;
  const frame = (id: string) => JSON.stringify({ kind: "workspace", active: { id, title: "t" } });
  const highlight = JSON.stringify({
    seq: 5, ts_ms: 0, actor: "claude", type: "object.highlighted", object_id: "p1",
    klass: "ambient", payload: {}, workspace: "w1",
  });

  async function started(first: Snapshot) {
    const sockets: { onmessage: ((m: { data: string }) => void) | null }[] = [];
    class FakeWS {
      onopen = null; onmessage: ((m: { data: string }) => void) | null = null; onclose = null;
      constructor() { sockets.push(this); }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeWS);
    vi.stubGlobal("location", { protocol: "http:", host: "h:1" });
    const load = vi.mocked(fetchWorkspace);
    load.mockReset();
    load.mockResolvedValue(first);
    vi.mocked(fetchWorkspaces).mockReset();
    vi.mocked(fetchWorkspaces).mockResolvedValue({ active: first.workspace.id, workspaces: [], more: 0 });
    const ws = createWorkspace();
    const stop = ws.start();
    await vi.waitFor(() => expect(sockets.length).toBe(1));
    sockets[0].onmessage!({ data: highlight });
    expect(ws.highlights.size).toBe(1);
    return { ws, load, stop, send: (d: string) => sockets[0].onmessage!({ data: d }) };
  }

  it("a frame for a different workspace clears highlights and reloads the snapshot once", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    load.mockResolvedValue(snap("w2", 6));
    send(frame("w2"));
    expect(ws.highlights.size).toBe(0);
    await vi.waitFor(() => expect(ws.snapshot?.workspace.id).toBe("w2"));
    expect(load).toHaveBeenCalledTimes(2); // initial load + one reload
    stop();
  });

  it("a frame for the shown workspace refreshes the list only", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    const lists = vi.mocked(fetchWorkspaces);
    const before = lists.mock.calls.length;
    send(frame("w1"));
    await vi.waitFor(() => expect(lists.mock.calls.length).toBe(before + 1));
    expect(ws.highlights.size).toBe(1);
    expect(load).toHaveBeenCalledTimes(1);
    stop();
  });

  it("drops a stale snapshot that arrives after a newer one", async () => {
    vi.useFakeTimers(); // the dropped snapshot arms a resync timer; it must not outlive the test (afterEach restores real timers)
    vi.mocked(fetchWorkspace).mockReset();
    vi.mocked(fetchWorkspaces).mockReset();
    vi.mocked(fetchWorkspaces).mockResolvedValue({ active: "w2", workspaces: [], more: 0 });
    const resolvers: ((s: Snapshot) => void)[] = [];
    vi.mocked(fetchWorkspace).mockImplementation(() => new Promise((r) => resolvers.push(r)));
    const ws = createWorkspace();
    const a = ws.reload();
    const b = ws.reload();
    resolvers[1](snap("w2", 9));
    await b;
    resolvers[0](snap("w1", 3));
    await a;
    expect(ws.snapshot?.workspace.id).toBe("w2");
    expect(ws.snapshot?.last_seq).toBe(9);
  });

  // an event that never triggers a reload, so only the frame's own load can switch the board
  const quiet = (seq: number, workspace: string) => JSON.stringify({
    seq, ts_ms: 0, actor: "claude", type: "cache.warm", object_id: null, klass: "internal", payload: {}, workspace,
  });
  const pending = (load: ReturnType<typeof vi.mocked<typeof fetchWorkspace>>) => {
    const resolvers: ((s: Snapshot) => void)[] = [];
    load.mockImplementation(() => new Promise((r) => resolvers.push(r)));
    return resolvers;
  };

  it("switches on the frame's snapshot even after a newer event raised lastSeq", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    const resolvers = pending(load);
    send(frame("w2"));
    send(quiet(8, "w2")); // forwarded before the switch-triggered snapshot (last_seq 7) lands
    resolvers[0](snap("w2", 7));
    await vi.waitFor(() => expect(ws.snapshot?.workspace.id).toBe("w2"));
    expect(ws.snapshot?.last_seq).toBe(7);
    stop();
  });

  it("switch-back race: a load for the previous frame's workspace never overrides a later frame", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    const resolvers = pending(load);
    send(frame("w2"));
    send(frame("w1")); // switched back before w2's load landed
    resolvers[0](snap("w2", 9));
    await new Promise((r) => setTimeout(r, 0));
    expect(ws.snapshot?.workspace.id).toBe("w1");
    stop();
  });

  it("resync: applies another workspace's snapshot taken after the fetch started, even behind the stream", async () => {
    vi.useFakeTimers();
    const { ws, load, stop, send } = await started(snap("w1", 1));
    const resolvers = pending(load);
    void ws.reload(); // e.g. the post-outage resync, no frame seen: the daemon switched meanwhile
    send(quiet(8, "w2")); // streamed while the fetch was in flight (lastSeq was 5 at its start)
    resolvers[0](snap("w2", 7));
    await vi.advanceTimersByTimeAsync(0);
    expect(ws.snapshot?.workspace.id).toBe("w2");
    await vi.advanceTimersByTimeAsync(1000);
    expect(load).toHaveBeenCalledTimes(2); // no extra resync poll
    stop();
  });

  it("resync: a snapshot behind the fetch start is retried a bounded number of times, then accepted", async () => {
    vi.useFakeTimers();
    const { ws, load, stop } = await started(snap("w1", 1));
    load.mockResolvedValue(snap("w2", 2)); // e.g. a daemon restarted on a wiped events DB (lastSeq 5)
    void ws.reload();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(ws.snapshot?.workspace.id).toBe("w2");
    expect(load.mock.calls.length).toBeLessThanOrEqual(6); // initial + resync + at most 4 retries
    const calls = load.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(load).toHaveBeenCalledTimes(calls);
    // the accepted snapshot resets the stream position: later snapshots of it are not "behind"
    load.mockResolvedValue(snap("w2", 3));
    await ws.reload();
    expect(ws.snapshot?.last_seq).toBe(3);
    stop();
  });

  it("a lost frame does not strand the board: a later switch's snapshot is reconciled", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    const resolvers = pending(load);
    send(frame("w2"));
    resolvers[0](snap("w2", 6));
    await vi.waitFor(() => expect(ws.snapshot?.workspace.id).toBe("w2"));
    // Claude switches to w3 but its frame is dropped (full queue): only the stream shows it
    send(JSON.stringify({ seq: 9, ts_ms: 0, actor: "claude", type: "workspace.opened", object_id: "w3",
      klass: "internal", payload: {}, workspace: "w3" }));
    await vi.waitFor(() => expect(resolvers.length).toBe(2));
    resolvers[1](snap("w3", 9));
    await vi.waitFor(() => expect(ws.snapshot?.workspace.id).toBe("w3"));
    // and the reconciled target holds: a later frame for w3 only patches in place
    send(frame("w3"));
    expect(load).toHaveBeenCalledTimes(3);
    stop();
  });

  it("a frame for the shown workspace patches its title and question in place", async () => {
    const { ws, load, stop, send } = await started(snap("w1", 1));
    send(JSON.stringify({ kind: "workspace", active: { id: "w1", title: "renamed", question: "why?", archived: false, created_at_ms: 0 } }));
    expect(ws.snapshot?.workspace.title).toBe("renamed");
    expect(ws.snapshot?.workspace.question).toBe("why?");
    expect(ws.highlights.size).toBe(1);
    expect(load).toHaveBeenCalledTimes(1);
    stop();
  });

  it("a list fetched before a rename cannot overwrite the one fetched after it", async () => {
    const { ws, stop, send } = await started(snap("w1", 1));
    const lists = vi.mocked(fetchWorkspaces);
    lists.mockReset();
    const resolvers: ((l: Awaited<ReturnType<typeof fetchWorkspaces>>) => void)[] = [];
    lists.mockImplementation(() => new Promise((r) => resolvers.push(r)));
    const list = (title: string) => ({ active: "w1", more: 0, workspaces: [{ id: "w2", title } as WorkspaceInfo] });
    ws.refreshWorkspaces(); // the popover opened: a slow request, answered before the rename
    send(frame("w1")); // the rename's frame: a newer request
    expect(resolvers.length).toBe(2);
    resolvers[1](list("renamed"));
    await vi.waitFor(() => expect(ws.workspaces[0]?.title).toBe("renamed"));
    resolvers[0](list("old")); // the stale response lands last
    await new Promise((r) => setTimeout(r, 0));
    expect(ws.workspaces[0].title).toBe("renamed");
    stop();
  });

  describe("a saved mutation (rhe2)", () => {
    const info = (id: string, title: string, archived = false) =>
      ({ id, title, archived, question: null, created_at_ms: 0, last_activity_ms: 0, counts: { panels: 0, hypotheses: 0, findings: 0, open_threads: 0 } }) as WorkspaceInfo;
    const list = (...ws: WorkspaceInfo[]) => ({ active: "w1", more: 0, workspaces: ws });
    type L = Awaited<ReturnType<typeof fetchWorkspaces>>;
    async function withList(...ws: WorkspaceInfo[]) {
      const { ws: store, stop, send } = await started(snap("w1", 1));
      const lists = vi.mocked(fetchWorkspaces);
      lists.mockReset();
      lists.mockResolvedValue(list(...ws));
      await store.refreshWorkspaces();
      const resolvers: ((l: L) => void)[] = [];
      lists.mockReset();
      lists.mockImplementation(() => new Promise((r) => resolvers.push(r)));
      return { store, stop, send, lists, resolvers };
    }

    it("shows in the list at once, before any refetch answers", async () => {
      const { store, stop, resolvers } = await withList(info("w1", "one"), info("w2", "two"));
      store.applyWorkspace(info("w2", "renamed"));
      expect(store.workspaces.map((w) => w.title)).toEqual(["one", "renamed"]);
      expect(resolvers.length).toBe(1); // and a refetch issued after it
      store.applyWorkspace(info("w2", "renamed", true));
      expect(store.workspaces.map((w) => w.id)).toEqual(["w1"]); // archived: off the live list
      store.applyWorkspace(info("w3", "new"));
      expect(store.workspaces.map((w) => w.id)).toEqual(["w1", "w3"]); // created
      stop();
    });

    it("a list request issued before it cannot revert it, even when no later request was issued", async () => {
      const { store, stop, send, lists, resolvers } = await withList(info("w1", "one"), info("w2", "two"));
      void store.refreshWorkspaces(); // the popover opened before the rename was saved
      lists.mockImplementation(() => new Promise(() => {})); // later refetches never answer (a loaded daemon)
      store.applyWorkspace(info("w2", "renamed"));
      send(frame("w1")); // the rename's frame
      resolvers[0](list(info("w1", "one"), info("w2", "two"))); // the stale answer lands last
      await new Promise((r) => setTimeout(r, 0));
      expect(store.workspaces.find((w) => w.id === "w2")?.title).toBe("renamed");
      stop();
    });

    it("a list request issued after it applies", async () => {
      const { store, stop, resolvers } = await withList(info("w1", "one"), info("w2", "two"));
      store.applyWorkspace(info("w2", "renamed"));
      resolvers[0](list(info("w1", "one"), info("w2", "renamed again elsewhere")));
      await vi.waitFor(() => expect(store.workspaces.find((w) => w.id === "w2")?.title).toBe("renamed again elsewhere"));
      stop();
    });
  });

  it("a frame that changes nothing shown keeps the snapshot (no board-wide panel refetch)", async () => {
    const { ws, stop, send } = await started(snap("w1", 1));
    const active = { id: "w1", title: "t", question: null, archived: false, created_at_ms: 0 };
    send(JSON.stringify({ kind: "workspace", active }));
    const before = ws.snapshot;
    send(JSON.stringify({ kind: "workspace", active: { ...active, last_activity_ms: 5 } })); // another workspace was renamed
    expect(ws.snapshot).toBe(before);
    stop();
  });

  it("fetches the workspace list with the first snapshot, even one that landed after a retry", async () => {
    vi.useFakeTimers();
    const load = vi.mocked(fetchWorkspace);
    load.mockReset();
    load.mockRejectedValueOnce(new Error("down")).mockResolvedValue(snap("w1", 1));
    const lists = vi.mocked(fetchWorkspaces);
    lists.mockReset();
    lists.mockResolvedValue({ active: "w1", workspaces: [{ id: "w1" } as WorkspaceInfo], more: 0 });
    const ws = createWorkspace();
    await ws.reload();
    expect(lists).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(lists).toHaveBeenCalledTimes(1);
    expect(ws.workspaces.map((w) => w.id)).toEqual(["w1"]);
  });
});
