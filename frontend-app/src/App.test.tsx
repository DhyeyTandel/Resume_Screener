import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "./App";
import { jsonResponse, makeReport } from "./test/fixtures";
import { mockFetch, renderWithClient, type Handler } from "./test/utils";

const baseHandlers: Handler[] = [
  (u) => (u === "/v1/health" ? jsonResponse({ status: "ok", llm_provider: "mock", mock_mode: true }) : undefined),
  (u) => (u === "/v1/samples" ? jsonResponse([makeReport()]) : undefined),
  (u) => (u === "/v1/samples/jd" ? new Response("Backend Engineer\n", { status: 200 }) : undefined),
];

describe("App JD panel and Screen button", () => {
  it("disables Screen Candidates when the JD is empty and validates inline on blur", async () => {
    mockFetch(...baseHandlers);
    const user = userEvent.setup();
    renderWithClient(<App />);
    const btn = screen.getByRole("button", { name: "Screen Candidates" });
    expect(btn).toBeDisabled();
    expect(screen.getByText("Add a job description to enable this button.")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Job description"));
    await user.tab();
    expect(await screen.findByText("A job description is required before screening.")).toBeInTheDocument();
    expect(screen.getByLabelText("Job description")).toHaveAttribute("aria-invalid", "true");
    expect(btn).toBeDisabled();
  });

  it("keeps Screen disabled until both a JD and a resume exist", async () => {
    mockFetch(...baseHandlers);
    const user = userEvent.setup();
    renderWithClient(<App />);
    const btn = screen.getByRole("button", { name: "Screen Candidates" });
    await user.type(screen.getByLabelText("Job description"), "Backend role");
    expect(btn).toBeDisabled();
    expect(screen.getByText("Upload or paste at least one resume.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Or paste one resume"), "Jane Doe Python");
    expect(btn).toBeEnabled();
  });

  it("loads the sample JD into the textarea", async () => {
    mockFetch(...baseHandlers);
    const user = userEvent.setup();
    renderWithClient(<App />);
    await user.click(screen.getByRole("button", { name: "Load sample JD" }));
    await waitFor(() => expect(screen.getByLabelText("Job description")).toHaveValue("Backend Engineer"));
  });
});

describe("App polling", () => {
  it("stops polling and shows a message when the screening is interrupted", async () => {
    let polls = 0;
    const fetchMock = mockFetch(
      ...baseHandlers,
      (u, init) => (u === "/v1/screenings" && init?.method === "POST" ? jsonResponse({ screening_id: "s1" }) : undefined),
      (u) => {
        if (u !== "/v1/screenings/s1") return undefined;
        polls += 1;
        return jsonResponse({ screening_id: "s1", status: "interrupted", total: 3, done: 1, candidates: [] });
      },
    );
    const user = userEvent.setup();
    renderWithClient(<App />);
    await user.type(screen.getByLabelText("Job description"), "Backend role");
    await user.type(screen.getByLabelText("Or paste one resume"), "Jane Doe Python");
    await user.click(screen.getByRole("button", { name: "Screen Candidates" }));

    const alert = await screen.findByText(/interrupted by a server restart after 1 of 3 candidates/);
    expect(alert).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Screen Candidates" })).toBeEnabled();

    const seen = polls;
    expect(seen).toBeGreaterThan(0);
    // The poll interval is 700 ms; wait past two of them and confirm no further requests.
    await new Promise((r) => setTimeout(r, 1700));
    expect(polls).toBe(seen);
    expect(fetchMock.mock.calls.filter(([u]) => String(u) === "/v1/screenings/s1")).toHaveLength(seen);
  });

  it("keeps polling while processing, then stops on complete", async () => {
    let polls = 0;
    mockFetch(
      ...baseHandlers,
      (u, init) => (u === "/v1/screenings" && init?.method === "POST" ? jsonResponse({ screening_id: "s2" }) : undefined),
      (u) => {
        if (u !== "/v1/screenings/s2") return undefined;
        polls += 1;
        return jsonResponse({ screening_id: "s2", status: polls < 2 ? "processing" : "complete", total: 1, done: polls < 2 ? 0 : 1, candidates: [] });
      },
    );
    const user = userEvent.setup();
    renderWithClient(<App />);
    await user.type(screen.getByLabelText("Job description"), "Backend role");
    await user.type(screen.getByLabelText("Or paste one resume"), "Jane Doe Python");
    await user.click(screen.getByRole("button", { name: "Screen Candidates" }));
    expect(await screen.findByText(/Done\. 1 candidate screened/, undefined, { timeout: 4000 })).toBeInTheDocument();
    const seen = polls;
    await new Promise((r) => setTimeout(r, 1500));
    expect(polls).toBe(seen);
  });
});
