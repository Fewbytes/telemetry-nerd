<script lang="ts">
  // A hover tooltip positioned by `placeTip`/`tipAt` (chart/plotKit): it sits beside the pointer
  // (`flip`/`flipY` pick the side) and is clamped, by its measured size, inside its positioned parent.
  import { clampTip, type HoverTip } from "../chart/plotKit";

  let { tip, class: cls = "", ...rest }: { tip: HoverTip; class?: string; [attr: string]: unknown } = $props();
  let el = $state<HTMLDivElement | null>(null);
  let tw = $state(0), th = $state(0);
  let box = $state({ w: Infinity, h: Infinity });
  $effect(() => {
    void tip; void tw; void th; // re-measure the parent whenever the tip moves or resizes
    const p = el?.offsetParent;
    if (p) box = { w: p.clientWidth, h: p.clientHeight };
  });
  const pos = $derived(clampTip(tip, tw, th, box.w, box.h));
</script>

<div bind:this={el} bind:clientWidth={tw} bind:clientHeight={th} class="chart-tip {cls}"
  style="left:{pos.left}px;top:{pos.top}px" {...rest}>{tip.text}</div>

<style>
  .chart-tip {
    position: absolute; white-space: pre-wrap; max-width: calc(100% - 8px); font-size: 11px; background: var(--fg);
    color: var(--bg); padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5;
  }
</style>
