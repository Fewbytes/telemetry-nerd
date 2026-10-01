/** DOM-side attention helpers shared by reference chips and the highlight strip. */

const el = (domId: string | null) => (domId ? document.getElementById(domId) : null);

export const hoverOn = (domId: string | null) => el(domId)?.classList.add("ref-hover");
export const hoverOff = (domId: string | null) => el(domId)?.classList.remove("ref-hover");

/** Scroll to the element and flash it once. */
export function flash(domId: string | null): void {
  const target = el(domId);
  if (!target) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.remove("ref-flash");
  void target.offsetWidth; // restart the animation on repeat clicks
  target.classList.add("ref-flash");
  target.addEventListener("animationend", () => target.classList.remove("ref-flash"), { once: true });
}

/** Bring the element into view only if it is not already (used once per Claude highlight). */
export function scrollIfOffscreen(domId: string | null): void {
  const target = el(domId);
  if (!target) return;
  const r = target.getBoundingClientRect();
  if (r.top >= 0 && r.bottom <= window.innerHeight) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
}

/** Mark highlighted elements; `author` drives the accent colour. */
export function syncHighlightClasses(entries: { domId: string | null; author: string }[]): void {
  for (const old of document.querySelectorAll(".highlighted")) {
    old.classList.remove("highlighted");
    old.removeAttribute("data-highlight-author");
  }
  for (const { domId, author } of entries) {
    const target = el(domId);
    if (!target) continue;
    target.classList.add("highlighted");
    target.setAttribute("data-highlight-author", author);
  }
}
