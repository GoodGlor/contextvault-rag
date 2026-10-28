import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the FastAPI backend so the SPA and API share an
// origin in development (no CORS, cookies/headers flow through). The API client
// prefixes every request with /api (see src/api/client.ts).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        // Defaults to the standard dev backend; override (e.g. to run on an
        // alternate port alongside another project) with VITE_PROXY_TARGET.
        target: process.env.VITE_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: false,
    // Unit/component tests live under src/. e2e/ is Playwright (its own runner) and
    // must not be picked up by Vitest.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
