"""One Pydantic model for the whole unified report (Spec 13, Stage 7, 3.3, 8.2, 10.4, 12).

Policy:
* The base contract (top-level keys) forbids extra keys, so a typo or a stray key is caught.
* Module blocks (integrity, authenticity, skill intelligence, interview questions) allow extra
  keys, because modules legitimately add fields (e.g. claim `weight`, `inflation_sub_signals`).
* Every optional module is Optional, so error and Not Enough Evidence reports validate.
  Older code used `{}` as an "absent" sentinel for some blocks; those are read as None.
* Enums that the spec fixes are Literal types. Open vocabularies (flag names, contradiction
  types, evidence sources) stay `str` because the collectors may add values.
"""
from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

VERIFICATION_GAPS_CONTRACT: Final = "verification_gaps.v1"

Recommendation = Literal["Shortlist", "Review Manually", "Not Recommended"]
AiLevel = Literal["Low", "Medium", "High"]
Priority = Literal["Must Have", "Preferred"]
RequirementStatus = Literal["Matched", "Partially Matched", "Missing", "Not Enough Evidence"]
Classification = Literal[
    "Exact Match", "Strongly Transferable", "Moderately Transferable",
    "Weakly Transferable", "No Evidence",
]
SemanticLevel = Literal["Very High", "High", "Moderate", "Low"]
Confidence = Literal["High", "Medium", "Low"]
AuthenticityBand = Literal["HIGH_TRUST", "MODERATE", "NEEDS_VERIFICATION", "INSUFFICIENT_EVIDENCE"]
ClaimStatus = Literal[
    "VERIFIED", "CORROBORATED", "WEAK", "UNSUPPORTED", "CONTRADICTED", "UNVERIFIABLE"
]
EpistemicTag = Literal["FACT", "EVIDENCE", "INFERENCE", "UNKNOWN"]
ClaimType = Literal[
    "SKILL", "PROJECT", "ROLE", "METRIC", "ACHIEVEMENT", "EDUCATION", "CERTIFICATION"
]
SourceStatus = Literal["ok", "missing", "no_consent", "error"]
GapPriority = Literal["high", "med", "low"]
IntegrityIntent = Literal["benign", "questionable", "deliberate"]
IntegrityAction = Literal["proceed", "flag_for_review", "disqualify_review"]
DEvidenceLevel = Literal["claimed", "not_demonstrated", "transferable"]
StageStatus = Literal["ok", "error"]
ExtensionStatus = Literal["Not Enough Evidence", "Error"]


def _empty_dict_is_none(v: Any) -> Any:
    """Absent-module sentinel: old builders emit `{}` where a module did not run."""
    return None if isinstance(v, dict) and not v else v


EmptyAsNone = BeforeValidator(_empty_dict_is_none)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- Module C: skill intelligence (Spec 10.4) -----------------------------------------------
class TransferFactors(_Open):
    skill_similarity: float
    concept_overlap: float
    experience_years: float
    project_evidence: float
    tech_proximity: float
    learning_curve: float


class SkillIntelligenceItem(_Open):
    required_skill: str
    semantic_match: float
    semantic_level: SemanticLevel
    transferability_score: float
    classification: Classification
    supporting_skills: list[str] = Field(default_factory=list)
    supporting_projects: list[str] = Field(default_factory=list)
    missing_concepts: list[str] = Field(default_factory=list)
    recruiter_suggestion: str
    reasoning: str
    confidence: Confidence
    factors: TransferFactors | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


# --- Base contract pieces -------------------------------------------------------------------
class AiTextIndicators(_Base):
    level: AiLevel
    disclaimer: str
    evidence: list[str] = Field(default_factory=list)


class RequirementMatch(_Base):
    requirement: str
    priority: Priority
    status: RequirementStatus
    resume_evidence: str = ""
    notes: str = ""
    transferability: Annotated[SkillIntelligenceItem | None, EmptyAsNone] = None


# --- Extensions -----------------------------------------------------------------------------
class ExperienceItem(_Open):
    title: str = ""
    company: str = ""
    start: str = ""
    end: str = ""
    relevant_points: list[str] = Field(default_factory=list)


class ProjectItem(_Open):
    name: str = ""
    description: str = ""


class CandidateProfile(_Open):
    contact: dict[str, Any] | None = None
    summary: str = ""
    experience: list[ExperienceItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    total_years: float | None = None


class RequirementContribution(_Open):
    requirement: str
    weight: float
    value: float | None = None  # None means the requirement is excluded (Spec 3.5)
    contribution: float | None = None  # absent for excluded requirements
    excluded: bool


class ScoreBreakdown(_Open):
    base_score: float
    integrity_penalty: float
    score_confidence: float
    per_requirement_contribution: list[RequirementContribution] = Field(default_factory=list)


class IntegrityFinding(_Open):
    code: str
    plain_explanation: str
    why_it_matters: str
    benign_alternative: str
    quoted_evidence: str | None = None  # code-added, HIDDEN_TEXT / INJECTION_HIDDEN only


class ScoreAdjustment(_Open):
    base_score: float
    penalty: float
    adjusted_score: float
    explanation: str


class Integrity(_Open):
    """Appendix A output plus the code-added fields. `penalty` and `human_decides` are only
    added by the guardrail step, so a clean (no-flag) report does not carry them."""

    headline: str
    intent: IntegrityIntent
    intent_reasoning: str
    manipulation_attempted: bool
    findings: list[IntegrityFinding] = Field(default_factory=list)
    score_adjustment: ScoreAdjustment | None = None
    recommended_action: IntegrityAction
    audit_log_entry: str | None = None
    penalty: float | None = None
    human_decides: str | None = None


class EvidenceRef(_Open):
    source: str
    citation: str
    note: str = ""


class SkillEvidence(_Open):
    skill: str
    required: bool
    status: ClaimStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Claim(_Open):
    claim_id: str
    type: ClaimType
    text: str
    status: ClaimStatus
    epistemic_tag: EpistemicTag
    evidence: list[EvidenceRef] = Field(default_factory=list)
    rationale: str = ""
    judge_confidence: float
    weight: float | None = None


class Contradiction(_Open):
    type: str
    detail: str
    sources: list[str] = Field(default_factory=list)


class AuthenticityFlag(_Open):
    flag: str
    repo: str = ""
    detail: str = ""


class VerificationGapV1(_Base):
    """Item of the versioned `verification_gaps.v1` contract (Spec 3.3). Do not change the
    fields without a version bump; extra keys are forbidden so drift is caught."""

    claim_id: str
    what_to_verify: str
    priority: GapPriority


class AuthenticityScores(_Open):
    authenticity: float
    reliability: float
    consistency: float
    coverage: float
    inflation_index: float
    assessment_confidence: float


class SourcesUsed(_Open):
    github: SourceStatus
    linkedin: SourceStatus
    portfolio: SourceStatus


class AuthenticityMeta(_Open):
    model: str
    config_version: str
    latency_ms: int
    llm_calls: int


class Authenticity(_Open):
    candidate_id: str
    band: AuthenticityBand
    scores: AuthenticityScores
    inflation_sub_signals: dict[str, float] = Field(default_factory=dict)
    sources_used: SourcesUsed
    skill_evidence: list[SkillEvidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    authenticity_flags: list[AuthenticityFlag] = Field(default_factory=list)
    verification_gaps: list[VerificationGapV1] = Field(default_factory=list)
    # Version tag for the verification_gaps item shape. A sibling key, so the list itself
    # stays a plain list and existing consumers are unaffected.
    verification_gaps_contract: Literal["verification_gaps.v1"] = VERIFICATION_GAPS_CONTRACT
    recruiter_summary: str = ""
    meta: AuthenticityMeta


class InterviewQuestion(_Open):
    skill: str
    evidence_level: DEvidenceLevel
    question: str
    purpose: str
    risk_if_unanswered: str


class InterviewQuestionsDetailed(_Open):
    """Module D output (Spec 12) plus its bookkeeping and failure fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    interview_questions: list[InterviewQuestion] = Field(default_factory=list)
    skipped_strong_evidence: list[str] = Field(default_factory=list)
    latency_seconds: float | None = Field(default=None, alias="_latency_seconds")
    attempts: int | None = Field(default=None, alias="_attempts")
    model: str | None = Field(default=None, alias="_model")
    usage: dict[str, Any] | None = Field(default=None, alias="_usage")
    error: str | None = None
    raw_output: Any = None


class StageInfo(_Open):
    status: StageStatus | None = None
    latency_ms: int | None = None
    error: str | None = None
    note: str | None = None


class ProvenanceEntry(_Open):
    """Which provider and model actually served one LLM-using task (additive, optional)."""

    task: str
    provider: str
    model: str | None = None
    fell_back: bool = False
    reason: str | None = None
    calls: int = 0


class Meta(_Open):
    stages: dict[str, StageInfo] = Field(default_factory=dict)
    llm_provider: str
    mock_mode: bool
    interview_model: str | None = None
    config_version: str
    latency_ms: int
    llm_calls: int | None = None
    provenance: list[ProvenanceEntry] | None = None
    schema_valid: bool | None = None
    schema_errors: list[str] | None = None


class Extensions(_Open):
    candidate_id: str | None = None
    status: ExtensionStatus | None = None
    error: str | None = None
    remediation: str | None = None
    candidate_profile: Annotated[CandidateProfile | None, EmptyAsNone] = None
    score_breakdown: Annotated[ScoreBreakdown | None, EmptyAsNone] = None
    integrity: Annotated[Integrity | None, EmptyAsNone] = None
    authenticity: Annotated[Authenticity | None, EmptyAsNone] = None
    skill_intelligence: list[SkillIntelligenceItem] = Field(default_factory=list)
    interview_questions_detailed: Annotated[InterviewQuestionsDetailed | None, EmptyAsNone] = None
    recommendation_reasons: list[str] = Field(default_factory=list)
    human_review_required: Literal[True]
    meta: Meta


class UnifiedReport(_Base):
    """Spec 13. Base contract keys are exact (extra keys forbidden); extensions is open."""

    candidate_name: str
    overall_match_score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    summary: str = Field(min_length=1)  # an empty explanation is a defect, not a valid report
    ai_text_indicators: AiTextIndicators
    requirement_match: list[RequirementMatch] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    missing_or_unclear_skills: list[str] = Field(default_factory=list)
    technical_gaps: list[str] = Field(default_factory=list)
    education_assessment: str = ""
    experience_assessment: str = ""
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    interview_questions: list[str] = Field(default_factory=list)
    fairness_notice: str
    extensions: Extensions
