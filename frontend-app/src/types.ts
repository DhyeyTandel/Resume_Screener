export type Recommendation = "Shortlist" | "Review Manually" | "Not Recommended" | string;

export interface Transferability {
  required_skill: string;
  semantic_match: number;
  semantic_level: string;
  transferability_score?: number;
  classification: string;
  supporting_skills: string[];
  supporting_projects: string[];
  missing_concepts: string[];
  recruiter_suggestion: string;
  reasoning: string;
  confidence?: string;
}

export interface RequirementMatch {
  requirement: string;
  priority: string;
  status: string;
  resume_evidence: string;
  notes: string;
}

export interface Contribution {
  requirement: string;
  weight: number;
  value: number;
  contribution: number;
  excluded: boolean;
}

export interface ScoreBreakdown {
  base_score?: number;
  integrity_penalty?: number;
  score_confidence?: number;
  per_requirement_contribution?: Contribution[];
}

export interface Evidence {
  source: string;
  citation?: string;
  note?: string;
}

export interface Claim {
  claim_id: string;
  type: string;
  text: string;
  status: string;
  epistemic_tag?: string;
  evidence: Evidence[];
  rationale?: string;
  judge_confidence?: number;
}

export interface Authenticity {
  band: string;
  scores: Record<string, number>;
  sources_used: Record<string, string>;
  claims: Claim[];
  contradictions: { type: string; detail: string; sources?: string[] }[];
  authenticity_flags: { flag: string; repo?: string; detail: string }[];
  verification_gaps: { claim_id?: string; what_to_verify: string; priority: string }[];
  recruiter_summary: string;
}

export interface IntegrityFinding {
  code: string;
  plain_explanation: string;
  why_it_matters: string;
  benign_alternative: string;
}

export interface Integrity {
  headline?: string;
  intent?: string;
  recommended_action?: string;
  findings?: IntegrityFinding[];
  score_adjustment?: {
    base_score?: number;
    penalty?: number;
    adjusted_score?: number;
    explanation?: string;
  };
  audit_log_entry?: string;
  human_decides?: string;
}

export interface InterviewQuestion {
  skill: string;
  evidence_level: string;
  question: string;
  purpose: string;
  risk_if_unanswered: string;
}

export interface Extensions {
  candidate_id: string;
  status?: string;
  error?: string;
  score_breakdown?: ScoreBreakdown;
  recommendation_reasons?: string[];
  skill_intelligence?: Transferability[];
  authenticity?: Authenticity;
  integrity?: Integrity;
  interview_questions_detailed?: {
    interview_questions?: InterviewQuestion[];
    skipped_strong_evidence?: string[];
  };
  meta?: { llm_provider?: string; mock_mode?: boolean };
}

export interface Report {
  candidate_name: string;
  overall_match_score: number;
  recommendation: Recommendation;
  summary: string;
  ai_text_indicators?: { level?: string; disclaimer?: string };
  requirement_match: RequirementMatch[];
  strengths?: string[];
  risks?: string[];
  missing_or_unclear_skills?: string[];
  fairness_notice?: string;
  extensions: Extensions;
}

/** One row of the results table (shape of the backend row_json). */
export interface Row {
  candidate_id: string;
  candidate_name: string;
  overall_match_score: number;
  base_score?: number | null;
  score_confidence?: number | null;
  recommendation: Recommendation;
  strengths: string[];
  gaps: string[];
  status: string;
  integrity_verdict: string;
  integrity_action: string;
  authenticity_band?: string | null;
  mock_mode?: boolean;
  /** "sample" rows come from /v1/samples and are not in the database. */
  source: "sample" | "screening";
}

export interface Screening {
  screening_id: string;
  status: string;
  total: number;
  done: number;
  candidates: Omit<Row, "source">[];
}

export interface Health {
  status: string;
  llm_provider: string;
  mock_mode: boolean;
}

export interface AuditEntry {
  decision: string;
  note?: string;
  at: string;
  config_version?: string;
}

export interface ApiError {
  code?: string;
  message?: string;
  remediation?: string;
}
