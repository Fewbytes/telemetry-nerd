import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { fetchPanelData, reportRender, type Panel, type PanelData } from "./api";
import { toUplot } from "./chart/toUplot";

const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
const fmtStep = (ms: number) => (ms % 60_000 === 0 ? `${ms / 60_000}m` : `${ms / 1000}s`);

export function PanelView({ panel }: { panel: Panel }) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<PanelData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [render, setRender] = useState<{ ms: number; exceeded: boolean } | null>(null);

  useEffect(() => {
    const width = Math.round(plotRef.current?.clientWidth || 800);
    fetchPanelData(panel.id, width).then(setData).catch((e) => setError(String(e)));
  }, [panel.id]);

  useEffect(() => {
    const el = plotRef.current;
    if (!data || !el) return;
    const model = toUplot(data.series);
    const width = el.clientWidth || 800;
    const unit = data.panel.spec.y.unit;
    const t0 = performance.now();
    const plot = new uPlot(
      {
        width, height: 260, series: model.series, bands: model.bands,
        scales: { x: { time: true } },
        axes: [{}, { label: unit ?? "value (unit unknown)" }],
      },
      model.data, el,
    );
    const ms = performance.now() - t0;
    reportRender({ panel_id: panel.id, render_ms: ms, points: model.points, width_px: width })
      .then((r) => setRender({ ms, exceeded: r.budget_exceeded }))
      .catch((e) => setError(String(e)));
    return () => plot.destroy();
  }, [data, panel.id]);

  return (
    <section
      className="panel"
      id={`panel-${panel.id}`}
      data-panel-id={panel.id}
      data-render-ms={render ? render.ms.toFixed(1) : undefined}
      data-budget-exceeded={render ? String(render.exceeded) : undefined}
    >
      <header>
        <span className="question">Q: {panel.question}</span>
        <span className={`status ${panel.status}`}>{panel.status}</span>
      </header>
      {panel.spec.y.range_mode === "data" && (
        <div className="badge">y scaled to data (no reference range yet)</div>
      )}
      {error && <div className="error">{error}</div>}
      <div ref={plotRef} className="plot" />
      {data && (
        <footer>
          {data.dataset.source} · <code>{data.dataset.expr}</code> ·{" "}
          {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step{" "}
          {fmtStep(data.effective_step_ms)} · {data.dataset.representation}, min/max envelope
          {data.caveats.length > 0 && <> · caveats: {data.caveats.join(", ")}</>}
        </footer>
      )}
    </section>
  );
}
