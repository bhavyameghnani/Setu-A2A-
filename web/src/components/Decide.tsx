import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { Approval } from "../api/types";
import { toast } from "../lib/toast";
import { Btn, ErrorNote, inputClass } from "./ui";

/** Approve / deny, inline. Used wherever an approval shows up (room, task card, inbox). */
export function Decide({ approvalId, summary }: { approvalId: string; summary?: string }) {
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: (approve: boolean) =>
      api<Approval>(`/v1/approvals/${approvalId}/decide`, { body: { approve, note: note || null } }),
    onSuccess: (a) => {
      toast(`${a.state === "approved" ? "Approved" : "Denied"}${summary ? `: ${summary}` : ""}`);
      for (const k of ["approvals", "tasks", "inbox", "unread"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
  return (
    <div className="mt-2 space-y-2">
      <input
        aria-label="Note (optional)"
        className={inputClass}
        placeholder="Note (optional; for a question, this is your answer)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="flex gap-2">
        <Btn tone="good" disabled={decide.isPending} onClick={() => decide.mutate(true)}>
          Approve
        </Btn>
        <Btn tone="danger" disabled={decide.isPending} onClick={() => decide.mutate(false)}>
          Deny
        </Btn>
      </div>
      <ErrorNote error={decide.error} />
    </div>
  );
}
