import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import { isAdmin, useDirectory, useViewer } from "../api/hooks";
import type { Room, Team } from "../api/types";
import { Icon } from "../components/icons";
import { Avatar, Btn, Card, Chip, Empty, ErrorNote, Field, inputClass, Loading, PageTitle, SearchBox } from "../components/ui";
import { toast } from "../lib/toast";

/** Teams: named groups of people and agents. A team only groups; it grants no access by itself. */
export function Teams() {
  const viewer = useViewer();
  const dir = useDirectory();
  const teams = useQuery({ queryKey: ["teams"], queryFn: () => api<Team[]>("/v1/teams") });
  const [creating, setCreating] = useState(false);
  const canCreate = viewer ? isAdmin(viewer.roles) : false;
  const newButton = (
    <Btn tone="primary" onClick={() => setCreating((c) => !c)} aria-expanded={creating}>
      <span className="inline-flex items-center gap-1.5"><Icon name="plus" className="h-4 w-4" /> New team</span>
    </Btn>
  );
  return (
    <div>
      <PageTitle right={canCreate ? newButton : undefined}>Teams</PageTitle>
      {creating && <NewTeam onDone={() => setCreating(false)} />}
      {teams.isLoading && <Loading />}
      <ErrorNote error={teams.error} />
      {teams.data?.length === 0 && !creating && (
        <Empty title="No teams yet" action={canCreate ? newButton : undefined}>
          Group the people and agents that work together, then add the whole team to a room in one click.
        </Empty>
      )}
      <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {teams.data?.map((t) => (
          <TeamCard key={t.id} team={t} manage={!!viewer && (viewer.roles.includes("org_admin") || (viewer.roles.includes("dept_admin") && viewer.departmentId === t.department_id))} deptName={dir.dept(t.department_id)} />
        ))}
      </ul>
    </div>
  );
}

function PeoplePicker({ chosen, onChange, exclude = [] }: { chosen: string[]; onChange: (ids: string[]) => void; exclude?: string[] }) {
  const dir = useDirectory();
  const [q, setQ] = useState("");
  const options = dir.participants.filter((p) => p.status === "active" && !exclude.includes(p.id) && `${p.display_name} ${p.handle}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <div>
      <SearchBox label="Search people and agents" value={q} onChange={setQ} placeholder="Search people and agents…" />
      <ul className="mt-2 max-h-44 space-y-0.5 overflow-y-auto rounded-lg border border-slate-200 p-1">
        {options.length === 0 && <li className="px-2 py-1.5 text-sm text-slate-500">No one to add.</li>}
        {options.map((p) => {
          const on = chosen.includes(p.id);
          return (
            <li key={p.id}>
              <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-slate-50">
                <input type="checkbox" checked={on} onChange={() => onChange(on ? chosen.filter((x) => x !== p.id) : [...chosen, p.id])} />
                <Avatar name={p.display_name} size="sm" agent={p.kind === "agent"} />
                <span className="font-medium">{p.display_name}</span>
                <span className="text-xs text-slate-400">{p.kind === "agent" ? "agent · " : ""}{dir.dept(p.department_id)}</span>
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function NewTeam({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const viewer = useViewer();
  const dir = useDirectory();
  const orgAdmin = viewer?.roles.includes("org_admin") ?? false;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [dept, setDept] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const create = useMutation({
    mutationFn: () => api<Team>("/v1/teams", { body: { name, description: description || null, department_id: dept || null, member_ids: members } }),
    onSuccess: (t) => {
      toast(`Team “${t.name}” created`);
      qc.invalidateQueries({ queryKey: ["teams"] });
      onDone();
    },
  });
  return (
    <Card className="mb-6">
      <form className="grid gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <div className="space-y-3">
          <Field label="Team name"><input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required autoFocus /></Field>
          <Field label="Description (optional)"><input className={inputClass} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
          {orgAdmin && (
            <Field label="Owning department" hint="Its admins manage the team. Defaults to yours.">
              <select className={inputClass} value={dept} onChange={(e) => setDept(e.target.value)}>
                <option value="">My department</option>
                {dir.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </Field>
          )}
          <ErrorNote error={create.error} />
          <Btn tone="primary" type="submit" disabled={!name.trim() || create.isPending}>Create team</Btn>
        </div>
        <div>
          <p className="mb-1 text-sm font-medium text-slate-700">Members ({members.length})</p>
          <PeoplePicker chosen={members} onChange={setMembers} />
        </div>
      </form>
    </Card>
  );
}

function TeamCard({ team, manage, deptName }: { team: Team; manage: boolean; deptName: string }) {
  const qc = useQueryClient();
  const dir = useDirectory();
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState<string[]>([]);
  const refresh = () => qc.invalidateQueries({ queryKey: ["teams"] });
  const add = useMutation({
    mutationFn: async () => { for (const id of adding) await api<Team>(`/v1/teams/${team.id}/members`, { body: { participant_id: id } }); },
    onSuccess: () => { setAdding([]); refresh(); },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api<Team>(`/v1/teams/${team.id}/members/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
  const del = useMutation({
    mutationFn: () => api(`/v1/teams/${team.id}`, { method: "DELETE" }),
    onSuccess: () => { toast("Team deleted"); refresh(); },
  });
  const names = team.member_ids.map((id) => dir.person(id)).filter((p) => p !== undefined);
  return (
    <li data-testid="team">
      <Card className="flex h-full flex-col">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="truncate font-medium text-slate-900">{team.name}</h3>
            {team.description && <p className="mt-0.5 line-clamp-2 text-sm text-slate-500">{team.description}</p>}
          </div>
          <Chip className="bg-brand-50 text-brand-700 ring-1 ring-brand-200">{deptName}</Chip>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <span className="flex -space-x-1">
            {names.slice(0, 5).map((p) => <Avatar key={p.id} name={p.display_name} size="sm" agent={p.kind === "agent"} />)}
          </span>
          <span className="text-xs text-slate-500">{team.member_ids.length} member{team.member_ids.length === 1 ? "" : "s"}</span>
          <button className="ml-auto text-sm font-medium text-brand-700 hover:underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "Close" : "Open"}
          </button>
        </div>
        {open && (
          <div className="mt-3 space-y-3 border-t border-slate-100 pt-3">
            <ul className="space-y-1">
              {names.length === 0 && <li className="text-sm text-slate-500">No members yet.</li>}
              {names.map((p) => (
                <li key={p.id} className="flex items-center gap-2 text-sm">
                  <Avatar name={p.display_name} size="sm" agent={p.kind === "agent"} />
                  <span className="font-medium">{p.display_name}</span>
                  <span className="text-xs text-slate-400">{p.kind === "agent" ? "agent · " : ""}{dir.dept(p.department_id)}</span>
                  {manage && (
                    <button aria-label={`Remove ${p.display_name}`} className="ml-auto text-xs text-red-700 hover:underline" disabled={remove.isPending} onClick={() => remove.mutate(p.id)}>Remove</button>
                  )}
                </li>
              ))}
            </ul>
            <AddToRoom team={team} />
            {manage && (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Add members</p>
                <PeoplePicker chosen={adding} onChange={setAdding} exclude={team.member_ids} />
                <div className="flex items-center justify-between">
                  <Btn disabled={adding.length === 0 || add.isPending} onClick={() => add.mutate()}>Add {adding.length || ""}</Btn>
                  <Btn tone="danger" disabled={del.isPending} onClick={() => { if (window.confirm(`Delete the team “${team.name}”? Its members are not affected.`)) del.mutate(); }}>Delete team</Btn>
                </div>
              </div>
            )}
            <ErrorNote error={add.error ?? remove.error ?? del.error} />
          </div>
        )}
      </Card>
    </li>
  );
}

/** Add every member of the team to a room, through the ordinary room-membership API, so room
 *  rules and policies apply to each person exactly as if they were added one by one. */
function AddToRoom({ team }: { team: Team }) {
  const qc = useQueryClient();
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => api<Room[]>("/v1/rooms") });
  const [room, setRoom] = useState("");
  const go = useMutation({
    mutationFn: async () => {
      let added = 0;
      const failed: string[] = [];
      for (const id of team.member_ids) {
        try {
          await api(`/v1/rooms/${room}/members`, { body: { participant_id: id, role: "member" } });
          added += 1;
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) failed.push(e instanceof ApiError ? e.detail : "failed"); // 409 = already in the room
        }
      }
      return { added, failed };
    },
    onSuccess: ({ added, failed }) => {
      qc.invalidateQueries({ queryKey: ["members", room] });
      toast(failed.length ? `Added ${added}; ${failed.length} could not be added (${failed[0]})` : `Added ${added} to the room`, failed.length ? "error" : undefined);
    },
  });
  if (team.member_ids.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <select aria-label="Room" className={`${inputClass} flex-1`} value={room} onChange={(e) => setRoom(e.target.value)}>
        <option value="">Add the team to a room…</option>
        {(rooms.data ?? []).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
      </select>
      <Btn disabled={!room || go.isPending} onClick={() => go.mutate()}>Add all</Btn>
    </div>
  );
}
