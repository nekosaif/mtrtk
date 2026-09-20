import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

// The daemon (uvicorn) listens on :8080; `pnpm dev` proxies the API, the
// liveness route and the WebSocket to it so the SPA can be developed against
// a running `mtrtk base` / `mtrtk replay`.
const DAEMON = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": DAEMON,
      "/healthz": DAEMON,
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
  // `dist/` is what docker/Dockerfile copies into src/mtrtk/web/static; keep it.
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
