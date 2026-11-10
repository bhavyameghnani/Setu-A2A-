import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { isAdmin, useAgents, useDirectory, useMe, usePendingApprovals, useViewer } from "../api/hooks";
import type { Analytics, Room } from "../api/types";
import { Icon, type IconName } from "../components/icons";
import { Avatar, Card, Empty, Loading, PageTitle } from "../components/ui";
import { agentHealth, timeAgo } from "../lib/format";
import { href } from "../lib/router";

/** The landing page: what needs attention, how the agents are doing, and where to go next. */
export function Home() {
  const me = useMe();
  const viewer = useViewer();
  const dir = useDirectory();
  const agents = useAgents();
  const approvals = usePendingApprovals();
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => api<Room[]>("/v1/rooms") });
  const week = useQuery({
    queryKey: ["analytics", 7],
    queryFn: () => api<Analytics>("/v1/analytics/summary", { query: { days: 7 } }),
    refetchInterval: 20_000,
  });

  const list = agents.data ?? [];
  const online = list.filter((a) => a.enabled && (a.transport !== "a2a_remote" || agentHealth(a.enabled, a.card_fetched_at).label === "ok"));
  const pending = approvals.data ?? [];
  const w = week.data;
  const name = me.data?.participant.display_name.split(" ")[0];

  return (
    <div>
      <PageTitle>{name ? `Welcome back, ${name}` : "Home"}</PageTitle>
      <div className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Tile to={href.rooms()} icon="tasks" label="Active tasks" value={w ? String(w.active) : "…"} hint="running right now" />
        <Tile to={href.inbox()} icon="inbox" label="Waiting on a decision" value={approvals.data ? String(pending.length) : "…"} hint={pending.length ? "needs approval" : "all clear"} tone={pending.length ? "amber" : undefined} />
        <Tile to={href.agents()} icon="agents" label="Agents online" value={agents.data ? `${online.length}/${list.length}` : "…"} hint="enabled, card fresh" />
        <Tile to={href.analytics()} icon="analytics" label="Success rate (7d)" value={w ? (w.success_rate === null ? "—" : `${Math.round(w.success_rate * 100)}%`) : "…"} hint={w ? `${w.completed} done, ${w.failed} failed` : ""} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Needs your attention</h2>
          {approvals.isLoading && <Loading />}
          {approvals.data && pending.length === 0 && <Empty>You’re all caught up. Nothing is waiting on you.</Empty>}
          <ul className="space-y-2">
            {pending.slice(0, 5).map((a) => (
              <li key={a.id}>
                <a href={href.inbox()}>
                  <Card className="flex items-start gap-3 hover:border-brand-200">
                    <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-amber-100 text-amber-700"><Icon name="policies" /></span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-slate-900">{a.summary}</p>
                      <p className="text-xs text-slate-500">{timeAgo(a.created_at)}{a.approver_department_id ? ` · ${dir.dept(a.approver_department_id)}` : ""}</p>
                    </div>
                  </Card>
                </a>
              </li>
            ))}
          </ul>

          <h2 className="mb-2 mt-6 text-sm font-semibold text-slate-700">Recent rooms</h2>
          {rooms.isLoading && <Loading />}
          {rooms.data?.length === 0 && (
            <Empty title="No rooms yet" action={<a href={href.rooms()} className="rounded-lg bg-brand-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-brand-700">Open Rooms</a>}>
              Rooms are where people and agents work together.
            </Empty>
          )}
          <ul className="space-y-2">
            {(rooms.data ?? []).slice(0, 5).map((r) => (
              <li key={r.id}>
                <a href={href.room(r.id)}>
                  <Card className="flex items-center justify-between gap-3 hover:border-brand-200">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-slate-900">{r.name}</p>
                      <p className="truncate text-xs text-slate-500">{r.topic ? `${r.topic} · ` : ""}{r.last_seq} messages</p>
                    </div>
                    <span className="shrink-0 text-xs text-slate-400">{timeAgo(r.created_at)}</span>
                  </Card>
                </a>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Agents</h2>
          {agents.isLoading && <Loading />}
          {agents.data && list.length === 0 && (
            <Empty title="Let’s get your first agent cracking" action={viewer && isAdmin(viewer.roles) ? <a href={href.agentNew()} className="rounded-lg bg-brand-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-brand-700">New agent</a> : undefined}>
              Register an A2A agent by its Agent Card URL.
            </Empty>
          )}
          <ul className="space-y-2">
            {list.slice(0, 6).map((a) => {
              const ok = a.enabled && (a.transport !== "a2a_remote" || agentHealth(a.enabled, a.card_fetched_at).label === "ok");
              return (
                <li key={a.participant_id}>
                  <a href={href.agents()}>
                    <Card className="flex items-center gap-3 hover:border-brand-200">
                      <Avatar name={a.display_name} agent status={ok ? "online" : "offline"} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-slate-900">{a.display_name}</p>
                        <p className="truncate font-mono text-xs text-slate-500">@{a.handle}</p>
                      </div>
                      <span className="text-xs text-slate-500">{dir.dept(a.department_id)}</span>
                    </Card>
                  </a>
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </div>
  );
}

function Tile({ to, icon, label, value, hint, tone }: { to: string; icon: IconName; label: string; value: string; hint: string; tone?: "amber" }) {
  return (
    <a href={to}>
      <Card className="h-full hover:border-brand-200">
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <span className={`flex h-7 w-7 items-center justify-center rounded-lg ${tone === "amber" ? "bg-amber-100 text-amber-700" : "bg-brand-50 text-brand-700"}`}><Icon name={icon} /></span>
          {label}
        </div>
        <p className="mt-3 text-3xl font-semibold tracking-tight text-slate-900">{value}</p>
        <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
      </Card>
    </a>
  );
}
