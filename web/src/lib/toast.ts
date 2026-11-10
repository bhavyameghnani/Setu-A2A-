// The smallest possible toast: a DOM event, so any code (including WebSocket handlers) can raise one.
export type ToastDetail = { text: string; kind: "info" | "error" };
export const toast = (text: string, kind: ToastDetail["kind"] = "info") =>
  window.dispatchEvent(new CustomEvent<ToastDetail>("setu:toast", { detail: { text, kind } }));
