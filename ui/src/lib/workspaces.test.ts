import { describe, expect, it } from "vitest";
import type { Panel, Snapshot, WorkspaceInfo } from "./api";
import { defaultTitle, reconcilePanels, sortForSwitcher, withWorkspace, workspaceChanged } from "./workspaces";

const info = (id: string, last_activity_ms = 0): WorkspaceInfo => ({
  id, title: id, question: null, archived: false, created_at_ms: 0, last_activity_ms,
  counts: { panels: 0, hypotheses: 0, findings: 0, open_threads: 0 },
});
const snap = (id: string): Snapshot => ({
  panels: [], annotations: [], hypotheses: [], findings: [], gaps: [], threads: [], last_seq: 1,
  workspace: info(id),
});

const panel = (id: string, question = "q"): Panel => ({
  id, question, status: "open", dataset_ids: ["d1"], created_at_ms: 0, answered_by: null, closed: false,
  spec: { layers: [{ mark: "line", data: "d1" }], y: { range_mode: "data", unit: null, label: null } },
});

describe("workspaceChanged", () => {
  it("is false for the first snapshot and for the same workspace", () => {
    expect(workspaceChanged(null, snap("w1"))).toBe(false);
    expect(workspaceChanged(snap("w1"), snap("w1"))).toBe(false);
  });
  it("is true when the workspace id differs", () => {
    expect(workspaceChanged(snap("w1"), snap("w2"))).toBe(true);
  });
});

describe("defaultTitle", () => {
  it("formats local time as Investigation YYYY-MM-DD HH:MM", () => {
    expect(defaultTitle(new Date(2026, 9, 3, 14, 5))).toBe("Investigation 2026-10-03 14:05");
  });
});

describe("sortForSwitcher", () => {
  it("puts the active one first, then most recent activity", () => {
    const list = [info("a", 1), info("b", 5), info("c", 3)];
    expect(sortForSwitcher(list, "a").map((w) => w.id)).toEqual(["a", "b", "c"]);
    expect(list.map((w) => w.id)).toEqual(["a", "b", "c"]);
  });
});

describe("withWorkspace", () => {
  const list = [info("w1"), info("w2")];
  it("replaces a saved workspace in place and appends a new one, without mutating", () => {
    const renamed = { ...info("w2"), title: "renamed" };
    expect(withWorkspace(list, renamed).map((w) => w.title)).toEqual(["w1", "renamed"]);
    expect(withWorkspace(list, info("w3")).map((w) => w.id)).toEqual(["w1", "w2", "w3"]);
    expect(list.map((w) => w.title)).toEqual(["w1", "w2"]);
  });
  it("drops an archived one from a live list, keeps it in a full one", () => {
    const archived = { ...info("w2"), archived: true };
    expect(withWorkspace(list, archived).map((w) => w.id)).toEqual(["w1"]);
    expect(withWorkspace(list, archived, true)[1].archived).toBe(true);
  });
});

describe("reconcilePanels", () => {
  it("reuses the old object for a panel that is byte-for-byte unchanged, by id", () => {
    const old1 = panel("p1");
    const old2 = panel("p2");
    // a fresh fetch re-parses every panel from JSON: p1 is identical content but a new object;
    // only p2 actually changed (its spec grew a window)
    const next1 = panel("p1");
    const next2 = { ...panel("p2"), spec: { ...panel("p2").spec, layers: [{ mark: "line", data: "d1", windows: [] }] } };
    const out = reconcilePanels([old1, old2], [next1, next2]);
    expect(out[0]).toBe(old1); // unchanged: kept the prior reference
    expect(out[1]).toBe(next2); // changed: the new object, so its component refetches
    expect(out[1]).not.toBe(old2);
  });

  it("does not reuse across ids, and passes new panels through untouched", () => {
    const old1 = panel("p1");
    const next = panel("p3");
    const out = reconcilePanels([old1], [next]);
    expect(out[0]).toBe(next);
  });

  it("returns `next` itself (no new array) when nothing could be reused", () => {
    const next = [panel("p9")];
    expect(reconcilePanels([], next)).toBe(next);
    expect(reconcilePanels([panel("p1")], next)).toBe(next);
  });

  it("does not mutate either input array", () => {
    const old = [panel("p1")];
    const next = [panel("p1")];
    reconcilePanels(old, next);
    expect(old[0]).not.toBe(next[0]); // originals untouched; only the returned array is new
  });
});
