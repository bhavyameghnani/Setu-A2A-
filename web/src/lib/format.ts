export function timeAgo(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** The date divider above a day of messages: "Today", "Yesterday", or "Sun, Oct 18". */
export function dayLabel(iso: string, now = Date.now()): string {
  const d = new Date(iso);
  const day = (t: Date) => new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const diff = Math.round((day(new Date(now)) - day(d)) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

export const sameDay = (a: string, b: string) => new Date(a).toDateString() === new Date(b).toDateString();

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const LIVE_STATES = ["submitted", "working", "input_required", "auth_required"];
export const isLive = (state: string) => LIVE_STATES.includes(state);

/** Tailwind classes for a task/span/approval state chip. */
export function stateStyle(state: string): string {
  switch (state) {
    case "completed":
    case "ok":
    case "approved":
    case "allow":
      return "bg-green-100 text-green-800";
    case "failed":
    case "rejected":
    case "denied":
    case "deny":
      return "bg-red-100 text-red-800";
    case "canceled":
      return "bg-gray-200 text-gray-700";
    case "input_required":
    case "auth_required":
    case "pending":
    case "require_approval":
      return "bg-amber-100 text-amber-800";
    default:
      return "bg-blue-100 text-blue-800"; // submitted, working
  }
}

export const label = (s: string) => s.replace(/_/g, " ");

/** Agent health, derived: the hub refreshes every card hourly, so a much older fetch means
 *  the card could not be reached. (There is no health endpoint.) */
export function agentHealth(
  enabled: boolean,
  fetchedAt: string | null | undefined,
  now = Date.now(),
): { label: string; style: string } {
  if (!enabled) return { label: "disabled", style: "bg-gray-200 text-gray-700" };
  if (!fetchedAt) return { label: "unknown", style: "bg-gray-200 text-gray-700" };
  const age = now - new Date(fetchedAt).getTime();
  return age > 2 * 3600_000
    ? { label: "stale card", style: "bg-amber-100 text-amber-800" }
    : { label: "ok", style: "bg-green-100 text-green-800" };
}
