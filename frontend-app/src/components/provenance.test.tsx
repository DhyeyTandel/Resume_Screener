import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CandidateDrawer from "./CandidateDrawer";
import ResultsTable from "./ResultsTable";
import { ProseChip, proseLabel, servedSummary, shortReason } from "./provenance";
import { makeReport, makeRow } from "../test/fixtures";
import { renderWithClient } from "../test/utils";
import type { ProvenanceEntry } from "../types";

const entry = (over: Partial<ProvenanceEntry> = {}): ProvenanceEntry => ({
  task: "narrative", provider: "openrouter", model: "nvidia/nemotron-3-super-120b-a12b:free",
  fell_back: false, reason: null, calls: 1, ...over,
});
const mockFallback = entry({
  provider: "mock", model: "mock-heuristic-v1", fell_back: true,
  reason: "openrouter: HTTP 429 after 3 attempts",
});

describe("prose label", () => {
  it("names the real provider and a short model", () => {
    expect(proseLabel([entry()])).toMatchObject({ text: "Prose: openrouter / nemotron-3-super-120b-a12b", tone: "ok" });
  });
  it("labels a mock fallback as mock template with the reason", () => {
    expect(proseLabel([mockFallback])).toMatchObject({ text: "Prose: mock template (OpenRouter rate-limited)", tone: "warn" });
  });
  it("labels a deterministic template", () => {
    const l = proseLabel([entry({ provider: "template", model: "deterministic-template", fell_back: true, reason: "openrouter: HTTP 429" })]);
    expect(l.text).toBe("Prose: template (OpenRouter rate-limited)");
    expect(l.tone).toBe("warn");
  });
  it("never claims a model for mock without a fallback", () => {
    const l = proseLabel([entry({ provider: "mock", model: "mock-heuristic-v1" })]);
    expect(l.text).toBe("Prose: mock template (no model configured)");
  });
  it("says not recorded for old reports and checking while loading", () => {
    expect(proseLabel(null).text).toBe("Prose: not recorded");
    expect(proseLabel([]).text).toBe("Prose: not recorded");
    expect(proseLabel(undefined).text).toBe("Prose: checking...");
  });
  it("shortens reasons", () => {
    expect(shortReason("openrouter: no API key in environment")).toBe("OpenRouter has no API key");
    expect(shortReason("ollama: ollama unreachable")).toBe("Ollama unreachable");
  });
  it("summarises who served a screening", () => {
    expect(servedSummary([[mockFallback], [mockFallback], [entry()], undefined, null])).toBe(
      "mock template x2, openrouter / nemotron-3-super-120b-a12b x1",
    );
    expect(servedSummary([undefined])).toBeNull();
  });
  it("renders as a chip", () => {
    render(<ProseChip entries={[mockFallback]} />);
    expect(screen.getByText(/Prose: mock template/)).toBeInTheDocument();
  });
});

describe("provenance in the UI", () => {
  it("shows the chip in the results row", () => {
    render(<ResultsTable rows={[makeRow({ provenance: [mockFallback] })]} onOpen={() => {}} emptyMessage="x" />);
    expect(screen.getByRole("columnheader", { name: "Prose source" })).toBeInTheDocument();
    expect(screen.getByText("Prose: mock template (OpenRouter rate-limited)")).toBeInTheDocument();
  });

  it("shows the chip and the detail list in the drawer", () => {
    const report = makeReport({}, {
      meta: {
        llm_provider: "mock", mock_mode: true,
        provenance: [mockFallback, entry({ task: "claim_judge", calls: 3 }), entry({ task: "interview_questions", provider: "mock", model: "mock-heuristic-v1" })],
      },
    });
    renderWithClient(<CandidateDrawer row={makeRow({ source: "sample" })} sampleReports={[report]} onClose={() => {}} />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByText("Prose: mock template (OpenRouter rate-limited)").length).toBeGreaterThan(0);
    const rows = within(dialog).getAllByTestId("provenance-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Mock template (deterministic placeholder, not a model)");
    expect(rows[0]).toHaveTextContent("Fell back: openrouter: HTTP 429 after 3 attempts");
    expect(rows[1]).toHaveTextContent("OpenRouter / nvidia/nemotron-3-super-120b-a12b:free");
    expect(rows[1]).toHaveTextContent("3 calls");
  });

  it("admits old reports have no provenance", () => {
    renderWithClient(<CandidateDrawer row={makeRow({ source: "sample" })} sampleReports={[makeReport()]} onClose={() => {}} />);
    expect(within(screen.getByRole("dialog")).getAllByText(/predates provenance tracking/).length).toBeGreaterThan(0);
  });
});
