import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api, session } from "../api/client";
import type { components } from "../api/schema";
import { Btn, ErrorNote, Field, inputClass } from "../components/ui";

type LoginResponse = components["schemas"]["LoginResponse"];

export function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => api<LoginResponse>("/v1/auth/login", { body: { email, password } }),
    onSuccess: (r) => {
      session.set(r.access_token);
      onSignedIn();
    },
  });
  return (
    <main className="mx-auto mt-24 max-w-sm px-4">
      <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-lg font-bold text-white">S</div>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Setu</h1>
      <p className="mb-6 text-sm text-slate-600">
        Sign in. You will see the rooms, tasks, traces and notifications your department is entitled to.
      </p>
      <form
        className="space-y-3 rounded-xl border border-slate-200 bg-white p-5 shadow-card"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <Field label="Email">
          <input
            className={inputClass}
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </Field>
        <Field label="Password">
          <input
            className={inputClass}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        <ErrorNote error={login.error} />
        <Btn tone="primary" type="submit" className="w-full" disabled={login.isPending}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </Btn>
      </form>
    </main>
  );
}
