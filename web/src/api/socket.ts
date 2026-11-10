// A WebSocket that reconnects with backoff. Browsers cannot set headers on a WebSocket, so the
// token goes in the query string (the server accepts `?token=`).
import { session } from "./client";

export type SocketHandle = { close: () => void };

type Options<F> = {
  /** Query string for each (re)connect; called again every time, so it can resume from state. */
  query?: () => Record<string, string | number | undefined>;
  onFrame: (frame: F) => void;
  onStatus?: (open: boolean) => void;
  /** Close codes after which retrying is pointless (not signed in, no access). */
  fatal?: number[];
};

export function openSocket<F>(path: string, o: Options<F>): SocketHandle {
  let ws: WebSocket | null = null;
  let stopped = false;
  let delay = 500;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const connect = () => {
    const params = new URLSearchParams();
    const token = session.current();
    if (token) params.set("token", token);
    for (const [k, v] of Object.entries(o.query?.() ?? {})) if (v !== undefined) params.set(k, String(v));
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${scheme}://${window.location.host}${path}?${params}`);
    ws.onopen = () => {
      delay = 500;
      o.onStatus?.(true);
    };
    ws.onmessage = (ev) => {
      try {
        o.onFrame(JSON.parse(ev.data as string) as F);
      } catch {
        /* ignore a malformed frame */
      }
    };
    ws.onclose = (ev) => {
      o.onStatus?.(false);
      if (stopped || (o.fatal ?? [4401, 4403, 4404]).includes(ev.code)) return;
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 10_000);
    };
  };
  connect();
  return {
    close: () => {
      stopped = true;
      clearTimeout(timer);
      ws?.close();
    },
  };
}
