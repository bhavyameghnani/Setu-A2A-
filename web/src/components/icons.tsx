// A tiny inline icon set (24px grid, 1.6 stroke), so the app needs no icon dependency.
import type { SVGProps } from "react";

const PATHS = {
  home: "M3 10.5 12 3l9 7.5M5 9.5V20h5v-5h4v5h5V9.5",
  agents: "M12 3v3m-6 4h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2Zm3 4v1m6-1v1",
  rooms: "M4 5h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-4 3v-3H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm14 4h2a1 1 0 0 1 1 1v7l-3-2h-5",
  inbox: "M4 13l2.5-7h11L20 13m-16 0v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5m-16 0h4.5a2 2 0 0 0 4 0H20",
  policies: "M12 3 5 6v5c0 4.2 2.8 7.6 7 9 4.2-1.4 7-4.8 7-9V6l-7-3Zm-2.5 9 2 2 3.5-4",
  traces: "M6 5a2 2 0 1 0 0 .01M18 12a2 2 0 1 0 0 .01M6 19a2 2 0 1 0 0 .01M8 5h5a3 3 0 0 1 3 3v2M8 19h5a3 3 0 0 0 3-3v-2",
  audit: "M7 3h8l4 4v14H7V3Zm8 0v4h4M10 12h6m-6 3h6m-6 3h4",
  teams: "M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-6 8a6 6 0 0 1 12 0m1-8a2.5 2.5 0 1 0 0-5m2 13a5 5 0 0 0-3-4.6",
  analytics: "M4 4h7v7H4V4Zm9 0h7v4h-7V4ZM4 13h7v7H4v-7Zm9-3h7v10h-7V10Z",
  contacts: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  search: "m21 21-4.3-4.3M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z",
  plus: "M12 5v14M5 12h14",
  chevron: "m9 6 6 6-6 6",
  back: "M19 12H5m6-6-6 6 6 6",
  check: "m5 12 5 5 9-10",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 8l-4 4 4 4m-4-4h11",
  at: "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Zm0 0v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1",
  send: "M21 3 10 14M21 3l-7 18-4-7-7-4 18-7Z",
  more: "M12 6v.01M12 12v.01M12 18v.01",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  crown: "M3 8l4.5 4L12 5l4.5 7L21 8l-2 10H5L3 8Z",
  users: "M16 11a3 3 0 1 0 0-6m-8 6a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-5 8a5 5 0 0 1 10 0m1-5a5 5 0 0 1 7 4.6",
  tasks: "M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2",
  x: "M6 6l12 12M18 6 6 18",
  ban: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM5.6 5.6l12.8 12.8",
  bell: "M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6Zm4 9a2 2 0 0 0 4 0",
  play: "M8 5v14l11-7L8 5Z",
  alert: "M12 4 3 20h18L12 4Zm0 6v4m0 3v.01",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6v-9Z",
  book: "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3V4Zm0 13a3 3 0 0 1 3-3h11",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className = "h-[18px] w-[18px]", ...rest }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
