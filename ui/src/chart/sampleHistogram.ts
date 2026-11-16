export interface SampleBar { x: number; y: number; width: number; height: number; lo: number; hi: number; count: number }

/** Bars of one compared column over the shared bin edges. Every column is scaled to the same
 *  tallest count, so equal heights mean equal counts across columns (n differs: counts, not shares). */
export function layoutSampleBars(edges: number[], counts: number[], yMax: number, width: number, height: number): SampleBar[] {
  const span = edges[edges.length - 1] - edges[0];
  return counts.map((count, i) => {
    const x = span > 0 ? ((edges[i] - edges[0]) / span) * width : 0;
    const right = span > 0 ? ((edges[i + 1] - edges[0]) / span) * width : width;
    const h = yMax > 0 ? (count / yMax) * height : 0;
    return { x, y: height - h, width: Math.max(right - x - 1, 1), height: h, lo: edges[i], hi: edges[i + 1], count };
  });
}

export const sampleBarTip = (b: SampleBar): string => `${b.lo} to ${b.hi}: ${b.count}`;
