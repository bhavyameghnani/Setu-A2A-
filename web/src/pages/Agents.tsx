import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { isAdmin, useAgents, useAgentStats, useDirectory, useViewer } from "../api/hooks";
import type { Agent, AgentStats } from "../api/types";
import { isAdminView } from "../api/types";
import { Icon } from "../components/icons";
import { Avatar, Btn, Card, Chip, Empty, ErrorNote, FilterSelect, Loading, PageTitle, SearchBox } from "../components/ui";
import { agentHealth, timeAgo } from "../lib/format";
import { href } from "../lib/router";
import { toast } from "../lib/toast";

const newAgentLink = (
  <a href={href.agentNew()} className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
    <Icon name="plus" className="h-4 w-4" /> New agent
  </a>
);

export function Agents() {
  const agents = useAgents();
  const stats = useAgentStats();
  const viewer = useViewer();
  const dir = useDirectory();
  const [q, setQ] = useState("");
  const [deptF, setDeptF] = useState("");
  const [transportF, setTransportF] = useState("");
  const [healthF, setHealthF] = useState("");
  const canRegister = viewer ? isAdmin(viewer.roles) : false;

  const healthOf = (a: Agent) =>
    a.transport === "a2a_remote" ? agentHealth(a.enabled, a.card_fetched_at).label : a.enabled ? "ok" : "disabled";
  const shown = (agents.data ?? []).filter(
    (a) =>
      `${a.handle} ${a.display_name} ${a.skills.map((s) => `${s.id} ${s.name} ${s.tags?.join(" ") ?? ""}`).join(" ")}`
        .toLowerCase()
        .includes(q.toLowerCase()) &&
      (!deptF || a.department_id === deptF) &&
      (!transportF || a.transport === transportF) &&
      (!healthF || healthOf(a) === healthF),
  );
  // Group by owning department, like Band groups by "This machine".
  const groups = new Map<string, Agent[]>();
  for (const a of shown) groups.set(a.department_id, [...(groups.get(a.department_id) ?? []), a]);
  const none = agents.data?.length === 0;

  return (
    <div>
      <PageTitle right={canRegister ? newAgentLink : undefined}>Agents</PageTitle>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <SearchBox label="Search agents" value={q} onChange={setQ} placeholder="Search agents by name, handle or skill…" />
        <FilterSelect label="Department" value={deptF} onChange={setDeptF} options={dir.departments.map((d) => [d.id, d.name])} />
        <FilterSelect label="Transport" value={transportF} onChange={setTransportF} options={[["a2a_remote", "A2A (card)"], ["sdk", "SDK"]]} />
        <FilterSelect label="Health" value={healthF} onChange={setHealthF} options={[["ok", "ok"], ["stale card", "stale card"], ["disabled", "disabled"], ["unknown", "unknown"]]} />
      </div>
      {!canRegister && viewer && <p className="mb-4 text-sm text-slate-500">Only department admins can register agents.</p>}
      {agents.isLoading && <Loading />}
      <ErrorNote error={agents.error} />
      {none && (
        <Empty title="Let’s get your first agent cracking" action={canRegister ? newAgentLink : undefined}>
          Register an existing A2A agent by its Agent Card URL, or connect one with the SDK. Setu never hosts your agent; it only connects it.
        </Empty>
      )}
      {agents.data && !none && shown.length === 0 && <Empty>No agents match these filters.</Empty>}
      {[...groups.entries()].map(([deptId, list]) => (
        <section key={deptId} className="mb-8">
          <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
            {dir.dept(deptId)} <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-600">{list.length}</span>
          </h2>
          <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {list.map((a) => (
              <AgentCard key={a.participant_id} a={a} deptName={dir.dept(a.department_id)} stats={stats.data?.get(a.participant_id)} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | undefined }) {
  return (
    <div>
      <p className="text-base font-semibold text-slate-900">{value ?? "—"}</p>
      <p className="text-xs text-slate-500">{label}</p>
    </div>
  );
}

function AgentCard({ a, deptName, stats }: { a: Agent; deptName: string; stats?: AgentStats }) {
  const qc = useQueryClient();
  const admin = isAdminView(a);
  // Health is about the Agent Card, so an SDK agent (which connects to Setu, with no card) has none.
  const health =
    a.transport === "a2a_remote"
      ? agentHealth(a.enabled, a.card_fetched_at)
      : { label: a.enabled ? "SDK" : "disabled", style: "bg-gray-200 text-gray-700" };
  const online = a.enabled && (a.transport !== "a2a_remote" || health.label === "ok");
  const refresh = useMutation({
    mutationFn: () => api<{ changed?: boolean }>(`/v1/agents/${a.participant_id}/refresh`, { body: {} }),
    onSuccess: (r) => {
      toast(r.changed ? `${a.display_name}: the card changed` : `${a.display_name}: the card is unchanged`);
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  const toggle = useMutation({
    mutationFn: () => api(`/v1/agents/${a.participant_id}`, { method: "PATCH", body: { enabled: !a.enabled } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });
  return (
    <li data-testid="agent" data-handle={a.handle}>
      <Card className="flex h-full flex-col">
        <div className="flex items-start gap-3">
          <Avatar name={a.display_name} size="lg" agent status={online ? "online" : "offline"} />
          <div className="min-w-0 flex-1">
            <h3 className="truncate font-medium text-slate-900">{a.display_name}</h3>
            <p className="truncate font-mono text-xs text-slate-500">@{a.handle}</p>
          </div>
          <Chip className={health.style}>{health.label}</Chip>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <Chip className="bg-slate-100 text-slate-700 ring-1 ring-slate-200">
            {a.transport === "a2a_remote" ? `A2A ${a.a2a_version ?? "?"}${a.streaming ? " · streaming" : ""}` : "SDK"}
          </Chip>
          <Chip className="bg-brand-50 text-brand-700 ring-1 ring-brand-200">{deptName}</Chip>
          {a.skills.length === 0 && <span className="text-xs text-slate-500">No skills advertised.</span>}
          {a.skills.map((s) => (
            <Chip key={s.id} className="bg-blue-50 text-blue-800">
              <span title={s.description ?? s.name}>{s.id}</span>
            </Chip>
          ))}
        </div>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-slate-600">
          <dt className="text-slate-400">Owner</dt>
          <dd>{deptName}</dd>
          {a.transport === "a2a_remote" && (
            <>
              <dt className="text-slate-400">Card fetched</dt>
              <dd>
                {a.card_fetched_at ? timeAgo(a.card_fetched_at) : "never"}
                {a.protocol_versions.length > 1 && ` · card lists ${a.protocol_versions.join(", ")}`}
              </dd>
            </>
          )}
          {admin && a.rpc_url && (
            <>
              <dt className="text-slate-400">Endpoint</dt>
              <dd className="break-all font-mono">{a.rpc_url}</dd>
            </>
          )}
          {admin && a.transport === "a2a_remote" && (
            <>
              <dt className="text-slate-400">Credentials</dt>
              <dd>{a.has_credentials ? "stored (encrypted)" : "none"}</dd>
            </>
          )}
        </dl>
        <div className="mt-auto">
          <div className="mt-4 grid grid-cols-4 gap-2 border-t border-slate-100 pt-3">
            <Stat label="Messages" value={stats?.messages} />
            <Stat label="Tasks" value={stats?.tasks} />
            <Stat label="Errors" value={stats?.failed} />
            <Stat label="Rooms" value={stats?.rooms} />
          </div>
          {admin && a.transport === "a2a_remote" && (
            <div className="mt-3 flex gap-2">
              <Btn disabled={refresh.isPending} onClick={() => refresh.mutate()}>Refresh card</Btn>
              <Btn tone={a.enabled ? "danger" : "plain"} disabled={toggle.isPending} onClick={() => toggle.mutate()}>
                {a.enabled ? "Disable" : "Enable"}
              </Btn>
            </div>
          )}
          <ErrorNote error={refresh.error ?? toggle.error} />
        </div>
      </Card>
    </li>
  );
}
