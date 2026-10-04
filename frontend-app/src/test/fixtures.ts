import type { Report, Row } from "../types";

export function makeRow(over: Partial<Row> = {}): Row {
  return {
    candidate_id: "c1",
    candidate_name: "Ada Lovelace",
    overall_match_score: 80,
    base_score: 80,
    score_confidence: 1,
    recommendation: "Shortlist",
    strengths: ["Evidence found for Python"],
    gaps: ["Kafka"],
    status: "Complete",
    integrity_verdict: "benign",
    integrity_action: "proceed",
    authenticity_band: "MODERATE",
    mock_mode: true,
    source: "screening",
    ...over,
  };
}

export function makeReport(over: Partial<Report> = {}, ext: Partial<Report["extensions"]> = {}): Report {
  return {
    candidate_name: "Ada Lovelace",
    overall_match_score: 80,
    recommendation: "Shortlist",
    summary: "Summary text.",
    requirement_match: [
      { requirement: "Python", priority: "Must Have", status: "Matched", resume_evidence: "Wrote Python", notes: "" },
      { requirement: "Kafka", priority: "Must Have", status: "Missing", resume_evidence: "", notes: "" },
      { requirement: "Terraform", priority: "Preferred", status: "Not Enough Evidence", resume_evidence: "", notes: "" },
    ],
    strengths: ["Python"],
    risks: [],
    missing_or_unclear_skills: ["Kafka"],
    extensions: { candidate_id: "c1", meta: { llm_provider: "mock", mock_mode: true }, ...ext },
    ...over,
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
