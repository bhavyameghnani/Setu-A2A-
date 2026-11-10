// Small pure helpers for the Analytics charts.

/** The smallest "clean" axis maximum at or above `max` (4, 8, 12, 16 ... then 1/1.5/2/3/5 x 10^n). */
export function niceMax(max: number): number {
  if (max <= 16) return Math.max(4, Math.ceil(max / 4) * 4);
  const pow = 10 ** Math.floor(Math.log10(max));
  for (const m of [1, 1.5, 2, 3, 5, 10]) if (m * pow >= max) return m * pow;
  return 10 * pow;
}

/** Five evenly spaced tick values from 0 to `max` (inclusive), as whole numbers. */
export const ticks = (max: number): number[] => [0, 1, 2, 3, 4].map((i) => Math.round((max * i) / 4));

export function seconds(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 1) return "<1 s";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}
