import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ResultsTable from "./ResultsTable";
import { makeRow } from "../test/fixtures";

const rows = [
  makeRow({ candidate_id: "a", candidate_name: "Ada", overall_match_score: 90, recommendation: "Shortlist", strengths: ["Python"], gaps: [] }),
  makeRow({ candidate_id: "b", candidate_name: "Bob", overall_match_score: 60, recommendation: "Review Manually", strengths: ["Go"], gaps: ["Kafka"] }),
  makeRow({ candidate_id: "c", candidate_name: "Cy", overall_match_score: 30, recommendation: "Not Recommended", strengths: [], gaps: ["Docker"] }),
];

const names = () =>
  screen.getAllByRole("button", { name: /^Open details for/ }).map((b) => b.textContent);

function setup() {
  const onOpen = vi.fn();
  render(<ResultsTable rows={rows} onOpen={onOpen} emptyMessage="nothing" />);
  return { onOpen, user: userEvent.setup() };
}

describe("ResultsTable", () => {
  it("defaults to score descending", () => {
    setup();
    expect(names()).toEqual(["Ada", "Bob", "Cy"]);
  });

  it.each([
    ["Shortlist", ["Ada"]],
    ["Review", ["Bob"]],
    ["Not Recommended", ["Cy"]],
    ["All", ["Ada", "Bob", "Cy"]],
  ])("filter %s", async (label, expected) => {
    const { user } = setup();
    await user.click(screen.getByRole("button", { name: label }));
    expect(names()).toEqual(expected);
    expect(screen.getByRole("button", { name: label })).toHaveAttribute("aria-pressed", "true");
  });

  it("searches names, gaps and strengths", async () => {
    const { user } = setup();
    const box = screen.getByRole("searchbox", { name: "Search candidates" });
    await user.type(box, "kafka");
    expect(names()).toEqual(["Bob"]);
    await user.clear(box);
    await user.type(box, "python");
    expect(names()).toEqual(["Ada"]);
    await user.clear(box);
    await user.type(box, "zzz");
    expect(screen.getByText("No candidates match this search or filter.")).toBeInTheDocument();
    expect(screen.getByText("0 of 3 shown")).toBeInTheDocument();
  });

  it("combines a filter with a search", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("button", { name: "Review" }));
    await user.type(screen.getByRole("searchbox"), "ada");
    expect(screen.getByText("No candidates match this search or filter.")).toBeInTheDocument();
  });

  it("sorts by clicking column headers and toggles direction", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("button", { name: /Candidate/ }));
    expect(names()).toEqual(["Ada", "Bob", "Cy"]);
    expect(screen.getByRole("columnheader", { name: /Candidate/ })).toHaveAttribute("aria-sort", "ascending");
    await user.click(screen.getByRole("button", { name: /Candidate/ }));
    expect(names()).toEqual(["Cy", "Bob", "Ada"]);
    await user.click(screen.getByRole("button", { name: /Score/ }));
    expect(names()).toEqual(["Ada", "Bob", "Cy"]);
    await user.click(screen.getByRole("button", { name: /Score/ }));
    expect(names()).toEqual(["Cy", "Bob", "Ada"]);
    expect(screen.getByRole("columnheader", { name: /Score/ })).toHaveAttribute("aria-sort", "ascending");
  });

  it("opens a candidate on row click", async () => {
    const { user, onOpen } = setup();
    const row = screen.getByRole("button", { name: "Open details for Bob" }).closest("tr") as HTMLElement;
    await user.click(within(row).getByText("Review Manually", { exact: false }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ candidate_id: "b" }));
  });

  it("shows the empty message when there are no rows", () => {
    render(<ResultsTable rows={[]} onOpen={() => {}} emptyMessage="nothing here" />);
    expect(screen.getByText("nothing here")).toBeInTheDocument();
  });
});
