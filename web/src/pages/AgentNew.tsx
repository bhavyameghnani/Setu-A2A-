import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useDirectory, useViewer } from "../api/hooks";
import type { AgentPreview } from "../api/types";
import { Icon } from "../components/icons";
import { Btn, Card, ErrorNote, Field, inputClass } from "../components/ui";
import { href, navigate } from "../lib/router";
import { toast } from "../lib/toast";

const STEPS = ["Connect", "Identity", "Review"] as const;
type AuthKind = "none" | "bearer" | "api_key";

/** Register an A2A agent in three steps: connect and test, name it, review. Setu connects to the
 *  agent; it never runs it, so there is no runtime or instructions step. */
export function AgentNew() {
  const qc = useQueryClient();
  const viewer = useViewer();
  const dir = useDirectory();
  const orgAdmin = viewer?.roles.includes("org_admin") ?? false;
  const choices = dir.departments.filter((d) => orgAdmin || d.id === viewer?.departmentId);

  const [step, setStep] = useState(0);
  const [cardUrl, setCardUrl] = useState("");
  const [auth, setAuth] = useState<AuthKind>("none");
  const [token, setToken] = useState("");
  const [header, setHeader] = useState("X-API-Key");
  const [dept, setDept] = useState("");
  const [handle, setHandle] = useState("");
  const [displayName, setDisplayName] = useState("");

  const authBody =
    auth === "bearer" ? { type: "bearer", token } : auth === "api_key" ? { type: "api_key", header, value: token } : null;

  const test = useMutation({
    mutationFn: () => api<AgentPreview>("/v1/agents/preview", { body: { card_url: cardUrl, auth: authBody } }),
    onSuccess: (p) => setHandle((h) => h || p.suggested_handle),
  });
  const preview = test.data;
  const register = useMutation({
    mutationFn: () =>
      api<{ display_name: string }>("/v1/agents/register", {
        body: {
          card_url: cardUrl,
          department_id: dept || choices[0]?.id,
          handle: handle || null,
          display_name: displayName || null,
          auth: authBody,
        },
      }),
    onSuccess: (r) => {
      toast(`Registered ${r.display_name}`);
      for (const k of ["agents", "participants", "agent-stats"]) qc.invalidateQueries({ queryKey: [k] });
      navigate(href.agents());
    },
  });
  const edited = () => test.reset(); // a changed URL or credential means the test no longer counts

  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-6 flex items-center justify-between border-b border-slate-200 pb-4">
        <div className="flex items-center gap-2 text-sm text-slate-600">
          <a href={href.agents()} className="inline-flex items-center gap-1.5 hover:text-slate-900">
            <Icon name="back" className="h-4 w-4" /> Agents
          </a>
          <span>/</span>
          <span className="font-medium text-slate-900">New agent</span>
        </div>
        <ol className="flex items-center gap-2 text-sm" aria-label="Steps">
          {STEPS.map((s, i) => (
            <li key={s} className="flex items-center gap-2">
              <span className={i === step ? "font-semibold text-slate-900" : i < step ? "text-brand-700" : "text-slate-400"}>{s}</span>
              {i < STEPS.length - 1 && <Icon name="chevron" className="h-3.5 w-3.5 text-slate-300" />}
            </li>
          ))}
        </ol>
      </div>

      {step === 0 && (
        <div className="space-y-4">
          <Field label="Agent Card URL" hint="Paste the agent's base URL or its /.well-known/agent-card.json.">
            <input className={inputClass} type="url" value={cardUrl} onChange={(e) => { setCardUrl(e.target.value); edited(); }} placeholder="https://agent.example.com" />
          </Field>
          <Field label="How does Setu authenticate to it?">
            <select className={inputClass} value={auth} onChange={(e) => { setAuth(e.target.value as AuthKind); edited(); }}>
              <option value="none">No credentials</option>
              <option value="bearer">Bearer token</option>
              <option value="api_key">API key header</option>
            </select>
          </Field>
          {auth === "api_key" && (
            <Field label="Header name"><input className={inputClass} value={header} onChange={(e) => { setHeader(e.target.value); edited(); }} /></Field>
          )}
          {auth !== "none" && (
            <Field label={auth === "bearer" ? "Token" : "Key"} hint="Stored encrypted. Never shown again.">
              <input className={inputClass} type="password" autoComplete="off" value={token} onChange={(e) => { setToken(e.target.value); edited(); }} />
            </Field>
          )}
          <Card>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="font-medium text-slate-900">Connection check</p>
                <p className="text-sm text-slate-500">Fetches the Agent Card the same way registering does, and saves nothing.</p>
              </div>
              <Btn disabled={!cardUrl || test.isPending || (auth !== "none" && !token)} onClick={() => test.mutate()}>
                {test.isPending ? "Testing…" : "Test connection"}
              </Btn>
            </div>
            <div className="mt-3">
              <ErrorNote error={test.error} />
              {preview && <CheckList p={preview} auth={auth} />}
            </div>
          </Card>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <Field label="Owning department" hint="Who runs this agent. Policies and notifications follow the department.">
            <select className={inputClass} value={dept || choices[0]?.id || ""} onChange={(e) => setDept(e.target.value)}>
              {choices.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </Field>
          <Field label="Handle (optional)" hint="How people @mention it. Defaults to the card's name.">
            <input className={`${inputClass} font-mono`} value={handle} onChange={(e) => setHandle(e.target.value)} />
          </Field>
          <Field label="Display name (optional)" hint={`Defaults to the card's name${preview ? `: ${preview.name}` : ""}.`}>
            <input className={inputClass} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </Field>
        </div>
      )}

      {step === 2 && preview && (
        <div>
          <h2 className="mb-3 text-base font-semibold text-slate-900">What Setu will create</h2>
          <Card className="divide-y divide-slate-100 p-0">
            {(
              [
                ["Name", displayName || preview.name],
                ["Handle", `@${handle || preview.suggested_handle}`],
                ["Description", preview.description || "—"],
                ["Department", dir.dept(dept || choices[0]?.id)],
                ["Endpoint", preview.rpc_url],
                ["Protocol", `A2A ${preview.a2a_version}${preview.streaming ? ", streaming" : ""}`],
                ["Skills", preview.skills.map((s) => s.id).join(", ") || "none advertised"],
                ["Authentication", auth === "none" ? "None" : auth === "bearer" ? "Bearer token (stored encrypted)" : `API key in ${header} (stored encrypted)`],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k} className="grid grid-cols-[9rem_1fr] gap-3 px-4 py-2.5 text-sm">
                <dt className="text-slate-500">{k}</dt>
                <dd className="break-all text-slate-900">{v}</dd>
              </div>
            ))}
          </Card>
          <div className="mt-3"><ErrorNote error={register.error} /></div>
          {(preview.warnings ?? []).map((w) => <p key={w} className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">{w}</p>)}
        </div>
      )}

      <div className="mt-8 flex items-center justify-between">
        {step === 0 ? (
          <a href={href.agents()} className="rounded-lg px-3.5 py-2 text-sm font-medium text-slate-800 shadow-sm ring-1 ring-slate-300 hover:bg-slate-50">Cancel</a>
        ) : (
          <Btn onClick={() => setStep(step - 1)}>Back</Btn>
        )}
        {step < 2 ? (
          <Btn tone="primary" disabled={step === 0 && !preview} onClick={() => setStep(step + 1)}>Continue</Btn>
        ) : (
          <Btn tone="primary" disabled={register.isPending} onClick={() => register.mutate()}>
            {register.isPending ? "Registering…" : "Register agent"}
          </Btn>
        )}
      </div>
    </div>
  );
}

function CheckList({ p, auth }: { p: AgentPreview; auth: AuthKind }) {
  const items: [boolean, string][] = [
    [true, `Agent Card reachable: ${p.name}`],
    [true, `A2A ${p.a2a_version}${p.protocol_versions.length > 1 ? ` (card lists ${p.protocol_versions.join(", ")})` : ""}`],
    [p.streaming, p.streaming ? "Streaming supported" : "No streaming: Setu will wait for the final result"],
    [p.skills.length > 0, p.skills.length ? `${p.skills.length} skill${p.skills.length > 1 ? "s" : ""}: ${p.skills.map((s) => s.id).join(", ")}` : "No skills advertised"],
    [p.security_schemes.length === 0 || auth !== "none", p.security_schemes.length ? `Card requires: ${p.security_schemes.join(", ")}` : "No authentication required"],
  ];
  return (
    <ul className="space-y-1.5 text-sm" data-testid="connection-check">
      {items.map(([ok, text]) => (
        <li key={text} className="flex items-start gap-2">
          <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${ok ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>
            <Icon name="check" className="h-3 w-3" />
          </span>
          <span className="text-slate-700">{text}</span>
        </li>
      ))}
      {(p.warnings ?? []).map((w) => <li key={w} className="rounded-lg bg-amber-50 px-3 py-2 text-amber-800">{w}</li>)}
    </ul>
  );
}
