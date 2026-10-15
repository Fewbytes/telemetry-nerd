// The state and handlers Panel passes to both of its control sets (NerdControls, SimpleToolbar).
import type { Panel, PanelData, YContext, YView, selectYView } from "../../lib/api";
import type { RangePreset, Viewport } from "../../chart/viewport";
import type { PreviewRender } from "../../chart/preview";
import type { DataViewName } from "../../chart/dataview";
import type { YStats } from "../../chart/yview";

export interface ControlProps {
  data: PanelData | null;
  panel: Panel;
  yst: YStats | null;
  yctx: YContext | null;
  yIsValues: boolean;
  chosen: YView | null;
  bandPick: boolean;
  idxBusy: boolean;
  overlayBusy: boolean;
  rangeBusy: boolean;
  rangeError: string | null;
  viewport: Viewport | null;
  previewRender: PreviewRender | null;
  viewNow: DataViewName | null;
  previewEl: HTMLDivElement | null;
  toggleOverlay: (key: "normal" | "limit" | "ghost", on: boolean) => void;
  pick: (body: Parameters<typeof selectYView>[1], optimistic: YView | null) => void;
  pickIndexed: (baseline: "window" | "previous" | "week") => void;
  pickView: (v: DataViewName) => void;
  pickPreset: (preset: RangePreset) => void;
  keepThisRange: () => void;
}
