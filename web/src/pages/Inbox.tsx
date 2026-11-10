import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { Notification, NotificationList } from "../api/types";
import { Decide } from "../components/Decide";
import { Icon, type IconName } from "../components/icons";
import { Btn, Card, Chip, DeptBadge, Empty, ErrorNote, inputClass, Loading, PageTitle } from "../components/ui";
import { dayLabel, label, sameDay, timeAgo } from "../lib/format";
import { href } from "../lib/router";
import { toast } from "../lib/toast";

export function Inbox() {
  const qc = useQueryClient();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const inbox = useInfiniteQuery({
    queryKey: ["inbox", unreadOnly],
    queryFn: ({ pageParam }) =>
      api<NotificationList>("/v1/notifications", {
        query: { limit: 20, before: pageParam, unread_only: unreadOnly || undefined },
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const readAll = useMutation({
    mutationFn: () => api<{ marked: number }>("/v1/notifications/read-all", { body: {} }),
    onSuccess: () => {
      for (const k of ["inbox", "unread"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
  const items = inbox.data?.pages.flatMap((p) => p.items) ?? [];
  const unread = inbox.data?.pages[0]?.unread ?? 0;

  return (
    <div className="mx-auto max-w-3xl">
      <PageTitle
        right={
          <div className="flex items-center gap-3 text-sm">
            <label className={`flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 ring-1 ${unreadOnly ? "bg-brand-50 text-brand-800 ring-brand-200" : "bg-white text-slate-700 ring-slate-300"}`}>
              <input type="checkbox" className="accent-[#2c6f8f]" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
              Unread only
            </label>
            <Btn disabled={unread === 0 || readAll.isPending} onClick={() => readAll.mutate()}>
              Mark all read
            </Btn>
          </div>
        }
      >
        Inbox{" "}
        {unread > 0 && (
          <span data-testid="inbox-unread" className="ml-2 rounded-full bg-red-600 px-2 py-0.5 align-middle text-xs font-medium text-white">
            {unread} unread
          </span>
        )}
      </PageTitle>
      {inbox.isLoading && <Loading />}
      <ErrorNote error={inbox.error} />
      {!inbox.isLoading && items.length === 0 && (
        <Empty title={unreadOnly ? "Nothing unread" : "All quiet"}>
          {unreadOnly ? "Nothing unread." : "Nothing here yet. You will be told when your department's agents start, finish or need you."}
        </Empty>
      )}
      <ul className="space-y-3">
        {items.map((n, i) => (
          <li key={n.id} className="list-none">
            {(i === 0 || !sameDay(items[i - 1].created_at, n.created_at)) && (
              <div className="mb-2 mt-3 flex items-center gap-3 text-xs font-medium text-slate-400">
                {dayLabel(n.created_at)} <span className="h-px flex-1 bg-slate-200" />
              </div>
            )}
            <Row n={n} />
          </li>
        ))}
      </ul>
      {inbox.hasNextPage && (
        <Btn className="mt-4" onClick={() => inbox.fetchNextPage()} disabled={inbox.isFetchingNextPage}>
          Load older
        </Btn>
      )}
    </div>
  );
}

/** An icon and color for the kind of event, so the list can be scanned. */
function kindOf(event: string): { icon: IconName; tone: string } {
  if (event.includes("failed") || event.includes("denied") || event.includes("rejected")) return { icon: "alert", tone: "bg-red-100 text-red-700" };
  if (event.includes("approval") || event.includes("input_required")) return { icon: "policies", tone: "bg-amber-100 text-amber-700" };
  if (event.includes("completed") || event.includes("approved")) return { icon: "check", tone: "bg-emerald-100 text-emerald-700" };
  if (event.includes("started")) return { icon: "play", tone: "bg-brand-100 text-brand-700" };
  if (event.includes("agent")) return { icon: "agents", tone: "bg-violet-100 text-violet-700" };
  return { icon: "bell", tone: "bg-slate-100 text-slate-600" };
}

function Row({ n }: { n: Notification }) {
  const qc = useQueryClient();
  const [replying, setReplying] = useState(false);
  const [text, setText] = useState("");
  const refresh = () => {
    for (const k of ["inbox", "unread", "tasks"]) qc.invalidateQueries({ queryKey: [k] });
  };
  const read = useMutation({
    mutationFn: () => api<Notification>(`/v1/notifications/${n.id}/read`, { body: {} }),
    onSuccess: refresh,
  });
  const reply = useMutation({
    mutationFn: () => api(`/v1/notifications/${n.id}/reply`, { body: { text } }),
    onSuccess: () => {
      setText("");
      setReplying(false);
      toast("Reply sent");
      refresh();
    },
  });
  const cancel = useMutation({
    mutationFn: () => api(`/v1/tasks/${n.task_id}/cancel`, { body: {} }),
    onSuccess: () => {
      toast("Task canceled");
      refresh();
    },
  });
  const a = n.actions;
  const unread = n.read_at === null;
  const k = kindOf(n.event_type);
  return (
    <div>
      <Card className={unread ? "border-brand-200 bg-brand-50/40" : "opacity-80"}>
        <div data-testid="notification" data-event={n.event_type} data-unread={unread} className="flex gap-3">
          <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${k.tone}`}>
            <Icon name={k.icon} />
          </span>
          <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <p className={`text-sm ${unread ? "font-semibold text-slate-900" : "text-slate-700"}`}>
              {unread && <span className="mr-2 inline-block h-2 w-2 rounded-full bg-brand-600 align-middle" aria-label="unread" />}
              {n.summary}
            </p>
            <time className="shrink-0 text-xs text-slate-500">{timeAgo(n.created_at)}</time>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
            <Chip className="bg-slate-100 text-slate-700">{label(n.event_type)}</Chip>
            <DeptBadge id={n.department_id} />
            {a?.open_room && n.room_id && <a className="font-medium text-brand-700 hover:underline" href={href.room(n.room_id)}>Open room</a>}
            {n.trace_id && <a className="font-medium text-brand-700 hover:underline" href={href.trace(n.trace_id)}>View trace</a>}
            {a?.reply && <button className="font-medium text-brand-700 hover:underline" onClick={() => setReplying(!replying)}>Reply</button>}
            {a?.cancel_task && (
              <button className="font-medium text-red-700 hover:underline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
                Cancel task
              </button>
            )}
            {unread && (
              <button className="ml-auto text-slate-500 hover:text-slate-900 hover:underline" onClick={() => read.mutate()}>
                Mark read
              </button>
            )}
          </div>

          {a?.approval_id && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3">
              <p className="text-sm font-medium text-amber-900">Your decision is needed</p>
              <Decide approvalId={a.approval_id} />
            </div>
          )}

          {replying && (
            <form
              className="mt-3 flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                reply.mutate();
              }}
            >
              <input aria-label="Reply" className={inputClass} value={text} onChange={(e) => setText(e.target.value)} placeholder="Reply in the room" autoFocus />
              <Btn tone="primary" type="submit" disabled={!text.trim() || reply.isPending}>Send</Btn>
            </form>
          )}
          <ErrorNote error={reply.error ?? cancel.error ?? read.error} />
          </div>
        </div>
      </Card>
    </div>
  );
}
