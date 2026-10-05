export interface Viewport {
  start_ms: number;
  end_ms: number;
}

/** Viewport fully covered by what's already been fetched, within `toleranceMs` (a few ms/one
 * step of slack for a "now"-anchored viewport resolved a moment after the data was fetched —
 * see `nowAnchor`). Without it (and without `nowAnchor`), this was effectively unreachable:
 * presets/typed ranges resolved against `Date.now()`, which only moves forward, so by the time
 * a click landed "now" was already past the fetched end_ms. */
export const isInBounds = (
  viewport: Viewport,
  fetched: { start_ms: number; end_ms: number },
  toleranceMs = 0,
): boolean =>
  viewport.start_ms >= fetched.start_ms && viewport.end_ms <= fetched.end_ms + toleranceMs;

/** What a preset/typed "now"-relative range resolves against. Anchoring to wall-clock time
 * unconditionally is why isInBounds almost never fired: "now" always runs ahead of whatever was
 * last fetched. Anchoring to the panel's already-fetched end instead means a preset no wider
 * than what's loaded lands in bounds (no round trip, just a client-side window of the data in
 * hand); only a genuinely wider or older range still needs the server. Falls back to wall-clock
 * time when nothing has been fetched yet. */
export const nowAnchor = (fetchedEndMs: number | null | undefined, clockNowMs: number): number =>
  fetchedEndMs != null && fetchedEndMs > 0 ? fetchedEndMs : clockNowMs;

const PRESET_MS: Record<string, number> = {
  "15m": 15 * 60_000,
  "1h": 3600_000,
  "6h": 6 * 3600_000,
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
};

export const presetRange = (preset: keyof typeof PRESET_MS, now_ms: number): Viewport => ({
  start_ms: now_ms - PRESET_MS[preset],
  end_ms: now_ms,
});

const RELATIVE = /^now-(\d+)(s|m|h|d)$/;
const UNIT_MS: Record<string, number> = { s: 1000, m: 60_000, h: 3600_000, d: 86_400_000 };

export const parseRelative = (text: string, now_ms: number): Viewport | null => {
  const m = RELATIVE.exec(text.trim());
  if (!m) return null;
  return { start_ms: now_ms - Number(m[1]) * UNIT_MS[m[2]], end_ms: now_ms };
};
