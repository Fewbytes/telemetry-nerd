import { tick } from "svelte";

/** Focus an element once the DOM has re-rendered; `find` runs after the update, so bindings are current. */
export async function focusAfterRender(find: () => HTMLElement | null | undefined): Promise<void> {
  await tick();
  find()?.focus();
}
