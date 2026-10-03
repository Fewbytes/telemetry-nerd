import type { FleetData } from "../chart/fleet";
import type { SeasonalSeries } from "../chart/seasonal";
import type { SpcBaseline, SpcSeries } from "../chart/spc";
import type { LittlesSeries, LittlesUnmatched } from "../chart/littles";
import type { VariationSource } from "./sources";
export interface ChartSpec {
  layers: {
    mark: string; data: string;
    windows?: { start_ms: number; end_ms: number; label: string }[]; color?: "count" | "density";
    quantiles?: number[];
  }[];
  y: {
    range_mode: "data" | "reference" | "semantic"; unit: string | null; label: string | null;
    views?: YView[]; selected?: YView | null; context?: YContext | null;
  };
  overlays?: OverlayFlags;
  auto?: { transform: "rate" | "reframe"; source_dataset: string; reason: string } | null;
  signal?: { filter: string; kind: string; reason: string; offered: string[]; default: string; selected?: string | null } | null;
  references?: Record<string, { mode: string; label: string; start_ms: number; end_ms: number; shift_ms: number; series: string; dist?: string | null }>;
  marginal?: { reference: "previous" | "week" | "profile"; author?: string; reason?: string | null } | null;
  /** the panel is one role of a panel group (bead czt.3) */
  group?: { id: string; role: string } | null;
}
/** What the catalog says about the y axis (bead 2as.10). */
/** A line drawn from catalog context (2as.15): a hard limit, a threshold or a reference series. */
export interface ContextLine {
  metric: string; dataset?: string | null; hi: number; basis: string;
  kind?: "limit" | "threshold" | "reference"; label?: string | null; origin?: string | null;
  confidence?: number | null; tone?: "bad" | "warn" | "info" | null; value?: number | null;
}
/** A proposed way to show the metric that carries its own context; accepting makes a new panel. */
export interface Reframing { title: string; reason: string; basis: string; expr: string; kind: "substitute" | "percent_of_limit"; unit?: string | null }
export interface YContext {
  natural_lo: number | null; natural_hi: number | null; bounds: string | null; bounds_origin: string | null;
  bounds_basis?: string | null; bounds_confidence?: number | null; // why a derived/asserted bound holds
  limit: ContextLine | null;
  lines?: ContextLine[];
  reframes?: Reframing[];
  profile: { lo: number; hi: number; label: string } | null;
  /** observed characteristic range from a catalog scan (4f1): descriptive, never a bound */
  typical?: { lo: number; hi: number; label: string; basis: string } | null;
  notes: string[];
}
export interface YView {
  mode: "auto" | "zero" | "data" | "reference" | "semantic" | "meaningful" | "band" | "log" | "indexed" | "typical";
  baseline?: "window" | "previous" | "week" | null;
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
  /** declared interval of a code output (DatasetMeta.uncertainty says what it is) */
  lo?: (number | null)[]; hi?: (number | null)[];
}
/** A tier-2 code output's producer (spec §5.2): the code node and its output name. */
export interface Producer { kind: "code" | "binding"; node?: string; output?: string; op?: string; description?: string }
/** Declared uncertainty: an interval (lo/hi per row) of `level` by `method`, or exact. */
export interface Uncertainty { method?: string; level?: number | null; kind?: string; exact?: boolean }
export interface DatasetMeta {
  id: string; source: string; expr: string; start_ms: number; end_ms: number;
  step_ms: number; resolution_ms: number; representation: string;
  quantile?: number | null; n_min?: number | null;
  scheme?: BucketSchemeInfo | null; histogram?: { selector: string; by: string[] } | null;
  source_caveats?: string[];
  producer?: Producer | null; parents?: string[]; unit?: string | null; uncertainty?: Uncertainty | null;
}
/** Distribution cells. lo === null means -Inf, hi === null means +Inf (JSON has no Infinity). */
export interface FilterInfo {
  filter: string; kind: "lowpass" | "highpass" | "bandpass"; reason: string;
  offered: ("overlay" | "filtered" | "removed" | "raw")[]; default: "overlay" | "filtered" | "removed" | "raw";
  selected?: "overlay" | "filtered" | "removed" | "raw" | null;
  edges: Record<string, number[][]>; period_ms: number; period_hi_ms?: number | null;
}
export interface SpectrumPanelData extends PanelDataBase {
  kind: "spectrum"; effective_step_ms: number; limits: { shortest_s: number; longest_s: number };
  series: { id: string; labels: Record<string, string>; periods_s: number[]; power: number[]; level: number;
    red_level?: number[]; ar1_phi?: number;
    peaks: { period_s: number; interval_s: [number, number]; power: number; significant: boolean; fap: number; fap_red_noise?: number; period: string }[]; caveats: string[] }[];
}
export interface SpectrogramPanelData extends PanelDataBase {
  kind: "spectrogram"; segment_ms: number; hop_ms: number; overlap: number; effective_step_ms: number;
  limits: { shortest_s: number; longest_s: number };
  series: { id: string; labels: Record<string, string>; ts: number[]; rows: { lo_s: number[]; hi_s: number[] };
    power: ((number | null)[])[]; level: (number | null)[] }[];
}
export interface SeasonalPanelData extends PanelDataBase {
  kind: "seasonal"; effective_step_ms: number; tz: string; series: SeasonalSeries[];
}
export interface FleetPanelData extends PanelDataBase, FleetData {
  kind: "fleet"; effective_step_ms: number;
}
export interface SpcPanelData extends PanelDataBase {
  kind: "spc"; effective_step_ms: number; baseline: SpcBaseline; series: SpcSeries[];
  skipped: { labels: Record<string, string>; reason: string }[];
}
export interface LittlesPanelData extends PanelDataBase {
  kind: "littles"; effective_step_ms: number; window_ms: number; substep_ms: number;
  series: LittlesSeries[]; unmatched: LittlesUnmatched[]; more_groups: number;
}
export interface IndexPayload {
  baseline: "window" | "previous" | "week"; label: string; refused?: string;
  values?: Record<string, number | null>;
  series?: { id: string; ts: number[]; avg: (number | null)[]; count: (number | null)[] }[];
}
export interface MarginalData {
  basis: "distribution" | "samples"; what: string; n_min: number; windows: WindowHist[]; excluded: number[];
  reference: { mode: string; label: string; start_ms: number; end_ms: number }; author: string; reason: string | null;
}
export interface HeatCells { ts: number[]; lo: (number | null)[]; hi: (number | null)[]; c: number[] }
/** Per column, the source bucket holding a quantile (server-side, n-gated). */
export interface QuantileBand { ts: number[]; lo: (number | null)[]; hi: (number | null)[] }
export interface HeatSeries {
  id: string; labels: Record<string, string>;
  ts: number[]; n: number[]; cover: number[]; cells: HeatCells;
  state?: BucketStatePayload | null;
  quantiles?: Record<string, QuantileBand>; // key String(q); band only where n >= minSamples(q)
}
export interface WindowHist {
  label: string; start_ms: number; end_ms: number; n: number; columns: number;
  expected_columns?: number; unknown?: boolean; unknown_columns?: number; missing_columns?: number;
  lo: (number | null)[]; hi: (number | null)[]; c: number[];
  /** source buckets, only present when `lo/hi/c` were value-merged into bars */
  source?: { lo: (number | null)[]; hi: (number | null)[]; c: number[] };
}
export interface BucketSchemeInfo {
  kind: string; edges: number[]; schema: number | null; per_decade: number | null; description: string;
}
export interface BucketStatePayload { id: string; ts: number[]; state: number[]; observed: number[]; expected: number[]; flags: number[] }
export interface Where { spans?: [number, number][] | null; series?: string[] | null }
export interface Caveat { code: string; severity: "info" | "warn" | "blocks_claim"; message: string; where?: Where | null; source: string }
interface PanelDataBase { panel: Panel; dataset: DatasetMeta; caveats: string[]; located?: Caveat[] }
/** Reference layers on a time panel (bead 2as.11): availability always, data only when on. */
export interface LineData extends ContextLine { series?: SeriesData[] }
export interface OverlayFlags { normal: boolean; limit: boolean; ghost: boolean }
export interface BandSeries { ts: number[]; lo: (number | null)[]; hi: (number | null)[] }
export interface GhostSeries { id: string; ts: number[]; avg: (number | null)[]; count: (number | null)[] }
export interface OverlaysPayload {
  flags: OverlayFlags;
  normal: { available: boolean; reason?: string; label?: string; stale?: boolean; series?: Record<string, BandSeries>; unmatched?: string[] };
  limit: { available: boolean; reason?: string; label?: string; metric?: string; hi?: number; lines?: LineData[] };
  ghost: { available: boolean; reason?: string; loaded: boolean; label?: string; series?: GhostSeries[] };
}
export interface TimePanelData extends PanelDataBase { kind: "time"; bucket_state?: BucketStatePayload[]; bucket_state_more?: number; overlays?: OverlaysPayload; effective_step_ms: number; series: SeriesData[]; marginal?: MarginalData | null; index?: IndexPayload | null; raw?: SeriesData[]; removed?: SeriesData[]; filter?: FilterInfo }
export interface HeatmapPanelData extends PanelDataBase {
  kind: "heatmap"; mark: "heatmap" | "percentiles"; effective_step_ms: number; value_merge: number; facet_height_px: number; series: HeatSeries[];
}
export interface HistogramPanelData extends PanelDataBase {
  kind: "histogram"; mark: "histogram" | "ecdf" | "quantile_curve" | "ccdf"; effective_step_ms: number; value_merge: number;
  series: { id: string; labels: Record<string, string>; windows: WindowHist[] }[];
}
export type PanelData = TimePanelData | HeatmapPanelData | HistogramPanelData | SpectrumPanelData | SpectrogramPanelData | SpcPanelData | SeasonalPanelData | FleetPanelData | LittlesPanelData;
export interface WorkspaceEvent {
  seq: number; ts_ms: number; actor: "claude" | "user" | "system" | "code"; type: string;
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
  | { kind: "claim"; source: string; metric: string; field: string; origins: string[]; note?: string | null }
  | {
      kind: "statistic"; dataset: string; name: string; value: number;
      interval: [number, number] | null; exact: boolean; method: string;
      params: Record<string, unknown>;
      /** spec §5.3: the value's uncertainty is not known (citable, flagged) */
      uncertainty_unknown?: boolean;
      /** spec §5.4: what the op attributed the variation to */
      source?: VariationSource | null;
    };
/** Server-derived uncertainty flag on one evidence item of a finding (spec §5.3). */
export interface EvidenceFlag {
  evidence: number;
  flag: "uncertainty_unknown" | "input_uncertainty_unknown" | "uncertainty_not_propagated";
  message: string;
}
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
  evidence_flags?: EvidenceFlag[];
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
/** A tier-2 run in the snapshot (no code text, no streams: GET /api/code/{id} has them). */
export interface CodeBrief {
  id: string; status: "running" | "ok" | "failed"; exec_status: string | null;
  inputs: string[]; outputs: string[]; author: string; created_at_ms: number;
  finished_at_ms: number | null; duration_s: number | null; rerun_of: string | null;
  error: string | null; restarted: boolean;
}
export interface CodeOutput {
  name: string; dataset: string; representation: string; rows: number; caveats: string[];
  /** always true since x2x (spec §5.3: every output is citable); false on nodes stored before */
  evidence_ok: boolean;
  /** uncertainty status: no_uncertainty | input_uncertainty_unknown | uncertainty_not_propagated */
  uncertainty?: string | null;
}
export interface CodeIssue { name: string; code: string; message: string }
/** A tier-2 run in full (spec §5.2). Finished nodes are immutable; a re-run is a new node. */
export interface CodeNode extends Omit<CodeBrief, "outputs"> {
  code: string; timeout_s: number | null; stdout: string; stderr: string; result: string | null;
  traceback: string | null; truncated: boolean; outputs: CodeOutput[]; issues: CodeIssue[];
}
export interface Snapshot {
  panels: Panel[]; annotations: Annotation[]; hypotheses: Hypothesis[];
  findings: Finding[]; gaps: Gap[]; threads: Thread[]; last_seq: number;
  code?: CodeBrief[];
  groups?: PanelGroup[];
}
/** One role of a panel group: its panel, or the gap where its signal is missing (bead czt.3). */
export interface GroupRole {
  role: string; metric: string | null; panel: string | null; form: string | null;
  view: "lines" | "fleet" | "heatmap" | "model" | "gap" | "error"; members: number | null; notes: string[];
  suggestion: { name: string; type: string; labels: string[] } | null; why: string | null;
  gap: string | null; error: string | null;
  /** binding_verdict (bead czt.4): this role against its reference windows */
  verdict?: RoleVerdict | null;
}
export interface RoleVerdict {
  status: "changed" | "no_change" | "insufficient" | "error" | string;
  direction: "higher" | "lower" | null; pattern: string | null; text: string | null; at_capacity: boolean;
  onset?: { at: string | null; interval?: [string | null, string]; before?: string; basis: string };
  /** spec §5.4: special_cause (changed), common_cause (no change), undetermined (change on suspect data) */
  source?: VariationSource;
}
/** binding_verdict's summary: which golden signal moved first, against what, at what alpha. */
export interface GroupVerdict {
  text: string; first: string | null; moved: string[]; reference: string; alpha: number; at_ms: number;
}
/** A USE / RED / Little's law binding drawn as one linked group of panels. */
export interface PanelGroup {
  id: string; kind: string; key: string; source: string; author: string; created_at_ms: number;
  start_ms: number; end_ms: number; step_ms: number; basis: "binding" | "suggestion";
  binding_origin: string | null; suggestion: string | null; join_on: string[];
  matchers: Record<string, string>; error_matcher: string | null; roles: GroupRole[];
  notes: string[]; closed: boolean; reframed_from: string | null;
  verdict?: GroupVerdict | null;
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
export const closeGroup = (id: string) => postJSON<PanelGroup>(`/api/groups/${id}/close`);
/** The same group over a selected window: a new group, this one stays. */
export const reframeGroup = (id: string, start_ms: number, end_ms: number) =>
  postJSON<PanelGroup>(`/api/groups/${id}/reframe`, { start_ms, end_ms });

export const fetchCode = (id: string) => fetch(`/api/code/${encodeURIComponent(id)}`).then((r) => json<CodeNode>(r));
/** Run a node's code again on the same inputs: answers with the NEW node once it has finished. */
export const rerunCode = (id: string) => postJSON<CodeNode>(`/api/code/${encodeURIComponent(id)}/rerun`);

export const fetchPanelData = (id: string, width: number) =>
  fetch(`/api/panels/${id}/data?width=${width}`).then((r) => json<PanelData>(r));

export const reportRender = (r: { panel_id: string; render_ms: number; points: number; width_px: number; height_px?: number }) =>
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

export const selectYView = (id: string, body: { mode?: string; lo?: number; hi?: number; suggestion?: string; baseline?: string }) =>
  postJSON<Panel>(`/api/panels/${id}/y-view`, body);

export const setMarginal = (id: string, reference: "previous" | "week" | "profile" | null) =>
  postJSON<Panel>(`/api/panels/${id}/marginal`, { reference });

export const selectDataView = (id: string, view: string) => postJSON<Panel>(`/api/panels/${id}/data-view`, { view });

/** Recompute a panel's y context, e.g. once its operating profile has finished computing. */
export interface OutcomeSplit { label: string; excluded: string[]; success: { panel: string; values: string[] } | null; failure: { panel: string; values: string[] } | null; note?: string }
export const splitOutcome = (id: string) => postJSON<OutcomeSplit>(`/api/panels/${id}/split-outcome`);
export const reframePanel = (id: string, index: number) => postJSON<{ panel: Panel }>(`/api/panels/${id}/reframe`, { index });
export const refreshYContext = (id: string) => postJSON<unknown>(`/api/panels/${id}/y-context`);

/** Switch reference layers; turning the ghost on makes the daemon fetch last week. */
export const setOverlays = (id: string, body: Partial<OverlayFlags>) =>
  postJSON<unknown>(`/api/panels/${id}/overlays`, body);

/** The metric card behind a panel (bead 2as.12). */
export interface CardClaim { value: unknown; origin: string; confidence: number; basis: string | null }
export interface CardField {
  field: string; editable: boolean; value: unknown; origin: string | null; confidence: number | null;
  basis: string | null; conflict: boolean; claims: CardClaim[];
}
export interface CardRelation { subject: string; kind: string; object: string; origin: string; confidence: number; contested: boolean; basis: string | null }
export interface CardBinding { kind: string; key: string; roles: Record<string, string | null>; join_on: string[]; origin: string; confidence: number; contested: boolean }
export interface CardMetric {
  metric: string; present: boolean;
  family?: { role: "family"; members: number; status: string } | { role: "member"; template: string; dimension: string | null; inherited: boolean } | null; fields: CardField[]; relations: CardRelation[]; bindings: CardBinding[];
  gaps: { id: string; binding: string; role: string }[];
}
export interface MetricCard {
  source: string; learned: boolean; metrics: CardMetric[];
  /** a code output's card: no catalog metrics behind it, the producer instead */
  produced_by?: { kind: "code"; node: string; output: string; parents: string[] };
  profile: {
    available: boolean; reason?: string; window_ms?: number; stale?: boolean; series_total?: number;
    range?: Record<string, number | null>; seasonal?: { period: string; amplitude: number | null }; caveats?: string[];
  };
  quality: {
    step_ms: number; resolution_ms: number; scrape_interval_ms: number | null; scrape_interval_reason: string | null;
    series: number; gap_pct: number | null;
    resets: { measured: boolean; reason?: string; window_ms?: number; series?: number; samples?: number; resets?: number; small_decreases?: number; negatives?: number; verdict?: string; scanned_ms?: number };
    cardinality: { in_panel: number; catalog: number | null };
  };
}
export const fetchCard = (id: string) => fetch(`/api/panels/${id}/card`).then((r) => json<MetricCard>(r));
/** The user confirms (current value) or edits a catalog field: recorded as origin "user". */
export const postClaim = (source: string, metric: string, field: string, value: unknown) =>
  postJSON<unknown>("/api/catalog/claims", { source, metric, field, value });

/** The catalog view (bead 2as.13): one page of the catalog with provenance per row. */
export interface CatalogRow {
  metric: string; present: boolean;
  type: string | null; unit: string | null; role: string | null; bounds: string | null;
  origins: Record<string, string>; confidences: Record<string, number>;
  conflicts: string[]; findings: { kind: string; id: string }[]; verdict: string | null; reviewed: boolean;
  /** name-template families (bead 2as.16): this row IS a family / belongs to one */
  is_family: boolean; family: string | null; dimension: string | null; family_members: number | null;
  family_info?: { template: string; members: number; distinct: number; status: "detected" | "confirmed"; decided_by: string | null } | null;
}
export interface CatalogPage {
  source: string; total: number; offset: number; rows: CatalogRow[];
  summary: { metrics: number; reviewed: number; conflicts: number; findings: number; families: number; family_members: number };
}
export const fetchCatalog = (params: URLSearchParams) =>
  fetch(`/api/catalog?${params}`).then((r) => json<CatalogPage>(r));
export const fetchCatalogMetric = (source: string, metric: string) =>
  fetch(`/api/catalog/${encodeURIComponent(source)}/${encodeURIComponent(metric)}`).then((r) => json<CardMetric>(r));
export interface SourceInfo { name: string; url?: string; live?: boolean }
export const fetchSources = () => fetch("/api/sources").then((r) => json<{ sources: SourceInfo[] }>(r));

export interface FamilyMembers {
  template: string; members: number; distinct: number; status: string; offset: number;
  members_page: { metric: string; dimension: string }[];
}
export const fetchFamilyMembers = (source: string, template: string, offset = 0) =>
  fetch(`/api/catalog/${encodeURIComponent(source)}/families/${encodeURIComponent(template)}/members?offset=${offset}`).then((r) => json<FamilyMembers>(r));
/** The user confirms a name family, or splits it into ordinary metrics for good. */
export const decideFamily = (source: string, template: string, action: "confirm" | "split") =>
  postJSON<unknown>("/api/catalog/families", { source, template, action });

