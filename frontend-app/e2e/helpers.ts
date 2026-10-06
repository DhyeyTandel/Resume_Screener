import { expect, type Page } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const pdf = (name: string) => resolve(dirname(fileURLToPath(import.meta.url)),"../../backend/tests/fixtures/pdfs", name);

/** Loads the sample JD, uploads one PDF, runs the screening and waits for it to finish. */
export async function screenPdf(page: Page, name: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "Load sample JD" }).click();
  await expect(page.getByRole("textbox", { name: "Job description" })).not.toHaveValue("");
  await page.getByLabel("Upload resumes").setInputFiles(pdf(name));
  await expect(page.getByRole("list", { name: "Selected resume files" })).toContainText(name);
  // The API rate-limits screenings (burst 5, 10 per minute). A long run can hit that, so wait
  // out the 429 and click again rather than fail.
  for (let attempt = 0; attempt < 4; attempt++) {
    await page.getByRole("button", { name: "Screen Candidates" }).click();
    const limited = page.getByText(/Too many requests/);
    const done = page.getByText(/Done\. 1 candidate screened/);
    const first = await Promise.race([
      done.waitFor({ timeout: 45_000 }).then(() => "done", () => "none"),
      limited.waitFor({ timeout: 45_000 }).then(() => "limited", () => "none"),
    ]);
    if (first === "done") break;
    await page.waitForTimeout(7_000);
  }
  await expect(page.getByText(/Done\. 1 candidate screened/)).toBeVisible({ timeout: 45_000 });
}
