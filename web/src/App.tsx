import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, session } from "./api/client";
import { isOversight, useMe, useViewer } from "./api/hooks";
import { openSocket } from "./api/socket";
import type { Notification } from "./api/types";
import { Icon, type IconName } from "./components/icons";
import { Avatar, Chip, DeptBadge, Toasts } from "./components/ui";
import { toast } from "./lib/toast";
import { href, useRoute } from "./lib/router";
import { AgentNew } from "./pages/AgentNew";
import { Analytics } from "./pages/Analytics";
import { Contacts } from "./pages/Contacts";
import { Home } from "./pages/Home";
import { Teams } from "./pages/Teams";
import { Agents } from "./pages/Agents";
import { Audit } from "./pages/Audit";
import { Inbox } from "./pages/Inbox";
import { Login } from "./pages/Login";
import { Policies } from "./pages/Policies";
import { RoomsLayout } from "./pages/Rooms";
import { TraceView } from "./pages/Trace";

type PushFrame =
  | { type: "unread"; unread: number }
  | { type: "notification"; notification: Notification; unread: number };

export default function App() {
  const [signedIn, setSignedIn] = useState(() => Boolean(session.current()));
  const qc = useQueryClient();
  useEffect(() => {
    const out = () => {
      qc.clear();
      setSignedIn(false);
    };
    window.addEventListener("setu:signed-out", out);
    return () => window.removeEventListener("setu:signed-out", out);
  }, [qc]);

  if (!signedIn) return <><Login onSignedIn={() => { qc.clear(); setSignedIn(true); }} /><Toasts /></>;
  return <Shell />;
}

function Shell() {
  const route = useRoute();
  const qc = useQueryClient();
  const me = useMe();
  const viewer = useViewer();
  const unread = useQuery({
    queryKey: ["unread"],
    queryFn: () => api<{ unread: number }>("/v1/notifications/unread-count"),
    refetchInterval: 30_000,
  });

  // Live inbox: new notifications update the badge, refresh the inbox and raise a toast.
  useEffect(() => {
    const sock = openSocket<PushFrame>("/v1/ws/notifications", {
      onFrame: (f) => {
        qc.setQueryData(["unread"], { unread: f.unread });
        if (f.type === "notification") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          qc.invalidateQueries({ queryKey: ["approvals"] });
          toast(f.notification.summary);
        }
      },
    });
    return () => sock.close();
  }, [qc]);

  const inRooms = route.name === "rooms" || route.name === "room";
  type Item = { to: string; text: string; icon: IconName; active: boolean };
  const main: Item[] = [
    { to: href.home(), text: "Home", icon: "home", active: route.name === "home" },
    { to: href.rooms(), text: "Rooms", icon: "rooms", active: route.name === "rooms" || route.name === "room" },
    { to: href.inbox(), text: "Inbox", icon: "inbox", active: route.name === "inbox" },
    { to: href.agents(), text: "Agents", icon: "agents", active: route.name === "agents" || route.name === "agent-new" },
    { to: href.teams(), text: "Teams", icon: "teams", active: route.name === "teams" },
    { to: href.analytics(), text: "Analytics", icon: "analytics", active: route.name === "analytics" },
  ];
  const directory: Item[] = [{ to: href.contacts(), text: "Contacts", icon: "contacts", active: route.name === "contacts" }];
  const governance: Item[] = [
    { to: href.policies(), text: "Policies", icon: "policies", active: route.name === "policies" },
    { to: href.traces(), text: "Traces", icon: "traces", active: route.name === "traces" },
  ];
  if (viewer && isOversight(viewer.roles))
    governance.push({ to: href.audit(), text: "Audit", icon: "audit", active: route.name === "audit" });

  const NavLink = ({ to, text, icon, active }: Item) => (
    <a
      href={to}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
        active ? "bg-brand-100 font-medium text-brand-800" : "text-slate-600 hover:bg-slate-200/60 hover:text-slate-900"
      }`}
    >
      <Icon name={icon} />
      <span>{text}</span>
      {text === "Inbox" && (unread.data?.unread ?? 0) > 0 && (
        <span data-testid="unread-badge" className="ml-auto rounded-full bg-red-600 px-1.5 py-0.5 text-xs font-semibold text-white">
          {unread.data?.unread}
        </span>
      )}
    </a>
  );

  return (
    <div className="min-h-screen bg-canvas text-slate-800 md:flex">
      <nav
        aria-label="Main"
        className="flex shrink-0 flex-col gap-1 border-b border-slate-200 bg-sidebar p-3 md:sticky md:top-0 md:h-screen md:w-60 md:border-b-0 md:border-r"
      >
        <a href={href.rooms()} className="mb-3 flex items-center gap-2 px-3 py-2 text-lg font-semibold tracking-tight text-slate-900">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">S</span>
          Setu
        </a>
        <div className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible">
          {main.map((i) => <NavLink key={i.to} {...i} />)}
        </div>
        <p className="mb-1 mt-4 flex items-center gap-2 px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Directory <span className="h-px flex-1 bg-slate-200" />
        </p>
        <div className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible">
          {directory.map((i) => <NavLink key={i.to} {...i} />)}
        </div>
        <p className="mb-1 mt-4 flex items-center gap-2 px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Governance <span className="h-px flex-1 bg-slate-200" />
        </p>
        <div className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible">
          {governance.map((i) => <NavLink key={i.to} {...i} />)}
        </div>
        <div className="mt-auto hidden border-t border-slate-200 pt-3 md:block">
          {me.data && (
            <div className="mb-2 flex items-center gap-2.5 px-2">
              <Avatar name={me.data.participant.display_name} status="online" />
              <div className="min-w-0">
                <p data-testid="whoami" className="truncate text-sm font-medium text-slate-900">{me.data.participant.display_name}</p>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  <DeptBadge id={me.data.participant.department_id} />
                  {me.data.roles.map((r) => (
                    <Chip key={r} className="bg-slate-200/70 text-slate-600">{r.replace("_", " ")}</Chip>
                  ))}
                </div>
              </div>
            </div>
          )}
          <button
            className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-200/60 hover:text-slate-900"
            onClick={() => {
              session.clear();
              window.dispatchEvent(new Event("setu:signed-out"));
            }}
          >
            <Icon name="logout" />
            Sign out
          </button>
        </div>
      </nav>
      {inRooms ? (
        // Rooms is a full-height workspace (list | conversation | participants), not a padded page.
        <main className="min-w-0 flex-1">
          <RoomsLayout activeId={route.name === "room" ? route.id : undefined} />
        </main>
      ) : (
        <main className="min-w-0 flex-1 px-6 py-6 md:px-10">
          <div className="mx-auto max-w-6xl">
            {route.name === "home" && <Home />}
            {route.name === "inbox" && <Inbox />}
            {route.name === "teams" && <Teams />}
            {route.name === "analytics" && <Analytics />}
            {route.name === "contacts" && <Contacts />}
            {route.name === "agents" && <Agents />}
            {route.name === "agent-new" && <AgentNew />}
            {route.name === "policies" && <Policies />}
            {route.name === "traces" && <TraceView id={route.id} />}
            {route.name === "audit" && <Audit />}
            {route.name === "notfound" && <p>Page not found.</p>}
          </div>
        </main>
      )}
      <Toasts />
    </div>
  );
}
