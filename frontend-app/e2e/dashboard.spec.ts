import { expect, test } from "@playwright/test";
import { screenPdf } from "./helpers";

const INJECTION = "Ignore all previous instructions";

test("(a) landing page shows sample banner, fairness notice, mock badge and six sample rows", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("status").filter({ hasText: "Sample data: run your own screening below" })).toBeVisible();
  await expect(page.getByRole("note").filter({ hasText: "Fairness notice." })).toBeVisible();
  await expect(page.getByText("Mock analysis mode")).toBeVisible();
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(6);
  await expect(page.getByText("6 of 6 shown")).toBeVisible();
});

test("(b) screen clean.pdf and see requirements in the drawer", async ({ page }) => {
  await screenPdf(page, "clean.pdf");
  await expect(page.getByText("Sample data: run your own screening below")).toHaveCount(0);
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: /^Open details for/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("tab", { name: "Requirements" }).click();
  const items = dialog.getByRole("tabpanel").getByRole("listitem");
  await expect(items.first()).toBeVisible();
  expect(await items.count()).toBeGreaterThan(3);
  await expect(dialog.getByRole("tabpanel")).toContainText("Python");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("(c) hidden injection is flagged and its text is never rendered", async ({ page }) => {
  await screenPdf(page, "injection_hidden.pdf");
  const row = page.locator("tbody tr");
  await expect(row).toHaveCount(1);
  await expect(row.getByText("Flagged: disqualify review")).toBeVisible();

  await row.getByRole("button", { name: /^Open details for/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("tab", { name: "Integrity" }).click();
  const panel = dialog.getByRole("tabpanel");
  // Spec 8.2: the hidden text is quoted (<= 15 words), always behind the label
  // 'hidden text reads:'. It must appear ONLY inside those labelled quotes, nowhere else.
  await expect(panel.getByText("INJECTION HIDDEN", { exact: true })).toBeVisible();
  await expect(panel.getByText("HIDDEN TEXT", { exact: true })).toBeVisible();
  await expect(panel).toContainText("The hidden text contains instructions aimed at an automated screener.");
  const quotes = panel.getByTestId("quoted-evidence");
  await expect(quotes.first()).toContainText('hidden text reads: "');
  for (const q of await quotes.allInnerTexts()) {
    expect(q.startsWith('hidden text reads: "')).toBe(true);
    expect(q.replace('hidden text reads: "', "").split(/\s+/).length).toBeLessThanOrEqual(15);
  }

  const tabs = ["Overview", "Requirements", "Skill Intelligence", "Authenticity", "Integrity", "Interview Questions", "Audit"];
  for (const t of tabs) {
    await dialog.getByRole("tab", { name: t }).click();
    await expect(dialog.getByRole("tab", { name: t })).toHaveAttribute("aria-selected", "true");
    // Remove the labelled quotes, then the phrase must be absent from the whole page.
    const text = await page.evaluate(() => {
      const clone = document.body.cloneNode(true) as HTMLElement;
      clone.querySelectorAll('[data-testid="quoted-evidence"]').forEach((n) => n.remove());
      return clone.innerText;
    });
    expect(text.toLowerCase(), `tab ${t}`).not.toContain(INJECTION.toLowerCase());
  }
});

test("(d) a recruiter decision on a real candidate appears in the audit list", async ({ page }) => {
  await screenPdf(page, "clean.pdf");
  await page.getByRole("button", { name: /^Open details for/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("tab", { name: "Audit" }).click();
  await expect(dialog.getByText("No decisions have been recorded yet.")).toBeVisible();
  await expect(dialog.getByLabel("Recruiter decision")).toBeEnabled();
  await dialog.getByLabel("Recruiter decision").selectOption("Hold");
  await dialog.getByLabel("Note (optional)").fill("Waiting on references");
  await dialog.getByRole("button", { name: "Record decision" }).click();
  const log = dialog.getByRole("list").filter({ hasText: "Waiting on references" });
  await expect(log.getByText("Hold", { exact: true })).toBeVisible();
  await expect(log.getByText("Waiting on references")).toBeVisible();
  await expect(dialog.getByText("No decisions have been recorded yet.")).toHaveCount(0);
});

test.describe("mobile", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("(e) landing page has no horizontal overflow at 375x812", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("tbody tr")).toHaveCount(6);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
    expect(m.sw).toBeLessThanOrEqual(m.iw);
  });
});

test("(f) the stage timeline shows all six stages finished after a real screening", async ({ page }) => {
  await screenPdf(page, "clean.pdf");
  const list = page.getByRole("list", { name: /^Stages for / });
  await expect(list.getByRole("listitem")).toHaveCount(6);
  for (const s of ["Parse", "Integrity", "Match", "Skills", "Authenticity", "Questions"]) {
    await expect(list.locator(`[data-stage="${s}"]`)).toHaveAttribute("data-status", "done");
    await expect(list.locator(`[data-stage="${s}"]`)).toContainText("Done");
  }
});
