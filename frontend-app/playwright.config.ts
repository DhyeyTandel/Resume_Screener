import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const PORT = 8090;
const AUTH_PORT = 8091; // second backend started WITH an API key, for e2e/auth.spec.ts only
const AUTH_KEY = "e2e-test-key-do-not-use-in-prod";
const AUTH_KEY_B = "e2e-test-key-b-do-not-use-in-prod"; // a second recruiter
const AUTH_KEY_ADMIN = "e2e-test-key-admin-do-not-use-in-prod"; // label "e2e-admin" is an admin
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// A throwaway DB so the e2e run never touches real data. Set once, inherited by workers.
process.env.E2E_DB_DIR ??= mkdtempSync(join(tmpdir(), "screening-e2e-"));

// Locally use the repo venv; in CI the interpreter on PATH already has the requirements.
const venvPython = join(repoRoot, ".venv", "bin", "python");
const python = existsSync(venvPython) ? venvPython : "python";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", testIgnore: /auth\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
    {
      name: "auth",
      testMatch: /auth\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${AUTH_PORT}` },
    },
  ],
  webServer: [{
    // Serves the built frontend-app/dist, so run `npm run build` first.
    command: `${python} -m uvicorn backend.app.main:app --host 127.0.0.1 --port ${PORT}`,
    cwd: repoRoot,
    url: `http://127.0.0.1:${PORT}/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      LLM_PROVIDER: "mock",
      SCREENING_DB_PATH: join(process.env.E2E_DB_DIR, "screening.db"),
    },
  },
  {
    command: `${python} -m uvicorn backend.app.main:app --host 127.0.0.1 --port ${AUTH_PORT}`,
    cwd: repoRoot,
    url: `http://127.0.0.1:${AUTH_PORT}/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      LLM_PROVIDER: "mock",
      SCREENING_DB_PATH: join(process.env.E2E_DB_DIR, "screening-auth.db"),
      SCREENING_API_KEYS: `e2e:${AUTH_KEY},e2e-b:${AUTH_KEY_B},e2e-admin:${AUTH_KEY_ADMIN}`,
      SCREENING_ADMIN_LABELS: "e2e-admin",
    },
  }],
});
