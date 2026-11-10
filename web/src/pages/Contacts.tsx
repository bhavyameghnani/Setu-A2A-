import { useState } from "react";
import { useDirectory } from "../api/hooks";
import { Avatar, Card, Chip, Empty, FilterSelect, Loading, PageTitle, SearchBox } from "../components/ui";
import { href } from "../lib/router";

/** Everyone in the organization, people and agents, grouped by department. */
export function Contacts() {
  const dir = useDirectory();
  const [q, setQ] = useState("");
  const [deptF, setDeptF] = useState("");
  const [kindF, setKindF] = useState("");
  const shown = dir.participants.filter(
    (p) =>
      p.status === "active" &&
      `${p.display_name} ${p.handle}`.toLowerCase().includes(q.toLowerCase()) &&
      (!deptF || p.department_id === deptF) &&
      (!kindF || p.kind === kindF),
  );
  const groups = new Map<string, typeof shown>();
  for (const p of shown) groups.set(p.department_id, [...(groups.get(p.department_id) ?? []), p]);

  return (
    <div>
      <PageTitle>Contacts</PageTitle>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <SearchBox label="Search contacts" value={q} onChange={setQ} placeholder="Search people and agents…" />
        <FilterSelect label="Department" value={deptF} onChange={setDeptF} options={dir.departments.map((d) => [d.id, d.name])} />
        <FilterSelect label="Type" value={kindF} onChange={setKindF} options={[["human", "People"], ["agent", "Agents"]]} />
      </div>
      {!dir.ready && <Loading />}
      {dir.ready && shown.length === 0 && <Empty>No one matches these filters.</Empty>}
      {[...groups.entries()].map(([deptId, list]) => (
        <section key={deptId} className="mb-8">
          <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
            {dir.dept(deptId)} <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-600">{list.length}</span>
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {list.map((p) => (
              <li key={p.id} data-testid="contact">
                <Card className="flex items-center gap-3">
                  <Avatar name={p.display_name} size="lg" agent={p.kind === "agent"} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-slate-900">{p.display_name}</p>
                    <p className="truncate font-mono text-xs text-slate-500">@{p.handle}</p>
                  </div>
                  {p.kind === "agent" ? (
                    <a href={href.agents()} className="shrink-0"><Chip className="bg-violet-100 text-violet-800">agent</Chip></a>
                  ) : (
                    <Chip className="bg-slate-100 text-slate-600">person</Chip>
                  )}
                </Card>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
