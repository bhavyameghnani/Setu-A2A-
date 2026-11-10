// Who may decide an approval. The server is the authority (it answers 403 otherwise); this only
// decides whether to *offer* the buttons, and mirrors `api/approvals.py::_may_decide`.
import type { Approval, Role } from "../api/types";

export type Viewer = { id: string; departmentId: string; roles: Role[] };

const has = (v: Viewer, role: Role) => v.roles.includes(role);

export function canDecide(a: Approval, v: Viewer, requesterId?: string | null): boolean {
  if (a.state !== "pending") return false;
  const isAdminOf = has(v, "org_admin") || (has(v, "dept_admin") && v.departmentId === a.approver_department_id);
  if (a.kind === "agent") return v.id === a.approver_id || isAdminOf;
  if (requesterId && v.id === requesterId) return false; // nobody approves their own request
  if (has(v, "org_admin")) return true;
  if (v.departmentId !== a.approver_department_id) return false;
  return a.approver_role === "dept_member"
    ? has(v, "dept_member") || has(v, "dept_admin")
    : has(v, a.approver_role as Role);
}
