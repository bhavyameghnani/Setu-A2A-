import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useDirectory } from "../api/hooks";
import type { Analytics as Data } from "../api/types";
import { Card, Empty, ErrorNote, Loading, PageTitle } from "../components/ui";
import { niceMax, seconds, ticks } from "../lib/charts";

// Chart colors. One series, so one categorical hue (slot 1 of the reference palette, validated for
// the light surface); status colors are used only for the failed number, with an icon and a label.
const INK = { primary: "#0b0b0b", secondary: "#52514e", muted: "#8a8984", grid: "#ececea", series: "#2a78d6" };

export function Analytics() {
  const [days, setDays] = useState(14);
  const [table, setTable] = useState(false);
  const q = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => api<Data>("/v1/analytics/summary", { query: { days } }),
    refetchInterval: 20_000,
  });
  const d = q.data;
  return (
    <div>
      <PageTitle
        right={
          <select aria-label="Time range" className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
          </select>
        }
      >
        Analytics
      </PageTitle>
      {q.isLoading && <Loading />}
      <ErrorNote error={q.error} />
      {d && (
        <>
          <p className="mb-4 text-sm text-slate-500">
            {d.scope === "org" ? "Every task in your organization" : "Tasks your department asked for or ran"}, created in the last {d.days} days.
          </p>
          <div className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label="Tasks" value={String(d.total)} hint={d.active ? `${d.active} running now` : "none running"} />
            <Stat label="Success rate" value={d.success_rate === null ? "—" : `${Math.round(d.success_rate * 100)}%`} hint={`${d.completed} completed`} />
            <Stat label="Failed" value={String(d.failed)} hint={d.failed ? "⚠ needs a look" : "✓ none"} />
            <Stat label="Average time" value={seconds(d.avg_seconds)} hint="completed tasks" />
          </div>
          {d.total === 0 ? (
            <Empty title="No tasks in this period">Send a task to an agent in a room and its numbers show up here.</Empty>
          ) : (
            <div className="grid gap-6 xl:grid-cols-[3fr_2fr]">
              <Card>
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-slate-800">Tasks per day</h2>
                  <button className="text-xs font-medium text-brand-700 hover:underline" onClick={() => setTable((t) => !t)}>
                    {table ? "Show chart" : "View as table"}
                  </button>
                </div>
                {table ? <DayTable d={d} /> : <Columns d={d} />}
              </Card>
              <Card>
                <h2 className="mb-3 text-sm font-semibold text-slate-800">Tasks by agent</h2>
                <AgentBars d={d} />
              </Card>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <Card>
      <p className="text-sm text-slate-500">{label}</p>
      <p className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">{value}</p>
      <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
    </Card>
  );
}

const short = (iso: string) => new Date(iso + "T00:00:00Z").toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" });

/** One series of columns: tasks created per day (UTC). Thin rounded columns on a hairline grid;
 *  hover or focus a column for its numbers. */
function Columns({ d }: { d: Data }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 240, L = 34, R = 8, T = 10, B = 26;
  const max = niceMax(Math.max(...d.per_day.map((x) => x.created), 1));
  const step = (W - L - R) / d.per_day.length;
  const bw = Math.min(24, step - 6);
  const y = (v: number) => T + (H - T - B) * (1 - v / max);
  const every = Math.ceil(d.per_day.length / 7);
  const h = hover === null ? null : d.per_day[hover];
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Tasks created per day, last ${d.days} days`} className="w-full">
        {ticks(max).map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke={INK.grid} strokeWidth={1} />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill={INK.muted}>{t}</text>
          </g>
        ))}
        {d.per_day.map((x, i) => {
          const cx = L + step * i + step / 2;
          const top = y(x.created);
          return (
            <g key={x.day} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(i)} onBlur={() => setHover(null)} tabIndex={0} aria-label={`${short(x.day)}: ${x.created} tasks, ${x.completed} completed, ${x.failed} failed`}>
              <rect x={cx - step / 2} y={T} width={step} height={H - T - B} fill="transparent" />
              {x.created > 0 && (
                <path
                  d={`M${cx - bw / 2},${y(0)} V${top + 4} a4,4 0 0 1 4,-4 h${bw - 8} a4,4 0 0 1 4,4 V${y(0)} Z`}
                  fill={INK.series}
                  opacity={hover === null || hover === i ? 1 : 0.55}
                />
              )}
              {(d.per_day.length - 1 - i) % every === 0 && <text x={cx} y={H - 8} textAnchor="middle" fontSize={11} fill={INK.muted}>{short(x.day)}</text>}
            </g>
          );
        })}
      </svg>
      {h && hover !== null && (
        <div
          role="status"
          className="pointer-events-none absolute -translate-x-1/2 whitespace-nowrap rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-lg"
          style={{ left: `${Math.min(86, Math.max(14, ((L + step * hover + step / 2) / W) * 100))}%`, top: 0 }}
        >
          <p className="font-medium text-slate-900">{short(h.day)}</p>
          <p className="text-slate-600">{h.created} created · {h.completed} completed · {h.failed} failed</p>
        </div>
      )}
    </div>
  );
}

function DayTable({ d }: { d: Data }) {
  return (
    <div className="max-h-72 overflow-y-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-slate-500">
          <tr><th className="py-1 font-medium">Day (UTC)</th><th className="font-medium">Created</th><th className="font-medium">Completed</th><th className="font-medium">Failed</th></tr>
        </thead>
        <tbody>
          {d.per_day.map((x) => (
            <tr key={x.day} className="border-t border-slate-100">
              <td className="py-1">{short(x.day)}</td><td>{x.created}</td><td>{x.completed}</td><td>{x.failed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Horizontal bars, one per agent, value at the tip; failures and average time as text. */
function AgentBars({ d }: { d: Data }) {
  const dir = useDirectory();
  const max = Math.max(...d.by_agent.map((a) => a.tasks), 1);
  return (
    <ul className="space-y-3" aria-label="Tasks by agent">
      {d.by_agent.map((a) => (
        <li key={a.participant_id} data-testid="agent-bar">
          <div className="flex items-baseline justify-between gap-2 text-sm">
            <span className="truncate font-medium text-slate-900">{dir.person(a.participant_id)?.display_name ?? "…"}</span>
            <span className="shrink-0 text-xs text-slate-500">
              {a.failed > 0 && <span className="mr-2 text-red-700">✗ {a.failed} failed</span>}
              {seconds(a.avg_seconds)} avg
            </span>
          </div>
          <div className="mt-1 flex items-center gap-2">
            <div className="h-3 flex-1 rounded-sm bg-slate-100">
              <div className="h-3 rounded-r-[4px]" style={{ width: `${(a.tasks / max) * 100}%`, background: INK.series }} />
            </div>
            <span className="w-6 text-right text-sm font-medium" style={{ color: INK.primary }}>{a.tasks}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
