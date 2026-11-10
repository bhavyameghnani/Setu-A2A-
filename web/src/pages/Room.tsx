import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { isAdmin, useAgents, useDirectory, usePendingApprovals, useViewer } from "../api/hooks";
import { openSocket } from "../api/socket";
import type { Approval, Member, Message, Room as RoomT, Task } from "../api/types";
import { Decide } from "../components/Decide";
import { Icon } from "../components/icons";
import {
  Avatar, Btn, Chip, DeptBadge, Empty, ErrorNote, Field, inputClass, Loading, Parts, StateChip, toneOf, Who,
} from "../components/ui";
import { canDecide } from "../lib/approvals";
import { agentHealth, clock, dayLabel, isLive, sameDay } from "../lib/format";
import { activeMention, insertMention, mentionedIds, splitMentions, suggestions, type Who as Person } from "../lib/mentions";
import { verifySignature, type Jwks } from "../lib/signature";
import { href } from "../lib/router";
import { toast } from "../lib/toast";

type Frame = { type: "message"; message: Message } | { type: "error"; detail: string } | { type: string };
const TASK_KINDS = ["task", "task_update", "result", "approval_request", "approval_response"];
type Tab = "work" | "chat";

export function Room({ id }: { id: string }) {
  const qc = useQueryClient();
  const viewer = useViewer();
  const dir = useDirectory();
  const agents = useAgents();
  const room = useQuery({ queryKey: ["room", id], queryFn: () => api<RoomT>(`/v1/rooms/${id}`) });
  const members = useQuery({ queryKey: ["members", id], queryFn: () => api<Member[]>(`/v1/rooms/${id}/members`) });
  const tasks = useQuery({
    queryKey: ["tasks", id],
    queryFn: () => api<Task[]>(`/v1/rooms/${id}/tasks`),
    refetchInterval: 4000,
  });
  const approvals = usePendingApprovals();

  const [messages, setMessages] = useState<Message[]>([]);
  const [live, setLive] = useState(false);
  const [tab, setTab] = useState<Tab>("chat");
  const [panel, setPanel] = useState(true);
  const [adding, setAdding] = useState(false);
  const lastSeq = useRef(0);

  // One WebSocket: it replays the room from the start, then stays live, and on a reconnect it
  // resumes from the last message seen, so nothing is missed or shown twice.
  // Only once the room itself loaded: for a room this person cannot see, the server refuses the
  // WebSocket handshake with a plain HTTP 403, which a browser reports as an anonymous failure
  // (close code 1006), so the socket would retry forever.
  const visible = room.isSuccess;
  useEffect(() => {
    if (!visible) return;
    setMessages([]);
    lastSeq.current = 0;
    const sock = openSocket<Frame>(`/v1/ws/rooms/${id}`, {
      query: () => ({ after_seq: lastSeq.current }),
      onStatus: setLive,
      onFrame: (f) => {
        if (f.type !== "message") return;
        const m = (f as { message: Message }).message;
        lastSeq.current = Math.max(lastSeq.current, m.seq);
        setMessages((cur) => (cur.some((x) => x.id === m.id) ? cur : [...cur, m].sort((a, b) => a.seq - b.seq)));
        if (m.task_id || TASK_KINDS.includes(m.kind)) {
          qc.invalidateQueries({ queryKey: ["tasks", id] });
          qc.invalidateQueries({ queryKey: ["approvals"] });
        }
      },
    });
    return () => sock.close();
  }, [id, qc, visible]);

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // A block body on purpose: an effect must not return what scrollIntoView returns (a Promise
    // in current Chromium), because React would treat it as the cleanup function.
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, tab]);

  const people: Person[] = useMemo(
    () =>
      (members.data ?? []).flatMap((m) => {
        const p = dir.person(m.participant_id);
        return p ? [{ id: p.id, handle: p.handle }] : [];
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [members.data, dir.ready],
  );
  const byMessage = useMemo(() => new Map(messages.map((m) => [m.id, m])), [messages]);

  if (room.error) return <div className="p-6"><ErrorNote error={room.error} /></div>;
  if (!room.data) return <div className="p-6"><Loading /></div>;
  const r = room.data;
  const canManage = viewer ? r.created_by === viewer.id || isAdmin(viewer.roles) : false;
  const taskList = tasks.data ?? [];
  const liveTasks = taskList.filter((t) => isLive(t.state));
  const agentCount = (members.data ?? []).filter((m) => dir.person(m.participant_id)?.kind === "agent").length;
  const lastUpdate = (taskId: string) => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const b = messages[i].body as { task_id?: string; text?: string };
      if (messages[i].kind === "task_update" && b.task_id === taskId && b.text) return b.text;
    }
    return "";
  };

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-slate-200 px-4 py-2.5">
          <a href={href.rooms()} className="text-slate-500 hover:text-slate-900 md:hidden" aria-label="Back to rooms">
            <Icon name="back" />
          </a>
          <div role="tablist" aria-label="Room view" className="inline-flex rounded-lg bg-slate-100 p-0.5 text-sm">
            {(["work", "chat"] as const).map((t) => (
              <button
                key={t}
                role="tab"
                aria-selected={tab === t}
                onClick={() => setTab(t)}
                className={`rounded-md px-3 py-1 font-medium capitalize ${tab === t ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-800"}`}
              >
                {t}
                {t === "work" && liveTasks.length > 0 && (
                  <span className="ml-1.5 rounded-full bg-brand-600 px-1.5 text-xs text-white">{liveTasks.length}</span>
                )}
              </button>
            ))}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-slate-900">{r.name}</h1>
            <div className="hidden items-center gap-1.5 sm:flex">
              <Chip className="bg-slate-100 text-slate-600">{r.visibility.replace("_", "-")}</Chip>
              <DeptBadge id={r.owning_department_id} />
              {r.topic && <span className="truncate text-xs text-slate-500">{r.topic}</span>}
            </div>
          </div>
          <span data-testid="live" className={`whitespace-nowrap text-xs ${live ? "text-emerald-700" : "text-amber-700"}`}>
            ● {live ? "live" : "reconnecting…"}
          </span>
          <button
            aria-label="Participants"
            aria-pressed={panel}
            onClick={() => setPanel((p) => !p)}
            className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm ${panel ? "bg-slate-100 text-slate-900" : "text-slate-600 hover:bg-slate-100"}`}
          >
            <Icon name="users" /> {(members.data ?? []).length}
          </button>
        </header>

        {/* Both views stay mounted so a task card or a message is never lost by switching tabs. */}
        <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-4 ${tab === "chat" ? "" : "hidden"}`} data-testid="messages" data-panel="chat">
          {messages.length === 0 && agentCount === 0 && (
            <Empty
              title="No agents in this room"
              action={canManage ? <Btn tone="primary" onClick={() => { setPanel(true); setAdding(true); }}>Add an agent</Btn> : undefined}
            >
              Add an agent to get started.
            </Empty>
          )}
          {messages.length === 0 && agentCount > 0 && (
            <Empty title="Nothing here yet">Tag an agent with @ and describe the task.</Empty>
          )}
          <div className="space-y-4">
            {messages.map((m, i) => (
              <div key={m.id}>
                {(i === 0 || !sameDay(messages[i - 1].created_at, m.created_at)) && <DayDivider iso={m.created_at} />}
                <MessageView m={m} people={people} tasks={taskList} approvals={approvals.data ?? []} />
              </div>
            ))}
          </div>
          <div ref={endRef} />
        </div>

        <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-4 ${tab === "work" ? "" : "hidden"}`} data-panel="work">
          {taskList.length === 0 && (
            <Empty title="No tasks yet">Tag an agent in Chat and send a task. Its progress and result show up here.</Empty>
          )}
          <ul className="mx-auto max-w-2xl space-y-3">
            {[...taskList]
              .sort((a, b) => Number(isLive(b.state)) - Number(isLive(a.state)))
              .map((t) => (
                <TaskCard key={t.id} task={t} request={t.request_message_id ? byMessage.get(t.request_message_id) : undefined} approvals={approvals.data ?? []} />
              ))}
          </ul>
        </div>

        <div className="px-5 pb-4">
          {liveTasks.length > 0 && <WorkingBar tasks={liveTasks} lastUpdate={lastUpdate} />}
          <Composer roomId={id} people={people} />
        </div>
      </div>

      {panel && (
        <aside className="hidden w-72 shrink-0 overflow-y-auto border-l border-slate-200 bg-white lg:block">
          <ParticipantsPanel
            room={r}
            members={members.data ?? []}
            canManage={canManage}
            adding={adding}
            setAdding={setAdding}
            liveTasks={liveTasks}
            health={new Map((agents.data ?? []).map((a) => [a.participant_id, a.transport !== "a2a_remote" ? a.enabled : a.enabled && agentHealth(a.enabled, a.card_fetched_at).label === "ok"]))}
          />
        </aside>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- messages

function DayDivider({ iso }: { iso: string }) {
  return (
    <div className="my-1 flex items-center gap-3 text-xs text-slate-400">
      <span className="h-px flex-1 bg-slate-200" />
      {dayLabel(iso)}
      <span className="h-px flex-1 bg-slate-200" />
    </div>
  );
}

/** Text with @mentions as colored pills, one color per person (same as their avatar). */
function Mentions({ text, people }: { text: string; people: Person[] }) {
  const dir = useDirectory();
  return (
    <p className="whitespace-pre-wrap break-words">
      {splitMentions(text, people).map((p, i) =>
        p.mention ? (
          <span
            key={i}
            className={`rounded-md px-1 py-0.5 font-medium ${toneOf(dir.person(p.mention.id)?.display_name ?? p.mention.handle)}`}
            data-mention={p.mention.id}
          >
            {p.text}
          </span>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </p>
  );
}

// A result is signed by Setu when it is stored. The badge is green only if this browser checked
// the signature against Setu's published key and the content still matches.
function SignedBadge({ token, parts }: { token: string; parts: unknown }) {
  const jwks = useQuery({
    queryKey: ["jwks"],
    queryFn: async () => (await fetch("/.well-known/jwks.json")).json() as Promise<Jwks>,
    staleTime: 5 * 60_000,
  });
  const check = useQuery({
    queryKey: ["signature", token],
    enabled: !!jwks.data,
    queryFn: () => verifySignature(token, parts, jwks.data as Jwks),
    staleTime: Infinity,
  });
  const s = check.data;
  const [text, cls, title] = !s
    ? ["signed", "bg-slate-100 text-slate-600", "Checking the signature…"]
    : s.state === "verified"
      ? ["✓ signed by Setu", "bg-green-100 text-green-800", `Verified against Setu's key. Setu attests this agent returned exactly this result in task ${s.claims.task}.`]
      : s.state === "invalid"
        ? ["✗ signature invalid", "bg-red-100 text-red-800", s.reason]
        : ["signed (unchecked)", "bg-amber-100 text-amber-800", s.reason];
  return (
    <div className="mt-1">
      <Chip className={cls}>
        <span data-signature={s?.state ?? "pending"} title={title}>
          {text}
        </span>
      </Chip>
    </div>
  );
}

function MessageView({ m, people, tasks, approvals }: { m: Message; people: Person[]; tasks: Task[]; approvals: Approval[] }) {
  const viewer = useViewer();
  const dir = useDirectory();
  const b = m.body as Record<string, unknown>;
  const text = typeof b.text === "string" ? b.text : "";
  const quiet = m.kind === "activity" || m.kind === "system";
  const sender = dir.person(m.sender_id);

  let body: React.ReactNode;
  switch (m.kind) {
    case "task":
      body = (
        <>
          <Mentions text={text} people={people} />
          {typeof b.skill === "string" && <Chip className="mt-1 bg-blue-50 text-blue-800">skill {b.skill}</Chip>}
        </>
      );
      break;
    case "task_update":
      body = (
        <div className="flex flex-wrap items-center gap-2">
          <StateChip state={String(b.state)} />
          {(typeof b.error === "string" || text) && <span className="text-slate-600">{String(b.error ?? text)}</span>}
        </div>
      );
      break;
    case "result":
      body = (
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
          {Array.isArray(b.parts) ? <Parts parts={b.parts as Record<string, unknown>[]} /> : <Mentions text={text} people={people} />}
          {typeof b.signature === "string" && <SignedBadge token={b.signature} parts={b.parts} />}
        </div>
      );
      break;
    case "approval_request": {
      const a = approvals.find((x) => x.id === b.approval_id);
      const requester = a ? tasks.find((t) => t.id === a.task_id)?.requester_id : undefined;
      body = (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <p>{String(b.prompt ?? text)}</p>
          {a && viewer && canDecide(a, viewer, requester) && <Decide approvalId={a.id} />}
          {a && !(viewer && canDecide(a, viewer, requester)) && (
            <p className="mt-1 text-xs text-amber-700">Waiting for a decision from the approver department.</p>
          )}
        </div>
      );
      break;
    }
    case "approval_response":
      body = (
        <p>
          <Chip className={b.approved ? "bg-green-100 text-green-800" : "bg-red-100 text-red-800"}>
            {b.approved ? "approved" : "denied"}
          </Chip>{" "}
          {typeof b.note === "string" && b.note}
        </p>
      );
      break;
    default:
      body = <Mentions text={text || JSON.stringify(b)} people={people} />;
  }

  if (quiet) {
    return (
      <article data-kind={m.kind} className="text-center text-xs text-slate-500">
        <span className="font-medium">{sender?.display_name ?? "…"}</span> {body}
      </article>
    );
  }
  return (
    <article data-kind={m.kind} className="flex gap-3 text-sm">
      <Avatar name={sender?.display_name ?? "?"} agent={sender?.kind === "agent"} />
      <div className="min-w-0 flex-1">
        <header className="mb-0.5 flex flex-wrap items-baseline gap-2">
          <span className="font-semibold text-slate-900">{sender?.display_name ?? "…"}</span>
          {sender?.kind === "agent" && <Chip className="bg-violet-100 text-violet-800">agent</Chip>}
          {m.kind !== "text" && <Chip className="bg-slate-100 text-slate-600">{m.kind.replace("_", " ")}</Chip>}
          <time className="text-xs text-slate-400">{clock(m.created_at)}</time>
        </header>
        <div className="text-slate-800">{body}</div>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------- live work

/** "Working · 2 agents": who is busy right now, with their latest status line. */
function WorkingBar({ tasks, lastUpdate }: { tasks: Task[]; lastUpdate: (id: string) => string }) {
  const dir = useDirectory();
  const [open, setOpen] = useState(false);
  const who = [...new Set(tasks.map((t) => t.assignee_id))];
  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-brand-200 bg-brand-50" data-testid="working-bar">
      <button className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="flex gap-0.5 text-brand-600" aria-hidden="true">
          <span className="animate-pulse">●</span><span className="animate-pulse [animation-delay:150ms]">●</span><span className="animate-pulse [animation-delay:300ms]">●</span>
        </span>
        <span className="flex -space-x-1">
          {who.slice(0, 4).map((id) => <Avatar key={id} name={dir.person(id)?.display_name ?? "?"} size="sm" agent />)}
        </span>
        <span className="font-medium text-brand-800">Working</span>
        <span className="ml-auto text-xs text-brand-700">{who.length} agent{who.length > 1 ? "s" : ""}</span>
        <Icon name="chevron" className={`h-4 w-4 text-brand-700 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <ul className="divide-y divide-brand-100 border-t border-brand-100 bg-white/60 px-3 text-sm">
          {tasks.map((t) => (
            <li key={t.id} className="flex items-center gap-2 py-1.5">
              <span className="font-medium">{dir.person(t.assignee_id)?.display_name ?? "…"}</span>
              <StateChip state={t.state} />
              <span className="truncate text-slate-500">{lastUpdate(t.id)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- tasks

function TaskCard({ task, request, approvals }: { task: Task; request?: Message; approvals: Approval[] }) {
  const qc = useQueryClient();
  const viewer = useViewer();
  const cancel = useMutation({
    mutationFn: () => api<Task>(`/v1/tasks/${task.id}/cancel`, { body: {} }),
    onSuccess: () => {
      toast("Task canceled");
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
  const pending = approvals.find((a) => a.task_id === task.id);
  const skill = (request?.body as { skill?: string } | undefined)?.skill;
  return (
    <li className="rounded-xl border border-slate-200 bg-white p-4 text-sm shadow-card" data-testid="task-card" data-state={task.state}>
      <div className="mb-1 flex items-center justify-between">
        <StateChip state={task.state} />
        {skill && <Chip className="bg-blue-50 text-blue-800">{skill}</Chip>}
      </div>
      <div className="space-y-0.5 text-xs text-slate-600">
        <div>from <Who id={task.requester_id} /></div>
        <div>to <Who id={task.assignee_id} /></div>
      </div>
      {task.error && <p className="mt-1 text-xs text-red-700">{task.error}</p>}
      {task.artifacts?.map((a) => (
        <div key={a.id} className="mt-2 rounded-lg bg-slate-50 p-2">
          <p className="mb-1 text-xs font-medium text-slate-500">{a.name ?? "result"}</p>
          <Parts parts={a.parts as Record<string, unknown>[]} />
        </div>
      ))}
      {pending && viewer && canDecide(pending, viewer, task.requester_id) && (
        <div className="mt-2 border-t border-slate-100 pt-2">
          <p className="text-xs text-amber-800">{pending.summary}</p>
          <Decide approvalId={pending.id} summary={pending.summary} />
        </div>
      )}
      <div className="mt-2 flex items-center gap-3 text-xs">
        {task.trace_id && <a className="text-brand-700 underline" href={href.trace(task.trace_id)}>View trace</a>}
        {isLive(task.state) && (
          <button className="text-red-700 underline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            Cancel
          </button>
        )}
      </div>
      <ErrorNote error={cancel.error} />
    </li>
  );
}

// ---------------------------------------------------------------- composer

function Composer({ roomId, people }: { roomId: string; people: Person[] }) {
  const qc = useQueryClient();
  const agents = useAgents();
  const ref = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [kind, setKind] = useState<"text" | "task">("text");
  const [skill, setSkill] = useState("");
  const [pick, setPick] = useState(0);

  const mention = activeMention(text, caret);
  const options = mention ? suggestions(people, mention.query) : [];
  const mentions = mentionedIds(text, people);
  const skillOptions = (agents.data ?? [])
    .filter((a) => (mentions.length ? mentions.includes(a.participant_id) : true))
    .flatMap((a) => a.skills.map((s) => s.id));

  const send = useMutation({
    mutationFn: () =>
      api<Message>(`/v1/rooms/${roomId}/messages`, {
        body: { kind, body: { text, ...(kind === "task" && skill ? { skill } : {}) }, mentions },
      }),
    onSuccess: () => {
      setText("");
      setSkill("");
      qc.invalidateQueries({ queryKey: ["tasks", roomId] });
    },
  });

  const choose = (p: Person) => {
    if (!mention) return;
    const r = insertMention(text, mention.start, caret, p.handle);
    setText(r.text);
    setCaret(r.caret);
    setPick(0);
    requestAnimationFrame(() => {
      ref.current?.focus();
      // Only if nothing was typed meanwhile: otherwise the caret would jump back into the text.
      if (ref.current?.value === r.text) ref.current.setSelectionRange(r.caret, r.caret);
    });
  };
  const startMention = () => {
    const at = ref.current?.selectionStart ?? text.length;
    const lead = at > 0 && !/\s/.test(text[at - 1]) ? " " : "";
    setText(`${text.slice(0, at)}${lead}@${text.slice(at)}`);
    const next = at + lead.length + 1;
    setCaret(next);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(next, next);
    });
  };
  const submit = () => text.trim() && !send.isPending && send.mutate();

  return (
    <form
      className="rounded-xl border border-slate-300 bg-white shadow-sm focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-brand-100"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="relative">
        <textarea
          ref={ref}
          aria-label="Message"
          className="h-20 w-full resize-none rounded-t-xl bg-transparent px-3 pt-3 text-sm placeholder:text-slate-400 focus:outline-none"
          placeholder="Tag an agent with @ and describe the task…"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setCaret(e.target.selectionStart);
            setPick(0);
          }}
          onKeyUp={(e) => setCaret(e.currentTarget.selectionStart)}
          onClick={(e) => setCaret(e.currentTarget.selectionStart)}
          onKeyDown={(e) => {
            if (options.length) {
              if (e.key === "ArrowDown") { e.preventDefault(); setPick((pick + 1) % options.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setPick((pick - 1 + options.length) % options.length); return; }
              if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); choose(options[pick]); return; }
              if (e.key === "Escape") { setCaret(0); return; }
            }
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
        />
        {options.length > 0 && (
          <ul role="listbox" aria-label="Mention suggestions" className="absolute bottom-full left-0 z-10 mb-1 w-64 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg">
            {options.map((p, i) => (
              <li key={p.id} role="option" aria-selected={i === pick}>
                <button
                  type="button"
                  className={`flex w-full items-center justify-between px-2 py-1.5 text-left text-sm ${i === pick ? "bg-slate-100" : ""}`}
                  onMouseDown={(e) => { e.preventDefault(); choose(p); }}
                >
                  <span>@{p.handle}</span>
                  <Who id={p.id} className="text-xs" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3 px-3 pb-2.5 pt-1">
        <button type="button" aria-label="Mention someone" onClick={startMention} className="rounded-md p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-800">
          <Icon name="at" />
        </button>
        <select aria-label="Send as" className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs" value={kind} onChange={(e) => setKind(e.target.value as "text" | "task")}>
          <option value="text">Message</option>
          <option value="task">Task for mentioned agent</option>
        </select>
        {kind === "task" && (
          <>
            <input aria-label="Skill" list="skills" className="w-44 rounded-md border border-slate-200 px-2 py-1 text-xs" placeholder="skill, e.g. risk.score" value={skill} onChange={(e) => setSkill(e.target.value)} />
            <datalist id="skills">{[...new Set(skillOptions)].map((s) => <option key={s} value={s} />)}</datalist>
          </>
        )}
        <span className="text-xs text-slate-500">
          {mentions.length > 0 ? `Addressing ${mentions.length}` : "No one addressed"}
        </span>
        <button
          type="submit"
          aria-label="Send"
          title="Send"
          disabled={send.isPending || !text.trim()}
          className="ml-auto rounded-lg bg-brand-600 p-2 text-white shadow-sm hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Icon name="send" />
        </button>
      </div>
      <div className="px-3 pb-2"><ErrorNote error={send.error} /></div>
    </form>
  );
}

// ---------------------------------------------------------------- participants

function ParticipantsPanel({
  room, members, canManage, adding, setAdding, liveTasks, health,
}: {
  room: RoomT;
  members: Member[];
  canManage: boolean;
  adding: boolean;
  setAdding: (v: boolean) => void;
  liveTasks: Task[];
  health: Map<string, boolean>;
}) {
  const dir = useDirectory();
  const busy = new Set(liveTasks.map((t) => t.assignee_id));
  const humans = members.filter((m) => dir.person(m.participant_id)?.kind !== "agent");
  const bots = members.filter((m) => dir.person(m.participant_id)?.kind === "agent");

  const Row = ({ m }: { m: Member }) => {
    const p = dir.person(m.participant_id);
    const agent = p?.kind === "agent";
    return (
      <li className="flex items-center gap-2.5 py-1.5 text-sm">
        <Avatar name={p?.display_name ?? "?"} agent={agent} status={agent ? (health.get(m.participant_id) ? "online" : "offline") : undefined} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-slate-900">{p?.display_name ?? "…"}</span>
            {agent && <Chip className="bg-violet-100 text-violet-800">agent</Chip>}
            {m.role === "owner" && <span title="Owner" className="text-amber-500"><Icon name="crown" className="h-4 w-4" /></span>}
            {m.role === "observer" && <Chip className="bg-slate-100 text-slate-600">observer</Chip>}
          </div>
          {agent && (
            <p className={`truncate text-xs ${busy.has(m.participant_id) ? "text-brand-600" : "font-mono text-slate-400"}`}>
              {busy.has(m.participant_id) ? "thinking…" : `@${p?.handle}`}
            </p>
          )}
          <p className="truncate text-xs text-slate-400">{dir.dept(p?.department_id)}</p>
        </div>
      </li>
    );
  };

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold text-slate-900">
          Participants <span className="ml-1 text-sm font-normal text-slate-400">{members.length}</span>
        </h2>
        {canManage && (
          <button aria-label="Add participant" onClick={() => setAdding(!adding)} className="rounded-md p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900">
            <Icon name="plus" />
          </button>
        )}
      </div>
      <div data-testid="members">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">Users ({humans.length})</p>
        <ul className="mb-4">{humans.map((m) => <Row key={m.participant_id} m={m} />)}</ul>
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">Agents ({bots.length})</p>
        <ul>{bots.map((m) => <Row key={m.participant_id} m={m} />)}</ul>
      </div>
      {canManage && adding && <ManageForms room={room} members={members} />}
    </div>
  );
}

function ManageForms({ room, members }: { room: RoomT; members: Member[] }) {
  const qc = useQueryClient();
  const dir = useDirectory();
  const [who, setWho] = useState("");
  const [role, setRole] = useState("member");
  const [dept, setDept] = useState("");
  const [access, setAccess] = useState("participate");
  const inRoom = new Set(members.map((m) => m.participant_id));
  const add = useMutation({
    mutationFn: () => api<Member>(`/v1/rooms/${room.id}/members`, { body: { participant_id: who, role } }),
    onSuccess: () => {
      setWho("");
      qc.invalidateQueries({ queryKey: ["members", room.id] });
    },
  });
  const grant = useMutation({
    mutationFn: () => api(`/v1/rooms/${room.id}/access`, { method: "PUT", body: { department_id: dept, access } }),
    onSuccess: () => toast("Department access granted"),
  });
  return (
    <div className="mt-4 space-y-4 border-t border-slate-100 pt-3">
      <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
        <Field label="Add participant">
          <select className={inputClass} value={who} onChange={(e) => setWho(e.target.value)} required>
            <option value="">Choose…</option>
            {dir.participants.filter((p) => !inRoom.has(p.id)).map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name} ({p.kind}, {dir.dept(p.department_id)})
              </option>
            ))}
          </select>
        </Field>
        <div className="flex gap-2">
          <select aria-label="Role" className={inputClass} value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="member">member</option>
            <option value="observer">observer</option>
            <option value="owner">owner</option>
          </select>
          <Btn type="submit" disabled={!who || add.isPending}>Add</Btn>
        </div>
        <ErrorNote error={add.error} />
      </form>
      {room.visibility !== "private" && (
        <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); grant.mutate(); }}>
          <Field label="Let a department's people in" hint="Humans of that department can then read (or take part in) this room.">
            <select className={inputClass} value={dept} onChange={(e) => setDept(e.target.value)} required>
              <option value="">Choose…</option>
              {dir.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </Field>
          <div className="flex gap-2">
            <select aria-label="Access" className={inputClass} value={access} onChange={(e) => setAccess(e.target.value)}>
              <option value="participate">participate</option>
              <option value="read">read only</option>
            </select>
            <Btn type="submit" disabled={!dept || grant.isPending}>Grant</Btn>
          </div>
          <ErrorNote error={grant.error} />
        </form>
      )}
    </div>
  );
}

