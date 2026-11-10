import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useDirectory } from "../api/hooks";
import type { Department } from "../api/types";
import { Btn, Card, ErrorNote, Field, inputClass } from "./ui";
import { slugify, validSlug } from "../lib/slug";
import { toast } from "../lib/toast";

/** Org admins create departments here. The server enforces the role and slug uniqueness. */
export function NewDepartment() {
  const qc = useQueryClient();
  const dir = useDirectory();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const shownSlug = slugEdited ? slug : slugify(name);
  const create = useMutation({
    mutationFn: () => api<Department>("/v1/departments", { body: { name: name.trim(), slug: shownSlug } }),
    onSuccess: (d) => {
      toast(`Department “${d.name}” created`);
      setName("");
      setSlug("");
      setSlugEdited(false);
      qc.invalidateQueries({ queryKey: ["departments"] });
    },
  });
  const ok = name.trim() !== "" && validSlug(shownSlug);
  return (
    <Card>
      <form
        className="space-y-3"
        data-testid="new-department"
        onSubmit={(e) => {
          e.preventDefault();
          if (ok) create.mutate();
        }}
      >
        <Field label="Department name">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="Slug" hint="Short id: lowercase letters, digits and hyphens. Unique in your organization.">
          <input
            className={inputClass}
            value={shownSlug}
            onChange={(e) => {
              setSlug(e.target.value);
              setSlugEdited(true);
            }}
            required
          />
        </Field>
        <ErrorNote error={create.error} />
        <Btn tone="primary" type="submit" disabled={!ok || create.isPending}>Create department</Btn>
      </form>
      <p className="mt-3 text-xs text-slate-500">
        Existing: {dir.departments.length ? dir.departments.map((d) => d.name).join(", ") : "none yet"}
      </p>
    </Card>
  );
}
