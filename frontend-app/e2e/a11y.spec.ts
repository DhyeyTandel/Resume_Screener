import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { pdf } from "./helpers";

const TABS = ["Overview", "Requirements", "Skill Intelligence", "Authenticity", "Integrity", "Interview Questions", "Audit"];

/** Fails on serious or critical violations only, and prints each one with its rule id. */
async function expectNoSeriousViolations(page: Page, label: string, include?: string) {
  let builder = new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]);
  if (include) builder = builder.include(include);
  const { violations } = await builder.analyze();
  const bad = violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = bad.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
  expect(report, `axe violations on ${label}`).toEqual([]);
}

test("axe: landing page", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("tbody tr")).toHaveCount(6);
  await expectNoSeriousViolations(page, "landing page");
});

test("axe: upload panel with files, evidence sources open and an unreadable file", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Upload resumes").setInputFiles([{ name: "clean.pdf", mimeType: "application/pdf", buffer: readFileSync(pdf("clean.pdf")) }, { name: "notes.exe", mimeType: "application/octet-stream", buffer: Buffer.from("x") }]);
  await expect(page.getByRole("list", { name: "Selected resume files" })).toContainText("clean.pdf");
  await page.getByText("Optional evidence sources").click();
  await expect(page.getByLabel("GitHub username")).toBeVisible();
  await expectNoSeriousViolations(page, "upload panel");
});

test("axe: progress timeline while and after screening", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Load sample JD" }).click();
  await page.getByLabel("Upload resumes").setInputFiles(pdf("clean.pdf"));
  await page.getByRole("button", { name: "Screen Candidates" }).click();
  await expect(page.getByTestId("stage-timeline")).toBeVisible();
  await expect(page.getByText(/Done\. 1 candidate screened/)).toBeVisible({ timeout: 45_000 });
  await expectNoSeriousViolations(page, "results after screening");
});

test("axe: candidate drawer, every tab", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("tbody tr")).toHaveCount(6);
  await page.getByRole("button", { name: /^Open details for/ }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  for (const t of TABS) {
    await dialog.getByRole("tab", { name: t }).click();
    await expect(dialog.getByRole("tab", { name: t })).toHaveAttribute("aria-selected", "true");
    await expectNoSeriousViolations(page, `drawer tab ${t}`);
  }
});

test("axe: drawer on a flagged candidate (integrity findings)", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("row").filter({ hasText: "Flagged:" }).first().getByRole("button", { name: /^Open details for/ }).click();
  const dialog = page.getByRole("dialog");
  for (const t of TABS) {
    await dialog.getByRole("tab", { name: t }).click();
    await expectNoSeriousViolations(page, `flagged drawer tab ${t}`);
  }
});

test("keyboard only: reach a candidate, open it, switch tabs with arrows, Escape returns focus", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("tbody tr")).toHaveCount(6);
  const focused = () => page.evaluate(() => {
    const a = document.activeElement as HTMLElement | null;
    return { tag: a?.tagName ?? "", label: a?.getAttribute("aria-label") ?? a?.textContent?.trim() ?? "" };
  });

  // Tab from the very top of the page until the first candidate button is focused.
  let found = false;
  for (let i = 0; i < 40 && !found; i++) {
    await page.keyboard.press("Tab");
    found = /^Open details for/.test((await focused()).label);
  }
  expect(found, "a candidate button is reachable with Tab").toBe(true);
  const trigger = (await focused()).label;

  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // Focus moved into the dialog.
  await expect.poll(() => page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'))).toBe(true);

  // Tab order inside the drawer: Close, then the selected tab only (roving tabindex).
  await page.keyboard.press("Tab");
  expect((await focused()).label).toBe("Close");
  await page.keyboard.press("Tab");
  const overview = dialog.getByRole("tab", { name: "Overview" });
  await expect(overview).toBeFocused();
  await expect(dialog.getByRole("tab", { name: "Requirements" })).toHaveAttribute("tabindex", "-1");

  // Arrow keys move selection and focus together; Home, End and wrap-around work.
  await page.keyboard.press("ArrowRight");
  await expect(dialog.getByRole("tab", { name: "Requirements" })).toBeFocused();
  await expect(dialog.getByRole("tab", { name: "Requirements" })).toHaveAttribute("aria-selected", "true");
  await expect(dialog.getByRole("tab", { name: "Requirements" })).toHaveAttribute("tabindex", "0");
  await expect(overview).toHaveAttribute("aria-selected", "false");
  await page.keyboard.press("End");
  await expect(dialog.getByRole("tab", { name: "Audit" })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(overview).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(dialog.getByRole("tab", { name: "Audit" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(overview).toHaveAttribute("aria-selected", "true");

  // Focus trap: many Tab presses never leave the dialog, and the page behind is inert.
  for (let i = 0; i < 25; i++) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'))).toBe(true);
  }
  for (let i = 0; i < 25; i++) {
    await page.keyboard.press("Shift+Tab");
    expect(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'))).toBe(true);
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: trigger })).toBeFocused();
});
