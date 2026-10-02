// Linked crosshair and selection across the panels of one group (bead czt.3). One instance per
// group, shared through context: a panel publishes the time under the pointer or its brush, and
// every other panel of the group draws them on its own x axis.
export const GROUP_GUTTER_PX = 88; // every grouped plot's y-axis gutter, so the x axes line up
export const GROUP_LABEL_PX = 22; // of which the rotated axis label

export class GroupLink {
  readonly id: string;
  readonly domain: [number, number];
  hoverMs = $state<number | null>(null);
  brush = $state<{ x0Ms: number; x1Ms: number; from: string } | null>(null);

  constructor(id: string, domain: [number, number]) {
    this.id = id;
    this.domain = domain;
  }

  hover(ms: number | null): void {
    this.hoverMs = ms;
  }

  select(x0Ms: number, x1Ms: number, from: string): void {
    this.brush = { x0Ms: Math.min(x0Ms, x1Ms), x1Ms: Math.max(x0Ms, x1Ms), from };
  }

  clear(from?: string): void {
    if (!from || this.brush?.from === from) this.brush = null;
  }
}
