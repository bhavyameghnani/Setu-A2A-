// One fetch wrapper for the whole app: bearer token, JSON in/out, readable errors, and a single
// place that reacts to an expired session.

const TOKEN_KEY = "setu.token";

export const session = {
  token(): string | null {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null; // storage can be blocked (private windows): the app then works per page load
    }
  },
  set(token: string): void {
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* keep going: the token is also held in memory below */
    }
    memory = token;
  },
  clear(): void {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to remove */
    }
    memory = null;
  },
  current(): string | null {
    return memory ?? this.token();
  },
};
let memory: string | null = null;

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

/** FastAPI's `detail` is a string, or a list of validation problems. */
export function detailOf(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((e) => {
        const item = e as { loc?: unknown[]; msg?: string };
        const field = (item.loc ?? []).filter((x) => x !== "body").join(".");
        return field ? `${field}: ${item.msg}` : (item.msg ?? "invalid");
      })
      .join("; ");
  }
  return fallback;
}

type Options = { method?: string; body?: unknown; query?: Record<string, string | number | boolean | undefined> };

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = {};
  const token = session.current();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(url, {
    method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const text = await resp.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    if (resp.status === 401 && token) {
      session.clear();
      window.dispatchEvent(new Event("setu:signed-out"));
    }
    throw new ApiError(resp.status, detailOf(data, resp.statusText));
  }
  return data as T;
}

export const errorText = (e: unknown): string =>
  e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
