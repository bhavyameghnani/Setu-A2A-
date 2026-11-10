// Friendly names for the generated OpenAPI types (src/api/schema.d.ts, `npm run gen:api`).
import type { components } from "./schema";

type S = components["schemas"];

export type Me = S["MeOut"];
export type Participant = S["ParticipantOut"];
export type Department = S["DepartmentOut"];
export type Room = S["RoomOut"];
export type Member = S["MemberOut"];
export type Message = S["MessageOut"];
export type Task = S["TaskOut"];
export type Artifact = S["ArtifactOut"];
export type Approval = S["ApprovalOut"];
export type Notification = S["NotificationOut"];
export type NotificationList = S["NotificationList"];
export type Span = S["SpanOut"];
export type Trace = S["TraceOut"];
export type Policy = S["PolicyOut"];
export type AuditEvent = S["AuditEventOut"];
export type AuditVerify = S["AuditVerifyOut"];
export type Delegation = S["DelegationOut"];
export type AgentPublic = S["AgentOut"];
export type AgentAdmin = S["AgentAdminOut"];
export type Agent = AgentPublic | AgentAdmin;
export type AgentPreview = S["AgentPreviewOut"];
export type AgentStats = S["AgentStatsOut"];
export type Team = S["TeamOut"];
export type Analytics = S["AnalyticsSummary"];

export type Role = "dept_member" | "dept_admin" | "org_admin" | "auditor";

/** Admins see the registration details (endpoint, credentials flag); everyone else does not. */
export function isAdminView(a: Agent): a is AgentAdmin {
  return "rpc_url" in a;
}
