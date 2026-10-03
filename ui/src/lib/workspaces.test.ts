import { describe, expect, it } from "vitest";
import type { Snapshot, WorkspaceInfo } from "./api";
import { defaultTitle, sortForSwitcher, workspaceChanged } from "./workspaces";

const info = (id: string, last_activity_ms = 0): WorkspaceInfo => ({
  id, title: id, question: null, archived: false, created_at_ms: 0, last_activity_ms,
  counts: { panels: 0, hypotheses: 0, findings: 0, open_threads: 0 },
});
const snap = (id: string): Snapshot => ({
  panels: [], annotations: [], hypotheses: [], findings: [], gaps: [], threads: [], last_seq: 1,
  workspace: info(id),
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
