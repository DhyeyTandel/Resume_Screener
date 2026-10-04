import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BandChip, Chip, IntegrityChip } from "./ui";

describe("Chip", () => {
  // Guards a real past bug: a runtime-built `chip-${tone}` class was dropped by Tailwind.
  it.each(["ok", "warn", "stop", "mute", "info"] as const)("renders the literal chip-%s class", (tone) => {
    render(<Chip tone={tone}>label</Chip>);
    const el = screen.getByText("label");
    expect(el).toHaveClass("chip");
    expect(el).toHaveClass(`chip-${tone}`);
  });
});

describe("BandChip", () => {
  it("shows Insufficient evidence and no number for INSUFFICIENT_EVIDENCE", () => {
    const { container } = render(<BandChip band="INSUFFICIENT_EVIDENCE" />);
    expect(screen.getByText("Insufficient evidence")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d/);
    expect(screen.getByText("Insufficient evidence")).toHaveClass("chip-mute");
  });

  it("maps trust bands to tones", () => {
    render(<><BandChip band="HIGH_TRUST" /><BandChip band="NEEDS_VERIFICATION" /><BandChip band={null} /></>);
    expect(screen.getByText("High trust")).toHaveClass("chip-ok");
    expect(screen.getByText("Needs verification")).toHaveClass("chip-stop");
    expect(screen.getByText("Not assessed")).toHaveClass("chip-mute");
  });
});

describe("IntegrityChip", () => {
  it("flags non-proceed actions", () => {
    render(<IntegrityChip action="disqualify_review" />);
    expect(screen.getByText("Flagged: disqualify review")).toHaveClass("chip-stop");
  });
});
