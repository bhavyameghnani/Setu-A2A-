import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { Agent, AgentStats, Approval, Department, Me, Participant, Role } from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/v1/auth/me"), staleTime: 5 * 60_000 });
}

export type Viewer = { id: string; departmentId: string; roles: Role[]; name: string };

export function useViewer(): Viewer | null {
  const me = useMe().data;
  if (!me) return null;
  return {
    id: me.participant.id,
    departmentId: me.participant.department_id,
    roles: me.roles as Role[],
    name: me.participant.display_name,
  };
}

/** Departments and participants of the org, with lookups. Cheap, so cached for a minute. */
export function useDirectory() {
  const depts = useQuery({
    queryKey: ["departments"],
    queryFn: () => api<Department[]>("/v1/departments"),
    staleTime: 60_000,
  });
  const people = useQuery({
    queryKey: ["participants"],
    queryFn: () => api<Participant[]>("/v1/participants"),
    staleTime: 60_000,
  });
  const deptMap = new Map((depts.data ?? []).map((d) => [d.id, d]));
  const personMap = new Map((people.data ?? []).map((p) => [p.id, p]));
  return {
    departments: depts.data ?? [],
    participants: people.data ?? [],
    dept: (id: string | null | undefined) => (id ? (deptMap.get(id)?.name ?? "…") : "…"),
    person: (id: string | null | undefined) => (id ? personMap.get(id) : undefined),
    ready: depts.isSuccess && people.isSuccess,
  };
}

export function usePendingApprovals() {
  return useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => api<Approval[]>("/v1/approvals", { query: { state: "pending" } }),
    refetchInterval: 10_000,
  });
}

export function useAgents() {
  return useQuery({ queryKey: ["agents"], queryFn: () => api<Agent[]>("/v1/agents"), staleTime: 15_000 });
}

/** Activity counts per agent, keyed by participant id. Empty for people who may not see them. */
export function useAgentStats() {
  return useQuery({
    queryKey: ["agent-stats"],
    queryFn: async () => new Map((await api<AgentStats[]>("/v1/agents/stats")).map((s) => [s.participant_id, s])),
    staleTime: 15_000,
  });
}

export const isAdmin = (roles: Role[]) => roles.includes("org_admin") || roles.includes("dept_admin");
export const isOversight = (roles: Role[]) => roles.includes("org_admin") || roles.includes("auditor");
