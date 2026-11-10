import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api } from "../api/client";
import { isOversight, useViewer } from "../api/hooks";
import type { AuditEvent, AuditVerify } from "../api/types";
import { Icon } from "../components/icons";
import { Btn, Chip, Empty, ErrorNote, Loading, PageTitle, SearchBox, Who } from "../components/ui";
import { clock } from "../lib/format";

const PAGE = 50;

/** A color per family of event, so denials and approvals stand out when scanning the log. */
function eventTone(type: string): string {
  if (type.includes("deny") || type.includes("denied")) return "bg-red-100 text-red-800";
  if (type.startsWith("approval")) return "bg-amber-100 text-amber-800";
  if (type.startsWith("policy")) return "bg-orange-100 text-orange-800";
  if (type.startsWith("delegation")) return "bg-teal-100 text-teal-800";
  if (type.startsWith("agent")) return "bg-violet-100 text-violet-800";
  if (type.startsWith("task")) return "bg-brand-100 text-brand-800";
  return "bg-slate-100 text-slate-700";
}

export function Audit() {
  const viewer = useViewer();
  const log = useInfiniteQuery({
    queryKey: ["audit"],
    queryFn: ({ pageParam }) => api<AuditEvent[]>("/v1/audit", { query: { after_seq: pageParam, limit: PAGE } }),
    initialPageParam: 0,
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].seq : undefined),
    enabled: viewer ? isOversight(viewer.roles) : false,
  });
  const verify = useMutation({ mutationFn: () => api<AuditVerify>("/v1/audit/verify", { body: {} }) });
  const [open, setOpen] = useState<number | null>(null);
  const [q, setQ] = useState("");

  if (viewer && !isOversight(viewer.roles)) return <Empty>The audit log is for auditors and org admins.</Empty>;
  const all = log.data?.pages.flat() ?? [];
  const events = all.filter((e) => !q || `${e.event_type} ${JSON.stringify(e.payload)}`.toLowerCase().includes(q.toLowerCase()));
  const v = verify.data;

  return (
    <div className="mx-auto max-w-4xl">
      <PageTitle right={<Btn tone="primary" disabled={verify.isPending} onClick={() => verify.mutate()}>Verify chain</Btn>}>
        Audit log
      </PageTitle>
      <p className="mb-4 flex items-center gap-2 text-sm text-slate-500">
        <Icon name="lock" className="h-4 w-4" /> Append-only and hash-chained: each event carries the hash of the one before it, so any edit breaks the chain.
      </p>
      {v && (
        <p
          data-testid="verify-result"
          role="status"
          className={`mb-4 flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-medium ${v.ok ? "bg-green-50 text-green-800 ring-1 ring-green-200" : "bg-red-50 text-red-800 ring-1 ring-red-200"}`}
        >
          {v.ok
            ? `Intact: ${v.checked} events, every link matches.`
            : `Tampering detected: the chain first breaks at event #${v.first_broken_seq} (of ${v.checked}).`}
        </p>
      )}
      <div className="mb-3"><SearchBox label="Filter events" value={q} onChange={setQ} placeholder="Filter loaded events by type or content…" /></div>
      <ErrorNote error={verify.error ?? log.error} />
      {log.isLoading && <Loading />}
      {!log.isLoading && events.length === 0 && <Empty title={q ? "No matching events" : "No events yet"}>{q ? "Nothing loaded matches that filter." : "Events appear here as people and agents act."}</Empty>}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr><th className="px-3 py-2">#</th><th className="px-3 py-2">When</th><th className="px-3 py-2">Event</th><th className="px-3 py-2">Actor</th></tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <Fragment key={e.seq}>
                <tr className="cursor-pointer border-b border-slate-100 hover:bg-slate-50" onClick={() => setOpen(open === e.seq ? null : e.seq)} data-testid="audit-row">
                  <td className="px-3 py-1.5 text-slate-500">{e.seq}</td>
                  <td className="px-3 py-1.5 text-slate-600">{clock(e.created_at)}</td>
                  <td className="px-3 py-1.5"><Chip className={eventTone(e.event_type)}>{e.event_type}</Chip></td>
                  <td className="px-3 py-1.5">{e.actor_id ? <Who id={e.actor_id} /> : "—"}</td>
                </tr>
                {open === e.seq && (
                  <tr className="border-b border-slate-100 bg-slate-50">
                    <td colSpan={4} className="px-3 py-2">
                      <pre className="overflow-x-auto rounded-lg border border-slate-200 bg-white p-3 font-mono text-xs">{JSON.stringify(e.payload, null, 2)}</pre>
                      <p className="mt-1 break-all text-[11px] text-slate-500">hash {e.hash}<br />prev {e.prev_hash}</p>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {log.hasNextPage && <Btn className="mt-4" onClick={() => log.fetchNextPage()} disabled={log.isFetchingNextPage}>Load more</Btn>}
    </div>
  );
}
