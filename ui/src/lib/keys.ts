// Shared send-key affordance for text boxes: Cmd/Ctrl+Enter submits, plain
// Enter keeps its default (newline in a textarea). Used by the Ask-Claude
// menu, the thread reply box, and the hypothesis note input.

export function isSendKey(e: KeyboardEvent): boolean {
  return e.key === "Enter" && (e.metaKey || e.ctrlKey);
}

export function sendHint(platform: string = navigator.platform ?? ""): string {
  return /Mac|iPhone|iPad/i.test(platform) ? "⌘⏎ to send" : "Ctrl+⏎ to send";
}