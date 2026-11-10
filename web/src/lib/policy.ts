// Policy rules in words.
export type Rule = Record<string, unknown>;

export const RULE_TYPES = ["allow_talk", "deny_talk", "topic_block", "require_approval"] as const;
export type RuleType = (typeof RULE_TYPES)[number];

export function describeRule(rule: Rule, dept: (id: string) => string): string {
  const d = (v: unknown) => (v === undefined || v === "*" ? "any department" : dept(String(v)));
  switch (rule.type) {
    case "allow_talk":
      return `Allow ${d(rule.from_department)} to talk to ${d(rule.to_department)}`;
    case "deny_talk":
      return `Deny ${d(rule.from_department)} talking to ${d(rule.to_department)}`;
    case "topic_block":
      return `Block ${((rule.topics as string[]) ?? []).join(", ")} from ${d(rule.from_department)} to ${d(rule.to_department)}`;
    case "require_approval": {
      const what = rule.skill ? `skill ${String(rule.skill)}` : `${String(rule.kind)} messages`;
      return `${what} (${d(rule.from_department)} → ${d(rule.to_department)}) needs a ${String(rule.approver_role)} of ${d(rule.approver_department)} to approve`;
    }
    default:
      return JSON.stringify(rule);
  }
}

/** Build a rule document from the form's fields, leaving out anything blank. */
export function buildRule(type: RuleType, f: Record<string, string>): Rule {
  const any = (v: string | undefined) => (v && v !== "" ? v : "*");
  switch (type) {
    case "allow_talk":
    case "deny_talk":
      return { type, from_department: any(f.from), to_department: any(f.to) };
    case "topic_block":
      return {
        type,
        from_department: any(f.from),
        to_department: any(f.to),
        topics: (f.topics ?? "").split(",").map((t) => t.trim()).filter(Boolean),
      };
    case "require_approval": {
      const rule: Rule = {
        type,
        approver_department: f.approverDept,
        approver_role: f.approverRole || "dept_admin",
        from_department: any(f.from),
        to_department: any(f.to),
      };
      if (f.matchBy === "kind") rule.kind = f.kind || "task";
      else rule.skill = f.skill || "*";
      return rule;
    }
  }
}
