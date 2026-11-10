import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser talks to Setu through this dev/preview proxy, so everything is same-origin: no
// CORS on the server, and WebSockets (`ws: true`) work with the token in the query string.
const api = process.env.SETU_API ?? "http://localhost:8000";
const proxy = {
  "/v1": { target: api, ws: true, changeOrigin: true },
  "/.well-known/jwks.json": { target: api, changeOrigin: true }, // Setu's public signing key
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
