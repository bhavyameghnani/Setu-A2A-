import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useEffect, useState } from "react";
import { useDirectory } from "../api/hooks";
import { errorText } from "../api/client";
import { label, stateStyle } from "../lib/format";
import { Icon } from "./icons";
import type { ToastDetail } from "../lib/toast";

export function Chip({ className = "", children }: { className?: string; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium ${className}`}>
      {children}
    </span>
  );
}

export const StateChip = ({ state }: { state: string }) => (
  <Chip className={stateStyle(state)}>{label(state)}</Chip>
);

export function DeptBadge({ id }: { id: string }) {
  const { dept } = useDirectory();
  return <Chip className="bg-brand-50 text-brand-700 ring-1 ring-brand-200">{dept(id)}</Chip>;
}

/** A person or agent: name, a marker for agents, and their department badge. */
export function Who({ id, className = "" }: { id: string; className?: string }) {
  const { person } = useDirectory();
  const p = person(id);
  if (!p) return <span className={`text-gray-400 ${className}`}>…</span>;
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span className="font-medium">{p.display_name}</span>
      {p.kind === "agent" && <Chip className="bg-violet-100 text-violet-800">agent</Chip>}
      <DeptBadge id={p.department_id} />
    </span>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { tone?: "primary" | "plain" | "danger" | "good" };
const TONES = {
  primary: "bg-brand-600 text-white shadow-sm hover:bg-brand-700",
  plain: "bg-white text-slate-800 shadow-sm ring-1 ring-slate-300 hover:bg-slate-50",
  danger: "bg-white text-red-700 ring-1 ring-red-300 hover:bg-red-50",
  good: "bg-green-600 text-white hover:bg-green-500",
};
export function Btn({ tone = "plain", className = "", ...rest }: BtnProps) {
  return (
    <button
      {...rest}
      className={`rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${TONES[tone]} ${className}`}
    />
  );
}

export const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100";

export function Field({ label: text, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{text}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export const Card = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <div className={`rounded-xl border border-slate-200 bg-white p-4 shadow-card ${className}`}>{children}</div>
);

export const PageTitle = ({ children, right }: { children: ReactNode; right?: ReactNode }) => (
  <div className="mb-5 flex items-center justify-between gap-4">
    <h1 className="whitespace-nowrap text-2xl font-semibold tracking-tight text-slate-900">{children}</h1>
    {right}
  </div>
);

export const ErrorNote = ({ error }: { error: unknown }) =>
  error ? (
    <p role="alert" className="rounded bg-red-50 px-3 py-2 text-sm text-red-700">
      {errorText(error)}
    </p>
  ) : null;

/** Friendly empty state. `title` and `action` are optional; a plain child still works. */
export const Empty = ({ children, title, action }: { children: ReactNode; title?: string; action?: ReactNode }) => (
  <div className="flex flex-col items-center rounded-xl border border-dashed border-slate-300 bg-white/60 px-4 py-10 text-center">
    <Mascot className="mb-3 h-14 w-14" />
    {title && <p className="mb-1 text-base font-semibold text-slate-900">{title}</p>}
    <p className="max-w-md text-sm text-slate-500">{children}</p>
    {action && <div className="mt-4 flex gap-2">{action}</div>}
  </div>
);

/** A small friendly robot, drawn inline so empty states have a face without an image asset. */
export function Mascot({ className = "h-14 w-14" }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true">
      <circle cx="32" cy="34" r="22" fill="#dbeaf2" />
      <circle cx="32" cy="34" r="22" fill="none" stroke="#2c6f8f" strokeWidth="2" />
      <circle cx="24" cy="32" r="4.5" fill="#2c6f8f" />
      <circle cx="40" cy="32" r="4.5" fill="#2c6f8f" />
      <path d="M26 42c3 3 9 3 12 0" stroke="#2c6f8f" strokeWidth="2" strokeLinecap="round" fill="none" />
      <path d="M32 12v-5" stroke="#2c6f8f" strokeWidth="2" strokeLinecap="round" />
      <circle cx="32" cy="6" r="2.5" fill="#3b86a8" />
    </svg>
  );
}

const AVATAR_TONES = ["bg-sky-100 text-sky-800", "bg-emerald-100 text-emerald-800", "bg-amber-100 text-amber-800", "bg-violet-100 text-violet-800", "bg-rose-100 text-rose-800", "bg-teal-100 text-teal-800"];
export function initialsOf(name: string): string {
  const w = name.trim().split(/[\s._-]+/).filter(Boolean);
  return ((w[0]?.[0] ?? "?") + (w.length > 1 ? (w[w.length - 1][0] ?? "") : "")).toUpperCase();
}
export const toneOf = (name: string) => AVATAR_TONES[[...name].reduce((n, c) => n + c.charCodeAt(0), 0) % AVATAR_TONES.length];

/** Initials in a soft color, with an optional presence dot (green = online, grey = offline). */
export function Avatar({ name, size = "md", status, agent }: { name: string; size?: "sm" | "md" | "lg"; status?: "online" | "offline"; agent?: boolean }) {
  const dim = size === "lg" ? "h-11 w-11 text-sm" : size === "sm" ? "h-6 w-6 text-[10px]" : "h-8 w-8 text-xs";
  return (
    <span className="relative inline-flex shrink-0">
      <span className={`inline-flex items-center justify-center font-semibold ${agent ? "rounded-lg" : "rounded-full"} ${dim} ${toneOf(name)}`}>
        {initialsOf(name)}
      </span>
      {status && (
        <span
          title={status}
          className={`absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${status === "online" ? "bg-emerald-500" : "bg-slate-300"}`}
        />
      )}
    </span>
  );
}

/** A search box with an icon, like the toolbars above lists. */
export function SearchBox({ value, onChange, placeholder, label: aria }: { value: string; onChange: (v: string) => void; placeholder: string; label: string }) {
  return (
    <div className="relative flex-1">
      <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
      <input aria-label={aria} className={`${inputClass} pl-9`} placeholder={placeholder} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

/** A filter dropdown: "All" plus choices. */
export function FilterSelect({ label: text, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <select aria-label={text} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{text}: all</option>
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}

export const Loading = () => <p className="text-sm text-slate-500">Loading…</p>;

export function Toasts() {
  const [items, setItems] = useState<(ToastDetail & { id: number })[]>([]);
  useEffect(() => {
    let n = 0;
    const on = (e: Event) => {
      const id = ++n;
      setItems((cur) => [...cur.slice(-2), { ...(e as CustomEvent<ToastDetail>).detail, id }]);
      setTimeout(() => setItems((cur) => cur.filter((t) => t.id !== id)), 5000);
    };
    window.addEventListener("setu:toast", on);
    return () => window.removeEventListener("setu:toast", on);
  }, []);
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2" aria-live="polite">
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          title="Click to dismiss"
          onClick={() => setItems((cur) => cur.filter((x) => x.id !== t.id))}
          className={`pointer-events-auto cursor-pointer rounded-lg px-3 py-2 text-sm shadow-lg ${t.kind === "error" ? "bg-red-600 text-white" : "bg-slate-900 text-white"}`}
        >
          {t.text}
        </div>
      ))}
    </div>
  );
}

export function Parts({ parts }: { parts: Record<string, unknown>[] }) {
  return (
    <div className="space-y-1">
      {parts.map((p, i) => {
        if (typeof p.text === "string") return <p key={i} className="whitespace-pre-wrap">{p.text}</p>;
        if (p.data !== undefined)
          return (
            <pre key={i} className="overflow-x-auto rounded bg-slate-100 p-2 text-xs">
              {JSON.stringify(p.data, null, 2)}
            </pre>
          );
        if (typeof p.url === "string")
          return (
            <a key={i} href={p.url} className="text-blue-700 underline" target="_blank" rel="noreferrer">
              {String(p.filename ?? p.url)}
            </a>
          );
        return <em key={i} className="text-slate-500">binary content</em>;
      })}
    </div>
  );
}
