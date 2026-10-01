export interface ChartSpec {
  layers: {
    mark: string; data: string;
    windows?: { start_ms: number; end_ms: number; label: string }[]; color?: "count" | "density";
  }[];
  y: {
    range_mode: "data" | "reference" | "semantic"; unit: string | null; label: string | null;
    views?: YView[]; selected?: YView | null;
  };
}
export interface YView {
  mode: "auto" | "zero" | "data" | "meaningful" | "band" | "log";
  label: string; reason?: string | null; lo?: number | null; hi?: number | null;
  id?: string | null; author?: string | null;
}
export interface Panel {
  id: string; question: string; status: string; spec: ChartSpec;
  dataset_ids: string[]; created_at_ms: number;
  answered_by: string | null; closed: boolean;
}
export interface SeriesData {
  id: string; labels: Record<string, string>; ts: number[];
  avg: (number | null)[]; min: (number | null)[]; max: (number | null)[]; count: (number | null)[];
}
export interface DatasetMeta {
  id: string; source: string; expr: string; start_ms: number; end_ms: number;
  step_ms: number; resolution_ms: number; representation: string;
  quantile?: number | null; n_min?: number | null;
  scheme?: BucketSchemeInfo | null; histogram?: { selector: string; by: string[] } | null;
  source_caveats?: string[];
}
/** Distribution cells. lo === null means -Inf, hi === null means +Inf (JSON has no Infinity). */
export interface HeatCells { ts: number[]; lo: (number | null)[]; hi: (number | null)[]; c: number[] }
export interface HeatSeries {
  id: string; labels: Record<string, string>;
  ts: number[]; n: number[]; cover: number[]; cells: HeatCells;
}
export interface WindowHist {
  label: string; start_ms: number; end_ms: number; n: number; columns: number;
  lo: (number | null)[]; hi: (number | null)[]; c: number[];
}
export interface BucketSchemeInfo {
  kind: string; edges: number[]; schema: number | null; per_decade: number | null; description: string;
}
interface PanelDataBase { panel: Panel; dataset: DatasetMeta; caveats: string[] }
export interface TimePanelData extends PanelDataBase { kind: "time"; effective_step_ms: number; series: SeriesData[] }
export interface HeatmapPanelData extends PanelDataBase {
  kind: "heatmap"; effective_step_ms: number; value_merge: number; facet_height_px: number; series: HeatSeries[];
}
export interface HistogramPanelData extends PanelDataBase {
  kind: "histogram"; mark: "histogram" | "ecdf"; effective_step_ms: number; value_merge: number;
  series: { id: string; labels: Record<string, string>; windows: WindowHist[] }[];
}
export type PanelData = TimePanelData | HeatmapPanelData | HistogramPanelData;
export interface WorkspaceEvent {
  seq: number; ts_ms: number; actor: "claude" | "user" | "system"; type: string;
  object_id: string | null; klass: "intentional" | "ambient" | "internal";
  payload: Record<string, unknown>;
}

export interface TimeSpan { start_ms: number; end_ms: number }
export interface Scope {
  source: string; selector: string; time_range: TimeSpan; step: string;
  aggregation: string; baseline_range: TimeSpan | null;
}
export type EvidenceRef =
  | { kind: "panel"; panel: string }
  | { kind: "annotation"; annotation: string }
  | {
      kind: "statistic"; dataset: string; name: string; value: number;
      interval: [number, number] | null; exact: boolean; method: string;
      params: Record<string, unknown>;
    };
export interface Annotation {
  id: string; kind: "event" | "region" | "threshold" | "band" | "note";
  panel: string | null; t_start_ms: number | null; t_end_ms: number | null;
  value: number | null; value_hi: number | null; label: string; links: string[];
  author: string; created_at_ms: number; deleted: boolean;
}
export interface Hypothesis {
  id: string; statement: string; status: "proposed" | "supported" | "refuted" | "inconclusive";
  author: string; evidence_for: string[]; evidence_against: string[];
  created_at_ms: number; updated_at_ms: number;
}
export interface Finding {
  id: string; claim: string; scope: Scope; evidence: EvidenceRef[]; caveats: string[];
  hypothesis: string | null; stance: "for" | "against" | null; answers_panel: string | null;
  author: string; created_at_ms: number;
  verdict: "accepted" | "rejected" | "needs-more" | null; verdict_comment: string | null;
}
export interface Gap {
  id: string; missing_signal: string; needed_for: string;
  suggestion: { name: string; type: "counter" | "gauge" | "histogram" | "summary"; labels: string[] };
  author: string; created_at_ms: number;
}
export interface Message {
  id: string; thread: string; author: string; text: string; created_at_ms: number;
  /** seq of the message's thread.message event; compared with Presence.delivered_up_to */
  seq: number | null;
}
/** Control frame on /ws (no seq, never logged): who is connected and what was delivered. */
export interface SessionPresence {
  consumer: string; kind: string; status: "live" | "terminal" | "offline";
  mode: "hook" | "channel" | null; since_ms: number | null;
}
export interface Presence {
  kind: "presence"; status: "live" | "terminal" | "offline";
  mode: "hook" | "channel" | null; since_ms: number | null; delivered_up_to: number;
  /** every connected consumer/session (per-session consumers, dtk) */
  sessions?: SessionPresence[];
}
export interface Thread {
  id: string; anchor: string | null; selection: TimeSpan | null; author: string;
  created_at_ms: number; messages: Message[];
}
export interface Snapshot {
  panels: Panel[]; annotations: Annotation[]; hypotheses: Hypothesis[];
  findings: Finding[]; gaps: Gap[]; threads: Thread[]; last_seq: number;
}

export class ApiError extends Error {
  status: number;
  error: string;
  hint: string | null;
  issues: unknown[];
  constructor(status: number, error: string, hint: string | null = null, issues: unknown[] = []) {
    super(`${status} ${error}${hint ? ` (${hint})` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.error = error;
    this.hint = hint;
    this.issues = issues;
  }
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text();
    let body: { error?: string; hint?: string; issues?: unknown[] } = {};
    try { body = JSON.parse(text); } catch { /* non-JSON error body */ }
    throw new ApiError(resp.status, body.error ?? text, body.hint ?? null, body.issues ?? []);
  }
  return resp.json() as Promise<T>;
}

export const postJSON = <T>(path: string, body: unknown = {}) =>
  fetch(path, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }).then((r) => json<T>(r));

export const fetchWorkspace = () => fetch("/api/workspace").then((r) => json<Snapshot>(r));
export const closePanel = (id: string) => postJSON<unknown>(`/api/panels/${id}/close`);

export const fetchPanelData = (id: string, width: number) =>
  fetch(`/api/panels/${id}/data?width=${width}`).then((r) => json<PanelData>(r));

export const reportRender = (r: { panel_id: string; render_ms: number; points: number; width_px: number }) =>
  postJSON<{ budget_exceeded: boolean }>("/api/render-report", r);

export interface SocketHandlers {
  onPresence?: (p: Presence) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

export function subscribe(
  onEvent: (e: WorkspaceEvent) => void,
  sinceRef: () => number = () => 0,
  handlers: SocketHandlers = {},
): () => void {
  let socket: WebSocket | null = null;
  let stopped = false;
  const connect = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws?since=${sinceRef()}`);
    socket.onopen = () => handlers.onOpen?.();
    socket.onmessage = (m) => {
      try {
        const frame = JSON.parse(m.data);
        if (frame.kind === "presence") handlers.onPresence?.(frame as Presence);
        else onEvent(frame as WorkspaceEvent);
      } catch (e) {
        console.error("malformed workspace event", e, m.data);
      }
    };
    socket.onclose = () => {
      if (stopped) return;
      handlers.onClose?.();
      setTimeout(connect, 1000);
    };
  };
  connect();
  return () => { stopped = true; socket?.close(); };
}

export const selectYView = (id: string, body: { mode?: string; lo?: number; hi?: number; suggestion?: string }) =>
  postJSON<Panel>(`/api/panels/${id}/y-view`, body);
