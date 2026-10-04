import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CandidateProgress, StageStatus } from "../types";
import StageTimeline, { progressAnnouncement } from "./StageTimeline";

const NAMES = ["Parse", "Integrity", "Match", "Skills", "Authenticity", "Questions"];

function cand(statuses: StageStatus[], over: Partial<CandidateProgress> = {}): CandidateProgress {
  return {
    index: 0,
    candidate_name: "Ada Lovelace",
    status: "running",
    current_stage: null,
    stages: NAMES.map((name, i) => ({ name, status: statuses[i] ?? "pending" })),
    ...over,
  };
}

const item = (stage: string) => document.querySelector(`[data-stage="${stage}"]`) as HTMLElement;

describe("StageTimeline", () => {
  it("renders nothing without progress, so no progress is faked", () => {
    const { container } = render(<StageTimeline progress={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the six stages in order with a text label for each state", () => {
    render(<StageTimeline progress={[cand(["done", "done", "running"], { current_stage: "Match" })]} />);
    const list = screen.getByRole("list", { name: "Stages for Ada Lovelace" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((i) => i.getAttribute("data-stage"))).toEqual(NAMES);
    expect(within(item("Parse")).getByText("Done")).toBeInTheDocument();
    expect(within(item("Match")).getByText("Running")).toBeInTheDocument();
    expect(within(item("Skills")).getByText("Pending")).toBeInTheDocument();
    expect(item("Match")).toHaveAttribute("aria-current", "step");
    expect(item("Parse")).not.toHaveAttribute("aria-current");
  });

  it("shows error and skipped as words, not just colour", () => {
    render(<StageTimeline progress={[cand(["done", "done", "error", "skipped", "skipped", "skipped"], { status: "error" })]} />);
    expect(within(item("Match")).getByText("Error")).toBeInTheDocument();
    expect(within(item("Skills")).getByText("Skipped")).toBeInTheDocument();
    expect(screen.getByText("Could not be screened")).toBeInTheDocument();
    expect(item("Match")).toHaveAttribute("data-status", "error");
  });

  it("shows each candidate with its own stages", () => {
    render(
      <StageTimeline
        progress={[
          cand(["done", "done", "done", "done", "done", "done"], { status: "done" }),
          cand([], { index: 1, candidate_name: "Grace Hopper", status: "pending" }),
        ]}
      />,
    );
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText("Waiting")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Stages for Grace Hopper" })).toBeInTheDocument();
  });

  it("announces only what is really running, in a polite live region", () => {
    const p = [cand(["done", "running"], { current_stage: "Integrity" })];
    render(<StageTimeline progress={p} />);
    const live = screen.getByRole("status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("Ada Lovelace: Integrity running");
    expect(progressAnnouncement([cand([], { status: "done" })])).toBe("");
    expect(progressAnnouncement([cand([], { status: "pending" })])).toBe("");
  });
});
