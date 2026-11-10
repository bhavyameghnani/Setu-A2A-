import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { isAdmin, useDirectory, useViewer } from "../api/hooks";
import type { Room as RoomT } from "../api/types";
import { Icon } from "../components/icons";
import { Btn, Empty, ErrorNote, Field, inputClass, Loading, Mascot, SearchBox } from "../components/ui";
import { timeAgo } from "../lib/format";
import { href, navigate } from "../lib/router";
import { Room } from "./Room";

const VISIBILITY = [
  ["private", "Private: members only"],
  ["department", "Department: my department"],
  ["cross_department", "Cross-department: grant other departments"],
] as const;

/** Rooms as a workspace: the list of rooms on the left, the open room (chat, work, participants)
 *  on the right. On a phone only one of the two shows at a time. */
export function RoomsLayout({ activeId }: { activeId?: string }) {
  return (
    <div className="flex h-screen overflow-hidden">
      <div className={`${activeId ? "hidden md:flex" : "flex"} w-full shrink-0 flex-col border-r border-slate-200 bg-white md:w-72`}>
        <RoomList activeId={activeId} />
      </div>
      <div className={`${activeId ? "flex" : "hidden md:flex"} min-w-0 flex-1 flex-col`}>
        {activeId ? (
          <Room id={activeId} key={activeId} />
        ) : (
          <div className="m-auto flex flex-col items-center text-center text-slate-500">
            <Mascot className="mb-3 h-16 w-16" />
            <p className="text-base font-semibold text-slate-900">Pick a room</p>
            <p className="text-sm">Or start a new one with the + button.</p>
          </div>
        )}
      </div>
    </div>
  );
}

function RoomList({ activeId }: { activeId?: string }) {
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => api<RoomT[]>("/v1/rooms"), refetchInterval: 15_000 });
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const shown = (rooms.data ?? []).filter((r) => `${r.name} ${r.topic ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <h1 className="text-xl font-semibold tracking-tight text-slate-900">Rooms</h1>
        <button
          aria-label="New room"
          aria-expanded={creating}
          onClick={() => setCreating((c) => !c)}
          className={`rounded-lg p-1.5 ${creating ? "bg-brand-100 text-brand-700" : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"}`}
        >
          <Icon name="plus" />
        </button>
      </div>
      {creating && <NewRoom onDone={() => setCreating(false)} />}
      <div className="px-3 pb-2">
        <SearchBox label="Search rooms" value={q} onChange={setQ} placeholder="Search rooms…" />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {rooms.isLoading && <Loading />}
        <ErrorNote error={rooms.error} />
        {rooms.data?.length === 0 && (
          <div className="px-2">
            <Empty>No rooms yet. Create one to get your agents working together.</Empty>
          </div>
        )}
        {rooms.data && rooms.data.length > 0 && shown.length === 0 && <p className="px-3 py-2 text-sm text-slate-500">No rooms match.</p>}
        <ul className="space-y-0.5">
          {shown.map((r) => (
            <li key={r.id}>
              <a
                href={href.room(r.id)}
                aria-current={r.id === activeId ? "page" : undefined}
                className={`block rounded-lg border-l-2 px-3 py-2.5 ${r.id === activeId ? "border-brand-600 bg-brand-50" : "border-transparent hover:bg-slate-50"}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-slate-900">{r.name}</span>
                  <span className="shrink-0 text-xs text-slate-400">{timeAgo(r.created_at)}</span>
                </div>
                <p className="mt-0.5 truncate text-xs text-slate-500">
                  {r.topic ? `${r.topic} · ` : ""}
                  {r.visibility.replace("_", "-")} · {r.last_seq} messages
                </p>
              </a>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

function NewRoom({ onDone }: { onDone: () => void }) {
  const viewer = useViewer();
  const dir = useDirectory();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [visibility, setVisibility] = useState<string>("cross_department");
  const [owning, setOwning] = useState("");
  const create = useMutation({
    mutationFn: () =>
      api<RoomT>("/v1/rooms", {
        body: { name, topic: topic || null, visibility, owning_department_id: owning || null },
      }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["rooms"] });
      onDone();
      navigate(href.room(r.id));
    },
  });
  const admin = viewer ? isAdmin(viewer.roles) : false;
  return (
    <form
      className="mx-3 mb-3 space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <Field label="Name">
        <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
      </Field>
      <Field label="Topic (optional)">
        <input className={inputClass} value={topic} onChange={(e) => setTopic(e.target.value)} />
      </Field>
      <Field label="Visibility">
        <select className={inputClass} value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          {VISIBILITY.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </Field>
      {admin && (
        <Field label="Owning department" hint="Defaults to yours.">
          <select className={inputClass} value={owning} onChange={(e) => setOwning(e.target.value)}>
            <option value="">My department</option>
            {dir.departments.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
      )}
      <ErrorNote error={create.error} />
      <Btn tone="primary" type="submit" className="w-full" disabled={create.isPending}>
        Create room
      </Btn>
    </form>
  );
}
