export interface Viewport {
  start_ms: number;
  end_ms: number;
}

export const isInBounds = (
  viewport: Viewport,
  fetched: { start_ms: number; end_ms: number },
): boolean => viewport.start_ms >= fetched.start_ms && viewport.end_ms <= fetched.end_ms;

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
