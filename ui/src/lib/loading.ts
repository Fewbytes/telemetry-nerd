// Panel fetch lifecycle (bead b0jz): what to show while a graph request is in flight,
// so "still working" never looks the same as "stalled" or "nothing here".
export type PanelLoadingState = "skeleton" | "overlay" | null;

export function panelLoadingState(loading: boolean, hasData: boolean): PanelLoadingState {
  if (!loading) return null;
  return hasData ? "overlay" : "skeleton";
}
