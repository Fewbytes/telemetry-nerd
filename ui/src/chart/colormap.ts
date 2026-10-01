// Perceptually uniform colormaps only (spec §6.3). 17 anchors sampled from matplotlib at
// t = 0, 1/16, …, 1 (generator in plan Task 6 step 1); linear interpolation between them.
export type ColormapName = "viridis" | "cividis";

const ANCHORS: Record<ColormapName, string[]> = {
  viridis: ['#440154', '#48186a', '#472d7b', '#424086', '#3b528b', '#33638d', '#2c728e', '#26828e', '#21918c', '#1fa088', '#28ae80', '#3fbc73', '#5ec962', '#84d44b', '#addc30', '#d8e219', '#fde725'],
  cividis: ['#00224e', '#002e6a', '#1a386f', '#32436d', '#434e6c', '#535a6d', '#61656f', '#6f7073', '#7d7c78', '#8c8878', '#9b9476', '#aba072', '#bcae6c', '#cdbb63', '#dec958', '#f0d846', '#fee838'],
};

const rgb = (hex: string): number[] => {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

export function colormap(name: ColormapName): (t: number) => string {
  const pts = ANCHORS[name].map(rgb);
  return (t: number) => {
    const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * (pts.length - 1);
    const i = Math.min(pts.length - 2, Math.floor(x));
    const f = x - i;
    const a = pts[i], b = pts[i + 1];
    return `rgb(${a.map((v, j) => Math.round(v + (b[j] - v) * f)).join(",")})`;
  };
}

/** WCAG relative luminance of "rgb(r,g,b)" or "#rrggbb". */
export function relativeLuminance(color: string): number {
  const m = color.match(/\d+/g);
  const [r, g, b] = color.startsWith("#") ? rgb(color) : (m ?? []).slice(0, 3).map(Number);
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
