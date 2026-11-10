import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useDirectory, useViewer } from "../api/hooks";
import type { Policy } from "../api/types";
import { NewDepartment } from "../components/NewDepartment";
import { Icon, type IconName } from "../components/icons";
import { Btn, Card, Chip, Empty, ErrorNote, Field, inputClass, Loading, PageTitle } from "../components/ui";
import { buildRule, describeRule, RULE_TYPES, type RuleType } from "../lib/policy";
import { toast } from "../lib/toast";

const RULE_LOOK: Record<string, { icon: IconName; tone: string; label: string }> = {
  allow_talk: { icon: "check", tone: "bg-emerald-100 text-emerald-700", label: "Allow" },
  deny_talk: { icon: "ban", tone: "bg-red-100 text-red-700", label: "Deny" },
  topic_block: { icon: "lock", tone: "bg-amber-100 text-amber-700", label: "Block topic" },
  require_approval: { icon: "policies", tone: "bg-brand-100 text-brand-700", label: "Needs approval" },
};

export function Policies() {
  const qc = useQueryClient();
  const viewer = useViewer();
  const dir = useDirectory();
  const orgAdmin = viewer?.roles.includes("org_admin") ?? false;
  const oversight = orgAdmin || (viewer?.roles.includes("auditor") ?? false);
  const policies = useQuery({
    queryKey: ["policies"],
    queryFn: () => api<Policy[]>("/v1/policies"),
    enabled: oversight,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["policies"] });
  const toggle = useMutation({
    mutationFn: (p: Policy) => api(`/v1/policies/${p.id}`, { method: "PUT", body: { enabled: !p.enabled } }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (p: Policy) => api(`/v1/policies/${p.id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast("Policy deleted");
      refresh();
    },
  });
  if (viewer && !oversight) {
    return (
      <Empty>
        Policies are visible to org admins and auditors. By default, people and agents may talk within their
        own department; anything across departments needs a rule that allows it.
      </Empty>
    );
  }
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_24rem]">
      <section>
        <PageTitle>Policies</PageTitle>
        <div className="mb-4 grid gap-2 text-sm sm:grid-cols-3">
          {[
            ["Same department", "allowed", "bg-emerald-50 text-emerald-800 ring-emerald-200"],
            ["Another department", "denied until a rule allows it", "bg-amber-50 text-amber-800 ring-amber-200"],
            ["Another organization", "never", "bg-slate-100 text-slate-700 ring-slate-200"],
          ].map(([a, b, c]) => (
            <div key={a} className={`rounded-xl px-3 py-2 ring-1 ${c}`}>
              <p className="text-xs opacity-70">{a}</p>
              <p className="font-medium">{b}</p>
            </div>
          ))}
        </div>
        {policies.isLoading && <Loading />}
        <ErrorNote error={policies.error ?? toggle.error ?? remove.error} />
        {policies.data?.length === 0 && <Empty title="No rules yet">So every cross-department request is denied.</Empty>}
        <ul className="space-y-2">
          {policies.data?.map((p) => (
            <li key={p.id} data-testid="policy">
              <Card className={p.enabled ? "" : "bg-slate-50 opacity-70"}>
                <div className="flex items-start gap-3">
                  <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${(RULE_LOOK[String(p.rule.type)] ?? RULE_LOOK.allow_talk).tone}`}>
                    <Icon name={(RULE_LOOK[String(p.rule.type)] ?? RULE_LOOK.allow_talk).icon} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="font-medium text-slate-900">{p.name}</p>
                    <p className="text-sm text-slate-600">{describeRule(p.rule, dir.dept)}</p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <Chip className="bg-slate-100 text-slate-600">{(RULE_LOOK[String(p.rule.type)] ?? RULE_LOOK.allow_talk).label}</Chip>
                    <Chip className={p.enabled ? "bg-green-100 text-green-800" : "bg-gray-200 text-gray-700"}>
                      {p.enabled ? "on" : "off"}
                    </Chip>
                  </div>
                </div>
                {orgAdmin && (
                  <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
                    <Btn disabled={toggle.isPending} onClick={() => toggle.mutate(p)}>{p.enabled ? "Turn off" : "Turn on"}</Btn>
                    <JsonEditor policy={p} onSaved={refresh} />
                    <Btn tone="danger" disabled={remove.isPending} onClick={() => { if (window.confirm(`Delete “${p.name}”?`)) remove.mutate(p); }}>
                      Delete
                    </Btn>
                  </div>
                )}
              </Card>
            </li>
          ))}
        </ul>
      </section>
      <aside>
        <PageTitle>Add a rule</PageTitle>
        {orgAdmin ? <RuleForm onCreated={refresh} /> : <Empty>Only org admins can change policies.</Empty>}
        {orgAdmin && (
          <div className="mt-8">
            <PageTitle>New department</PageTitle>
            <NewDepartment />
          </div>
        )}
      </aside>
    </div>
  );
}

function RuleForm({ onCreated }: { onCreated: () => void }) {
  const dir = useDirectory();
  const [name, setName] = useState("");
  const [type, setType] = useState<RuleType>("allow_talk");
  const [f, setF] = useState<Record<string, string>>({ matchBy: "skill", approverRole: "dept_admin" });
  const set = (k: string) => (v: string) => setF((cur) => ({ ...cur, [k]: v }));
  const create = useMutation({
    mutationFn: () => api("/v1/policies", { body: { name, rule: buildRule(type, f) } }),
    onSuccess: () => {
      toast("Rule added");
      setName("");
      onCreated();
    },
  });
  const Dept = ({ k, label, any = true }: { k: string; label: string; any?: boolean }) => (
    <Field label={label}>
      <select className={inputClass} value={f[k] ?? ""} onChange={(e) => set(k)(e.target.value)} required={!any}>
        <option value="">{any ? "Any department" : "Choose…"}</option>
        {dir.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </Field>
  );
  return (
    <Card>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Field label="Name"><input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <Field label="Type">
          <select className={inputClass} value={type} onChange={(e) => setType(e.target.value as RuleType)}>
            {RULE_TYPES.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
          </select>
        </Field>
        <Dept k="from" label="From department" />
        <Dept k="to" label="To department" />
        {type === "topic_block" && (
          <Field label="Blocked skills or topics" hint="Comma separated."><input className={inputClass} value={f.topics ?? ""} onChange={(e) => set("topics")(e.target.value)} required /></Field>
        )}
        {type === "require_approval" && (
          <>
            <Field label="Applies to">
              <select className={inputClass} value={f.matchBy} onChange={(e) => set("matchBy")(e.target.value)}>
                <option value="skill">a skill</option>
                <option value="kind">a message kind</option>
              </select>
            </Field>
            {f.matchBy === "skill" ? (
              <Field label="Skill" hint="e.g. risk.score, or * for any."><input className={inputClass} value={f.skill ?? ""} onChange={(e) => set("skill")(e.target.value)} required /></Field>
            ) : (
              <Field label="Message kind"><input className={inputClass} value={f.kind ?? "task"} onChange={(e) => set("kind")(e.target.value)} /></Field>
            )}
            <Dept k="approverDept" label="Approver department" any={false} />
            <Field label="Approver role">
              <select className={inputClass} value={f.approverRole} onChange={(e) => set("approverRole")(e.target.value)}>
                <option value="dept_admin">department admin</option>
                <option value="dept_member">any member</option>
                <option value="org_admin">org admin</option>
              </select>
            </Field>
          </>
        )}
        <ErrorNote error={create.error} />
        <Btn tone="primary" type="submit" disabled={create.isPending}>Add rule</Btn>
      </form>
    </Card>
  );
}

function JsonEditor({ policy, onSaved }: { policy: Policy; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(JSON.stringify(policy.rule, null, 2));
  const save = useMutation({
    mutationFn: () => api(`/v1/policies/${policy.id}`, { method: "PUT", body: { rule: JSON.parse(text) } }),
    onSuccess: () => {
      toast("Rule updated");
      setOpen(false);
      onSaved();
    },
  });
  if (!open) return <Btn onClick={() => setOpen(true)}>Edit rule</Btn>;
  return (
    <div className="w-full space-y-2">
      <textarea aria-label="Rule JSON" className={`${inputClass} h-40 font-mono text-xs`} value={text} onChange={(e) => setText(e.target.value)} />
      <ErrorNote error={save.error} />
      <div className="flex gap-2">
        <Btn tone="primary" disabled={save.isPending} onClick={() => save.mutate()}>Save</Btn>
        <Btn onClick={() => setOpen(false)}>Cancel</Btn>
      </div>
    </div>
  );
}
