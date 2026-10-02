<script lang="ts">
  // A hover tooltip positioned by `tipAt`/`placeTip` (chart/plotKit): `flip` puts it left of the pointer, `flipY` above.
  import type { HoverTip } from "../chart/plotKit";

  let { tip, ...rest }: { tip: HoverTip; [attr: string]: unknown } = $props();
</script>

<div class="chart-tip" class:flip={tip.flip} class:flipY={tip.flipY} style="left:{tip.x}px;top:{tip.y}px" {...rest}>{tip.text}</div>

<style>
  .chart-tip {
    position: absolute; white-space: pre; font-size: 11px; background: var(--fg); color: var(--bg);
    padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5;
  }
  .chart-tip.flip { transform: translateX(-100%); }
  .chart-tip.flipY { transform: translateY(-100%); }
  .chart-tip.flip.flipY { transform: translate(-100%, -100%); }
</style>
