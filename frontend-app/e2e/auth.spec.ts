import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

// Runs against the backend started with SCREENING_API_KEYS (port 8091, see playwright.config.ts).
const KEY = "e2e-test-key-do-not-use-in-prod";

test("key prompt, wrong key error, right key loads samples, sign out", async ({ page }) => {
  await page.goto("/");
  const input = page.getByLabel("API key", { exact: true });
  await expect(input).toBeVisible();
  await expect(input).toHaveAttribute("type", "password");
  const { violations } = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  expect(violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);

  await input.fill("not-the-key");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText("not accepted");
  await expect(page.locator("tbody tr")).toHaveCount(0);

  await page.getByRole("button", { name: "Show key" }).click();
  await expect(input).toHaveAttribute("type", "text");
  await input.fill(KEY);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(6);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain(KEY);
  expect(page.url()).not.toContain(KEY);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("API key", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
});

test("API requests without a valid key are refused, health stays public", async ({ request }) => {
  const denied = await request.get("/v1/samples");
  expect(denied.status()).toBe(401);
  expect(denied.headers()["www-authenticate"]).toBe("Bearer");
  expect((await denied.json()).code).toBe("UNAUTHORIZED");
  expect((await request.get(`/v1/samples?api_key=${KEY}`)).status()).toBe(401);
  expect((await request.get("/v1/samples", { headers: { Authorization: "Bearer wrong" } })).status()).toBe(401);
  expect((await request.get("/v1/samples", { headers: { Authorization: `Bearer ${KEY}` } })).status()).toBe(200);
  expect((await request.get("/v1/samples", { headers: { "X-API-Key": KEY } })).status()).toBe(200);
  const health = await request.get("/v1/health");
  expect((await health.json()).auth_required).toBe(true);
});

// Per-recruiter ownership (A-30): keys for two recruiters and an admin, set in playwright.config.ts.
const KEY_B = "e2e-test-key-b-do-not-use-in-prod";
const KEY_ADMIN = "e2e-test-key-admin-do-not-use-in-prod";
const bearer = (k: string) => ({ Authorization: `Bearer ${k}` });

test("a candidate screened by recruiter A is 404 for recruiter B and visible to an admin", async ({ request }) => {
  const created = await request.post("/v1/screenings", {
    headers: bearer(KEY),
    multipart: {
      jd_text: "Backend engineer. Must have Python and SQL.",
      pasted_resumes: "Ada Owner\nBackend engineer with 5 years of Python and SQL experience.",
    },
  });
  expect(created.status()).toBe(200);
  const sid = (await created.json()).screening_id as string;
  let candidates: { candidate_id: string }[] = [];
  await expect.poll(async () => {
    const r = await request.get(`/v1/screenings/${sid}`, { headers: bearer(KEY) });
    const body = await r.json();
    candidates = body.candidates;
    return body.status;
  }, { timeout: 45_000 }).toBe("complete");
  const cid = candidates[0].candidate_id;

  const paths = [`/v1/screenings/${sid}`, `/v1/candidates/${cid}`, `/v1/candidates/${cid}/audit`, `/v1/authenticity/${cid}`];
  for (const p of paths) {
    expect((await request.get(p, { headers: bearer(KEY) })).status(), `owner ${p}`).toBe(200);
    expect((await request.get(p, { headers: bearer(KEY_ADMIN) })).status(), `admin ${p}`).toBe(200);
    const other = await request.get(p, { headers: bearer(KEY_B) });
    expect(other.status(), `other ${p}`).toBe(404);
    expect((await other.json()).detail.code).toBe("NOT_FOUND");
  }
  const decide = await request.post(`/v1/candidates/${cid}/decision`, { headers: bearer(KEY_B), form: { decision: "Hold" } });
  expect(decide.status()).toBe(404);
  expect((await request.delete(`/v1/candidates/${cid}`, { headers: bearer(KEY_B) })).status()).toBe(404);
  expect((await request.get("/v1/samples", { headers: bearer(KEY_B) })).status()).toBe(200);

  // The admin may erase it; afterwards it is gone for everyone.
  const erased = await request.delete(`/v1/candidates/${cid}`, { headers: bearer(KEY_ADMIN) });
  expect(erased.status()).toBe(200);
  expect((await erased.json()).erased.candidates).toBe(1);
  expect((await request.get(`/v1/candidates/${cid}`, { headers: bearer(KEY) })).status()).toBe(404);
});
