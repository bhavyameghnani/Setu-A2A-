// A hash router in a few lines: no dependency, back/forward work, and links are plain `<a href>`.
import { useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "rooms" }
  | { name: "room"; id: string }
  | { name: "inbox" }
  | { name: "agents" }
  | { name: "agent-new" }
  | { name: "policies" }
  | { name: "traces"; id?: string }
  | { name: "audit" }
  | { name: "teams" }
  | { name: "analytics" }
  | { name: "contacts" }
  | { name: "notfound" };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  switch (parts[0]) {
    case undefined:
    case "home":
      return { name: "home" };
    case "rooms":
      return parts[1] ? { name: "room", id: parts[1] } : { name: "rooms" };
    case "inbox":
      return { name: "inbox" };
    case "agents":
      return parts[1] === "new" ? { name: "agent-new" } : { name: "agents" };
    case "policies":
      return { name: "policies" };
    case "traces":
      return { name: "traces", id: parts[1] };
    case "audit":
      return { name: "audit" };
    case "teams":
      return { name: "teams" };
    case "analytics":
      return { name: "analytics" };
    case "contacts":
      return { name: "contacts" };
    default:
      return { name: "notfound" };
  }
}

export const href = {
  home: () => "#/home",
  teams: () => "#/teams",
  analytics: () => "#/analytics",
  contacts: () => "#/contacts",
  rooms: () => "#/rooms",
  room: (id: string) => `#/rooms/${id}`,
  inbox: () => "#/inbox",
  agents: () => "#/agents",
  agentNew: () => "#/agents/new",
  policies: () => "#/policies",
  trace: (id: string) => `#/traces/${id}`,
  traces: () => "#/traces",
  audit: () => "#/audit",
};

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export const navigate = (to: string) => {
  window.location.hash = to;
};
