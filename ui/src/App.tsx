import { useEffect, useState } from "react";
import { fetchPanels, subscribe, type Panel } from "./api";
import { PanelView } from "./Panel";

export default function App() {
  const [panels, setPanels] = useState<Panel[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => fetchPanels().then(setPanels).catch((e) => setError(String(e)));
    load();
    return subscribe((e) => { if (e.type === "panel.created") load(); });
  }, []);

  useEffect(() => {
    const match = location.hash.match(/^#\/panel\/(\w+)$/);
    if (match) document.getElementById(`panel-${match[1]}`)?.scrollIntoView();
  }, [panels]);

  return (
    <main>
      <h1>Telemetry Nerd</h1>
      {error && <div className="error">{error}</div>}
      {panels.length === 0 && <p className="empty">No panels yet. Ask Claude a question about your metrics.</p>}
      {panels.map((p) => <PanelView key={p.id} panel={p} />)}
    </main>
  );
}
