import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useDirectory } from "../api/hooks";
import type { Delegation, Span, Trace } from "../api/types";
import { Icon, type IconName } from "../components/icons";
import { Btn, Card, Chip, Empty, ErrorNote, inputClass, Loading, PageTitle, StateChip, Who } from "../components/ui";
import { duration } from "../lib/format";
import { href, navigate } from "../lib/router";

// One look per kind of step: chip color, an icon, and the accent on the card's left edge.
const KIND: Record<string, { chip: string; icon: IconName; edge: string; word: string }> = {
  message: { chip: "bg-slate-200 text-slate-800", icon: "rooms", edge: "border-l-slate-400", word: "message" },
  a2a_call: { chip: "bg-violet-100 text-violet-800", icon: "agents", edge: "border-l-violet-500", word: "agent call" },
  policy: { chip: "bg-amber-100 text-amber-800", icon: "policies", edge: "border-l-amber-500", word: "policy check" },
  delegation: { chip: "bg-teal-100 text-teal-800", icon: "teams", edge: "border-l-teal-500", word: "delegation" },
  approval: { chip: "bg-orange-100 text-orange-800", icon: "check", edge: "border-l-orange-500", word: "approval" },
};
const look = (kind: string) => KIND[kind] ?? { chip: "bg-slate-100 text-slate-700", icon: "traces" as IconName, edge: "border-l-slate-300", word: kind };

function flat(spans: Span[]): Span[] {
  return spans.flatMap((s) => [s, ...flat(s.children ?? [])]);
}

export function TraceView({ id }: { id?: string }) {
  const [typed, setTyped] = useState(id ?? "");
  const trace = useQuery({
    queryKey: ["trace", id],
    queryFn: () => api<Trace>(`/v1/traces/${id}`),
    enabled: Boolean(id),
    retry: false,
  });
  return (
    <div className="mx-auto max-w-4xl">
      <PageTitle>Trace</PageTitle>
      <form
        className="mb-4 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (typed.trim()) navigate(href.trace(typed.trim()));
        }}
      >
        <div className="relative flex-1">
          <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input aria-label="Trace id" className={`${inputClass} pl-9 font-mono`} placeholder="Trace id (from a task card or notification)" value={typed} onChange={(e) => setTyped(e.target.value)} />
        </div>
        <Btn type="submit" tone="primary">Open</Btn>
      </form>
      {!id && (
        <Empty title="Follow one request through the system">
          Open a trace from a task (“View trace”) or from an inbox notification.
        </Empty>
      )}
      {trace.isLoading && id && <Loading />}
      <ErrorNote error={trace.error} />
      {trace.data && (
        <>
          <p className="mb-2 text-sm text-slate-600" data-testid="trace-summary">
            {trace.data.span_count} steps in trace <code className="rounded bg-slate-100 px-1">{trace.data.trace_id}</code>
            {trace.data.hidden_spans > 0 && (
              <> · <span className="text-amber-700">{trace.data.hidden_spans} more are in rooms your department cannot see</span></>
            )}
          </p>
          <div className="mb-4 flex flex-wrap gap-2" aria-label="Steps by kind">
            {Object.entries(
              flat(trace.data.roots).reduce<Record<string, number>>((acc, s) => ({ ...acc, [s.kind]: (acc[s.kind] ?? 0) + 1 }), {}),
            ).map(([kind, n]) => (
              <span key={kind} className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium ${look(kind).chip}`}>
                <Icon name={look(kind).icon} className="h-3.5 w-3.5" /> {n} {look(kind).word}{n === 1 ? "" : "s"}
              </span>
            ))}
          </div>
          <ul className="space-y-2" data-testid="trace-tree">
            {trace.data.roots.map((s) => <Node key={s.id} span={s} />)}
          </ul>
        </>
      )}
    </div>
  );
}

function attr<T = string>(s: Span, key: string): T | undefined {
  return (s.attributes as Record<string, unknown>)[key] as T | undefined;
}

function Node({ span: s }: { span: Span }) {
  const dir = useDirectory();
  return (
    <li data-testid="span" data-kind={s.kind}>
      <Card className={`border-l-4 py-3 ${look(s.kind).edge}`}>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Chip className={look(s.kind).chip}>{s.kind.replace("_", " ")}</Chip>
          <span className="font-medium">{s.name}</span>
          <StateChip state={s.status} />
          {s.duration_ms !== null && s.duration_ms !== undefined && (
            <span className="text-xs text-slate-500">{duration(s.duration_ms)}</span>
          )}
          {s.actor_id && <span className="ml-auto text-xs"><Who id={s.actor_id} /></span>}
        </div>
        <Details s={s} dept={dir.dept} />
      </Card>
      {(s.children ?? []).length > 0 && (
        <ul className="ml-6 mt-2 space-y-2 border-l-2 border-dashed border-slate-300 pl-5">
          {(s.children ?? []).map((c) => <Node key={c.id} span={c} />)}
        </ul>
      )}
    </li>
  );
}

function Details({ s, dept }: { s: Span; dept: (id: string) => string }) {
  switch (s.kind) {
    case "policy": {
      const decision = attr(s, "decision");
      return (
        <p className="mt-1.5 text-xs text-slate-700">
          <StateChip state={String(decision)} /> {attr(s, "reason")}
          {attr(s, "from_department") && attr(s, "to_department") && (
            <> · {dept(attr(s, "from_department")!)} → {dept(attr(s, "to_department")!)}</>
          )}
        </p>
      );
    }
    case "delegation":
      return <DelegationDetails s={s} />;
    case "approval":
      return (
        <p className="mt-1.5 text-xs text-slate-700">
          needs a <b>{attr(s, "approver_role")}</b> of {attr(s, "approver_department") ? dept(attr(s, "approver_department")!) : "?"}
          {attr(s, "note") && <> · “{attr(s, "note")}”</>}
        </p>
      );
    case "a2a_call":
      return (
        <p className="mt-1.5 text-xs text-slate-700">
          {attr(s, "a2a_version") && <>A2A {attr(s, "a2a_version")}</>}
          {attr<boolean>(s, "resumed") && " · resumed after an answer"}
          {attr(s, "error") && <span className="text-red-700"> · {attr(s, "error")}</span>}
        </p>
      );
    default: {
      const skills = attr<string[]>(s, "skills") ?? [];
      return skills.length > 0 ? <p className="mt-1.5 text-xs text-slate-700">skills: {skills.join(", ")}</p> : null;
    }
  }
}

/** The delegation behind a hop: who granted what to whom, back to the human at the root. */
function DelegationDetails({ s }: { s: Span }) {
  const id = attr(s, "delegation_id");
  const needed = attr<string[]>(s, "needed") ?? [];
  const reason = attr(s, "reason");
  const [open, setOpen] = useState(false);
  const chain = useQuery({
    queryKey: ["delegation-chain", id],
    queryFn: () => api<Delegation[]>(`/v1/delegations/${id}/chain`),
    enabled: open && Boolean(id),
    retry: false,
  });
  const root = attr(s, "root_human");
  return (
    <div className="mt-1.5 text-xs text-slate-700">
      <p>
        needs <b>{needed.join(", ")}</b>
        {reason && <span className="text-red-700"> · {reason}</span>}
        {root && <> · acting for <Who id={root} /></>}
      </p>
      {id && (
        <button className="mt-1 font-medium text-brand-700 hover:underline" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"} delegation chain
        </button>
      )}
      {open && chain.isLoading && <span> loading…</span>}
      {open && chain.error && <p className="mt-1 text-slate-500">The chain is not visible to you.</p>}
      {chain.data && (
        <ol className="mt-1 space-y-1" data-testid="delegation-chain">
          {[...chain.data].reverse().map((d) => (
            <li key={d.id}>
              <Who id={d.from_participant_id} /> → <Who id={d.to_participant_id} />{" "}
              <span className="text-slate-500">[{d.scopes.join(", ")}]</span>
              {d.revoked_at && <Chip className="ml-1 bg-red-100 text-red-800">revoked</Chip>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
