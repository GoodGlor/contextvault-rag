import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end config. The tests drive the *running* app, so bring the full stack up
 * first with `./dev.sh` (db + migrations + seeded admin + backend + frontend), then
 * run `npm run test:e2e`. baseURL points at the Vite dev server.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  // One worker, and it is not a speed preference. Every spec signs in as the
  // same seeded `admin` against one backend and one database, so parallel
  // workers race each other's session and fixtures: the whole suite then fails
  // at the sign-in step, and Playwright reports multi-minute durations against
  // a 30 s test timeout, which reads as a hung server rather than contention.
  // `fullyParallel: false` alone does NOT prevent this — it only stops tests
  // *within* a file from running together, while separate files still get a
  // worker each. Measured: 0/14 with the default 4 workers, 14/14 in 27 s here.
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
