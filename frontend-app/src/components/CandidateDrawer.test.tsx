import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import CandidateDrawer from "./CandidateDrawer";
import { QuestionsTab } from "./tabs";
import { jsonResponse, makeReport, makeRow } from "../test/fixtures";
import { mockFetch, renderWithClient } from "../test/utils";

const sampleRow = makeRow({ source: "sample" });
const report = makeReport();

function setup(row = sampleRow, onClose = vi.fn()) {
  renderWithClient(<CandidateDrawer row={row} sampleReports={[report]} onClose={onClose} />);
  return { onClose, user: userEvent.setup() };
}

describe("CandidateDrawer", () => {
  it("closes on Escape", async () => {
    const { user, onClose } = setup();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("has keyboard reachable tabs with roving tabindex and arrow navigation", async () => {
    const { user } = setup();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(7);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[0]).toHaveAttribute("tabindex", "0");
    expect(tabs[1]).toHaveAttribute("tabindex", "-1");

    tabs[0].focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Requirements" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Requirements" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "tab-requirements");

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Audit" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Audit" })).toHaveFocus();
  });

  it("renders Missing and Not Enough Evidence distinctly", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("tab", { name: "Requirements" }));
    const panel = screen.getByRole("tabpanel");
    const kafka = screen.getByText("Kafka", { selector: "strong" }).closest("li") as HTMLElement;
    const tf = screen.getByText("Terraform", { selector: "strong" }).closest("li") as HTMLElement;

    // Chips in the list items (the legend also uses the same two labels).
    expect(within(kafka).getByText("Missing")).toHaveClass("chip-stop");
    expect(within(tf).getByText("? Not Enough Evidence")).toHaveClass("chip-warn");
    expect(within(kafka).getByText(/Nothing in the resume supports/)).toBeInTheDocument();
    expect(within(tf).getByText(/not the same as missing/)).toBeInTheDocument();
    expect(kafka.className).not.toEqual(tf.className);
    expect(tf.className).toContain("border-dashed");
    expect(kafka.className).not.toContain("border-dashed");
    expect(within(panel).queryByText("? Not Enough Evidence", { selector: "li *" })).not.toBeNull();
  });

  it("disables the audit form for sample candidates", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("tab", { name: "Audit" }));
    expect(screen.getByLabelText("Recruiter decision")).toBeDisabled();
    expect(screen.getByLabelText("Note (optional)")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Record decision" })).toBeDisabled();
    expect(screen.getByText(/decisions cannot be recorded/)).toBeInTheDocument();
  });

  it("enables the audit form for real candidates and records a decision", async () => {
    const fetchMock = mockFetch(
      (url) => (url === "/v1/candidates/c1" ? jsonResponse(report) : undefined),
      (url, init) => (url === "/v1/candidates/c1/decision" && init?.method === "POST" ? jsonResponse({ decision: "Hold", at: "2026-01-01T00:00:00Z" }) : undefined),
      (url) => (url === "/v1/candidates/c1/audit" ? jsonResponse([]) : undefined),
    );
    const user = userEvent.setup();
    renderWithClient(<CandidateDrawer row={makeRow()} sampleReports={[]} onClose={() => {}} />);
    await user.click(await screen.findByRole("tab", { name: "Audit" }));
    const select = await screen.findByLabelText("Recruiter decision");
    expect(select).toBeEnabled();
    await user.selectOptions(select, "Hold");
    await user.click(screen.getByRole("button", { name: "Record decision" }));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeTruthy();
    expect((post![1]!.body as FormData).get("decision")).toBe("Hold");
  });
});

describe("Delete candidate data", () => {
  const handlers = () => [
    (url: string) => (url === "/v1/candidates/c1" ? jsonResponse(report) : undefined),
    (url: string) => (url === "/v1/candidates/c1/audit" ? jsonResponse([]) : undefined),
  ];

  it("is not offered for sample candidates", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("tab", { name: "Audit" }));
    expect(screen.queryByRole("button", { name: "Delete candidate data" })).toBeNull();
  });

  it("asks first, names the candidate, says it cannot be undone, and Cancel deletes nothing", async () => {
    const fetchMock = mockFetch(...handlers());
    const user = userEvent.setup();
    renderWithClient(<CandidateDrawer row={makeRow()} sampleReports={[]} onClose={() => {}} />);
    await user.click(await screen.findByRole("tab", { name: "Audit" }));
    await user.click(await screen.findByRole("button", { name: "Delete candidate data" }));
    const group = screen.getByRole("group", { name: /Ada Lovelace/ });
    expect(group).toHaveTextContent("cannot be undone");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("group", { name: /Ada Lovelace/ })).toBeNull();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("deletes on confirm, then refreshes the table and closes the drawer", async () => {
    const fetchMock = mockFetch(
      (url, init) => (url === "/v1/candidates/c1" && init?.method === "DELETE" ? jsonResponse({ erased: { candidates: 1 } }) : undefined),
      ...handlers(),
    );
    const onClose = vi.fn();
    const onErased = vi.fn();
    const user = userEvent.setup();
    renderWithClient(<CandidateDrawer row={makeRow()} sampleReports={[]} onClose={onClose} onErased={onErased} />);
    await user.click(await screen.findByRole("tab", { name: "Audit" }));
    await user.click(await screen.findByRole("button", { name: "Delete candidate data" }));
    await user.click(screen.getByRole("button", { name: /Yes, permanently delete Ada Lovelace/ }));
    await vi.waitFor(() => expect(onErased).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  });

  it("keeps the drawer open and shows the error when the server refuses", async () => {
    mockFetch(
      (url, init) => (url === "/v1/candidates/c1" && init?.method === "DELETE" ? jsonResponse({ detail: { code: "NOT_FOUND", message: "No candidate c1." } }, 404) : undefined),
      ...handlers(),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithClient(<CandidateDrawer row={makeRow()} sampleReports={[]} onClose={onClose} onErased={() => {}} />);
    await user.click(await screen.findByRole("tab", { name: "Audit" }));
    await user.click(await screen.findByRole("button", { name: "Delete candidate data" }));
    await user.click(screen.getByRole("button", { name: /Yes, permanently delete/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No candidate c1.");
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("QuestionsTab", () => {
  it("orders must-have questions before preferred and other topics", () => {
    const r = makeReport({}, {
      interview_questions_detailed: {
        interview_questions: [
          { skill: "Docker", evidence_level: "none", question: "Q-other", purpose: "p", risk_if_unanswered: "r" },
          { skill: "Terraform", evidence_level: "weak", question: "Q-preferred", purpose: "p", risk_if_unanswered: "r" },
          { skill: "Kafka", evidence_level: "none", question: "Q-must", purpose: "p", risk_if_unanswered: "r" },
        ],
      },
    });
    renderWithClient(<QuestionsTab r={r} />);
    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(["Must-have requirements", "Preferred requirements", "Other topics"]);
    const order = screen.getAllByText(/^Q-/).map((p) => p.textContent);
    expect(order).toEqual(["Q-must", "Q-preferred", "Q-other"]);
  });
});
