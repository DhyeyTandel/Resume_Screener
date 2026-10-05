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
