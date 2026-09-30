export interface ChartSpec {
  layers: { mark: string; data: string }[];
  y: { range_mode: "data" | "reference" | "semantic"; unit: string | null; label: string | null };
}
export interface Panel {
  id: string; question: string; status: string; spec: ChartSpec;
  dataset_ids: string[]; created_at_ms: number;
}
export interface SeriesData {
  id: string; labels: Record<string, string>; ts: number[];
  avg: (number | null)[]; min: (number | null)[]; max: (number | null)[]; count: (number | null)[];
}
export interface DatasetMeta {
  id: string; source: string; expr: string; start_ms: number; end_ms: number;
  step_ms: number; resolution_ms: number; representation: string;
}
export interface PanelData {
  panel: Panel; dataset: DatasetMeta; effective_step_ms: number;
  series: SeriesData[]; caveats: string[];
}
export interface WorkspaceEvent {
  seq: number; ts_ms: number; actor: "claude" | "user" | "system"; type: string;
  object_id: string | null; klass: "intentional" | "ambient" | "internal";
  payload: Record<string, unknown>;
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
  return resp.json() as Promise<T>;
}

export const fetchPanels = () => fetch("/api/panels").then((r) => json<Panel[]>(r));

export const fetchPanelData = (id: string, width: number) =>
  fetch(`/api/panels/${id}/data?width=${width}`).then((r) => json<PanelData>(r));

export const reportRender = (r: { panel_id: string; render_ms: number; points: number; width_px: number }) =>
  fetch("/api/render-report", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(r),
  }).then((resp) => json<{ budget_exceeded: boolean }>(resp));

export function subscribe(onEvent: (e: WorkspaceEvent) => void): () => void {
  let socket: WebSocket | null = null;
  let stopped = false;
  const connect = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws`);
    socket.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch (e) {
        console.error("malformed workspace event", e, m.data);
      }
    };
    socket.onclose = () => { if (!stopped) setTimeout(connect, 1000); };
  };
  connect();
  return () => { stopped = true; socket?.close(); };
}
