import { describe, expect, it } from "vitest";
import { detailOf } from "../api/client";
import type { Approval } from "../api/types";
import { canDecide } from "./approvals";
import { agentHealth, dayLabel, duration, isLive, sameDay, stateStyle, timeAgo } from "./format";
import { activeMention, insertMention, mentionedIds, splitMentions, suggestions } from "./mentions";
import { buildRule, describeRule } from "./policy";
import { parseRoute } from "./router";
import { niceMax, seconds, ticks } from "./charts";
import { verifySignature } from "./signature";
import { slugify, validSlug } from "./slug";

describe("router", () => {
  it("parses every page, defaulting to home", () => {
    expect(parseRoute("")).toEqual({ name: "home" });
    expect(parseRoute("#/")).toEqual({ name: "home" });
    expect(parseRoute("#/teams")).toEqual({ name: "teams" });
    expect(parseRoute("#/analytics")).toEqual({ name: "analytics" });
    expect(parseRoute("#/contacts")).toEqual({ name: "contacts" });
    expect(parseRoute("#/rooms")).toEqual({ name: "rooms" });
    expect(parseRoute("#/rooms/abc")).toEqual({ name: "room", id: "abc" });
    expect(parseRoute("#/inbox")).toEqual({ name: "inbox" });
    expect(parseRoute("#/traces")).toEqual({ name: "traces", id: undefined });
    expect(parseRoute("#/traces/t1")).toEqual({ name: "traces", id: "t1" });
    expect(parseRoute("#/audit")).toEqual({ name: "audit" });
    expect(parseRoute("#/nope")).toEqual({ name: "notfound" });
  });
});

describe("mentions", () => {
  const people = [
    { id: "1", handle: "triage-agent" },
    { id: "2", handle: "olga" },
    { id: "3", handle: "risk.agent" },
  ];

  it("finds the mention being typed at the caret", () => {
    expect(activeMention("hello @tr", 9)).toEqual({ start: 6, query: "tr" });
    expect(activeMention("@", 1)).toEqual({ start: 0, query: "" });
    expect(activeMention("mail me@tr", 10)).toBeNull(); // an email is not a mention
    expect(activeMention("hello @tr there", 15)).toBeNull(); // caret is past it
    expect(activeMention("hello", 5)).toBeNull();
  });

  it("completes a mention and keeps the caret after it", () => {
    const r = insertMention("hi @tr please", 3, 6, "triage-agent");
    expect(r.text).toBe("hi @triage-agent  please");
    expect(r.caret).toBe(3 + "triage-agent".length + 2);
  });

  it("suggests by handle, case-insensitively, capped", () => {
    expect(suggestions(people, "AG").map((p) => p.id)).toEqual(["1", "3"]);
    expect(suggestions(people, "", 2)).toHaveLength(2);
  });

  it("maps @handles in text to member ids, once each", () => {
    expect(mentionedIds("@triage-agent please ask @olga, and @triage-agent again", people)).toEqual(["1", "2"]);
    expect(mentionedIds("@risk.agent. thanks", people)).toEqual(["3"]); // trailing dot is punctuation
    expect(mentionedIds("@nobody here", people)).toEqual([]);
  });

  it("splits text so only real mentions are highlighted", () => {
    expect(splitMentions("ask @olga or @ghost now", people)).toEqual([
      { text: "ask " },
      { text: "@olga", mention: people[1] },
      { text: " or @ghost now" },
    ]);
    expect(splitMentions("plain", people)).toEqual([{ text: "plain" }]);
  });
});

const approval = (o: Partial<Approval>): Approval =>
  ({
    id: "a",
    task_id: "t",
    kind: "policy",
    state: "pending",
    approver_department_id: "comp",
    approver_role: "dept_admin",
    approver_id: null,
    ...o,
  }) as Approval;

describe("who may decide an approval", () => {
  const carl = { id: "carl", departmentId: "comp", roles: ["dept_admin" as const] };
  it("offers a policy approval only to the right role in the approver department", () => {
    expect(canDecide(approval({}), carl, "olga")).toBe(true);
    expect(canDecide(approval({}), { ...carl, roles: ["dept_member"] }, "olga")).toBe(false);
    expect(canDecide(approval({}), { ...carl, departmentId: "ops" }, "olga")).toBe(false);
    expect(canDecide(approval({ approver_role: "dept_member" }), { ...carl, roles: ["dept_member"] }, "olga")).toBe(true);
  });
  it("never lets people approve their own request, but org admins decide anything else", () => {
    expect(canDecide(approval({}), carl, "carl")).toBe(false);
    const admin = { id: "root", departmentId: "ops", roles: ["org_admin" as const] };
    expect(canDecide(approval({}), admin, "olga")).toBe(true);
  });
  it("answers an agent's question for the person it asked, or a department admin", () => {
    const q = approval({ kind: "agent", approver_id: "olga", approver_department_id: "ops", approver_role: "dept_member" });
    expect(canDecide(q, { id: "olga", departmentId: "ops", roles: ["dept_member"] })).toBe(true);
    expect(canDecide(q, { id: "otto", departmentId: "ops", roles: ["dept_member"] })).toBe(false);
    expect(canDecide(q, { id: "boss", departmentId: "ops", roles: ["dept_admin"] })).toBe(true);
  });
  it("offers nothing once decided", () => {
    expect(canDecide(approval({ state: "approved" }), carl, "olga")).toBe(false);
  });
});

describe("policy rules", () => {
  const dept = (id: string) => ({ ops: "Operations", risk: "Risk", comp: "Compliance" })[id] ?? id;
  it("describes each rule type", () => {
    expect(describeRule({ type: "allow_talk", from_department: "ops", to_department: "risk" }, dept)).toBe(
      "Allow Operations to talk to Risk",
    );
    expect(describeRule({ type: "deny_talk", from_department: "*", to_department: "risk" }, dept)).toBe(
      "Deny any department talking to Risk",
    );
    expect(
      describeRule(
        { type: "require_approval", skill: "risk.score", approver_department: "comp", approver_role: "dept_admin", from_department: "*", to_department: "*" },
        dept,
      ),
    ).toBe("skill risk.score (any department → any department) needs a dept_admin of Compliance to approve");
  });
  it("builds rule documents from form fields, defaulting blanks to any", () => {
    expect(buildRule("allow_talk", { from: "ops", to: "" })).toEqual({
      type: "allow_talk",
      from_department: "ops",
      to_department: "*",
    });
    expect(buildRule("topic_block", { topics: "a, b ,," })).toMatchObject({ topics: ["a", "b"] });
    expect(buildRule("require_approval", { approverDept: "comp", skill: "x.y", matchBy: "skill" })).toEqual({
      type: "require_approval",
      approver_department: "comp",
      approver_role: "dept_admin",
      from_department: "*",
      to_department: "*",
      skill: "x.y",
    });
    expect(buildRule("require_approval", { approverDept: "comp", matchBy: "kind", kind: "text" })).toMatchObject({ kind: "text" });
  });
});

describe("formatting", () => {
  it("renders relative times and durations", () => {
    const now = Date.parse("2026-11-06T12:00:00Z");
    expect(timeAgo("2026-11-06T11:59:58Z", now)).toBe("just now");
    expect(timeAgo("2026-11-06T11:59:30Z", now)).toBe("30s ago");
    expect(timeAgo("2026-11-06T11:15:00Z", now)).toBe("45m ago");
    expect(timeAgo("2026-11-06T09:00:00Z", now)).toBe("3h ago");
    expect(duration(160)).toBe("160 ms");
    expect(duration(2500)).toBe("2.5 s");
    expect(duration(null)).toBe("");
  });
  it("knows which task states are still live and how to colour them", () => {
    expect(["submitted", "working", "input_required"].every(isLive)).toBe(true);
    expect(["completed", "failed", "canceled", "rejected"].some(isLive)).toBe(false);
    expect(stateStyle("completed")).toContain("green");
    expect(stateStyle("rejected")).toContain("red");
    expect(stateStyle("input_required")).toContain("amber");
  });
  it("derives agent health from the age of the last card fetch", () => {
    const now = Date.parse("2026-11-06T12:00:00Z");
    expect(agentHealth(true, "2026-11-06T11:30:00Z", now).label).toBe("ok");
    expect(agentHealth(true, "2026-11-06T08:00:00Z", now).label).toBe("stale card");
    expect(agentHealth(false, "2026-11-06T11:30:00Z", now).label).toBe("disabled");
    expect(agentHealth(true, null, now).label).toBe("unknown");
  });
});

describe("api errors", () => {
  it("reads FastAPI's string and validation-list details", () => {
    expect(detailOf({ detail: "room not found" }, "x")).toBe("room not found");
    expect(
      detailOf({ detail: [{ loc: ["body", "rule", "type"], msg: "Field required" }] }, "x"),
    ).toBe("rule.type: Field required");
    expect(detailOf(null, "Bad Gateway")).toBe("Bad Gateway");
  });
});

describe("result signatures", () => {
  // Produced by the server's Signer (server/app/core/signing.py) over these parts.
  const token =
    "eyJhbGciOiJFZERTQSIsImtpZCI6IjAyZDk3MWQ5NmY4MzgyOTUiLCJ0eXAiOiJzZXR1LWFydGlmYWN0K2p3dCJ9.eyJpc3MiOiJodHRwOi8vaHViIiwic3ViIjoiYWcxIiwiaWF0IjoxNzAwMDAwMDAwLCJ0YXNrIjoidDEiLCJyb29tIjoicjEiLCJhcnRpZmFjdCI6ImExIiwic2hhMjU2IjoiYzk4NTk0ZWFlYWY3MDFhMmQwMDhjM2Q3MThkMGVkNDc2YzhjNWI3ZGVlOWRkOTg1MjE4YTE3NWQ3ODVlOTE0NCIsInRyYWNlIjoidHIxIn0.rjGrtyPUGUEOSZCQ2RXCAqQ9YC-pj2Lks1bvlm3Gdb8nG-e3-yU9aoPBP154-bmVUtEKKY9Lu2Y9UAkk42UiDw";
  const parts = [{ text: "héllo ☃" }, { data: { z: 1, a: [1.5, true, null], n: { y: 2, b: "x" } } }];
  const jwks = { keys: [{ kty: "OKP", crv: "Ed25519", kid: "02d971d96f838295", x: "P9a0WA-OhbE8TSva09eoE_uym6m2qFyiB6m5hUlUUJM" }] };

  it("verifies what the server signed", async () => {
    const s = await verifySignature(token, parts, jwks);
    expect(s.state).toBe("verified");
  });
  it("rejects changed content, a wrong key and garbage", async () => {
    expect((await verifySignature(token, [{ text: "forged" }], jwks)).state).toBe("invalid");
    expect((await verifySignature(token, parts, { keys: [] })).state).toBe("invalid");
    expect((await verifySignature("not.a.jwt", parts, jwks)).state).toBe("invalid");
    expect((await verifySignature(token.slice(0, -4) + "AAAA", parts, jwks)).state).toBe("invalid");
  });
});

describe("department slugs", () => {
  it("derives a valid slug from a name", () => {
    expect(slugify("Risk & Compliance")).toBe("risk-compliance");
    expect(slugify("  Opérations  ")).toBe("operations");
    expect(slugify("R&D 2")).toBe("r-d-2");
    expect(slugify("!!!")).toBe("");
    expect(validSlug(slugify("Customer Support"))).toBe(true);
    expect(validSlug("")).toBe(false);
    expect(validSlug("-x")).toBe(false);
    expect(validSlug("Risk")).toBe(false);
  });
});

describe("date dividers", () => {
  const now = new Date(2026, 9, 18, 15, 0).getTime(); // Sun 18 Oct 2026, 15:00 local
  it("says Today, Yesterday, or the date", () => {
    expect(dayLabel(new Date(2026, 9, 18, 9, 30).toISOString(), now)).toBe("Today");
    expect(dayLabel(new Date(2026, 9, 17, 23, 59).toISOString(), now)).toBe("Yesterday");
    expect(dayLabel(new Date(2026, 9, 1, 12, 0).toISOString(), now)).toMatch(/Oct/);
  });
  it("compares days in local time", () => {
    expect(sameDay(new Date(2026, 9, 18, 0, 1).toISOString(), new Date(2026, 9, 18, 23, 59).toISOString())).toBe(true);
    expect(sameDay(new Date(2026, 9, 18, 23, 59).toISOString(), new Date(2026, 9, 19, 0, 1).toISOString())).toBe(false);
  });
});

describe("chart helpers", () => {
  it("rounds an axis maximum up to a clean number", () => {
    expect(niceMax(0)).toBe(4);
    expect(niceMax(3)).toBe(4);
    expect(niceMax(7)).toBe(8);
    expect(niceMax(11)).toBe(12);
    expect(niceMax(87)).toBe(100);
    expect(niceMax(1234)).toBe(1500);
  });
  it("makes whole-number ticks from zero", () => {
    expect(ticks(4)).toEqual([0, 1, 2, 3, 4]);
    expect(ticks(100)).toEqual([0, 25, 50, 75, 100]);
  });
  it("writes durations people can read", () => {
    expect(seconds(null)).toBe("—");
    expect(seconds(0.4)).toBe("<1 s");
    expect(seconds(12.3)).toBe("12 s");
    expect(seconds(95)).toBe("1 min 35 s");
    expect(seconds(3700)).toBe("1 h 2 min");
  });
});
