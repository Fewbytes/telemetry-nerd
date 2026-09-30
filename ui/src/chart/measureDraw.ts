// uPlot schedules its first draw via queueMicrotask, so timing `new uPlot()` only measures
// setup. `construct` must register `onDraw` as a uPlot `draw` hook; the elapsed time is
// reported once, when the first draw actually completes.
export function measureFirstDraw<T>(
  construct: (onDraw: () => void) => T,
  report: (ms: number) => void,
  now: () => number = () => performance.now(),
): T {
  const t0 = now();
  let reported = false;
  return construct(() => {
    if (reported) return;
    reported = true;
    report(now() - t0);
  });
}
