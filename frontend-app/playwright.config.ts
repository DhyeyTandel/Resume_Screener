import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const PORT = 8090;
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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
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
});
