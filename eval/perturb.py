"""Ground-truth-labeled perturbations of the sample resumes (Spec 16.1).

Each function takes a base resume's text plus its JD-derived skill list and
returns (perturbed_text, expected). `expected` says what a correct system
should conclude, so run_eval.py can score pass/fail without a human label.

P1 skill injection, P2 metric inflation, P3 AI rewrite (facts unchanged - must
NOT be flagged), P4 date/title contradiction, P5 forked/tutorial repo claimed
as own (GitHub-side, not text - handled via a fixture in run_eval.py), P6
no-GitHub/private-work profile.
"""
from __future__ import annotations
import random
import re

random.seed(7)  # determinism (Spec 2.10)

UNSUPPORTED_SKILLS = ["Kubernetes", "GraphQL", "Terraform", "RabbitMQ", "gRPC"]


def p1_skill_injection(text: str, n: int = 4) -> tuple[str, dict]:
    """Adds n unsupported skills to the Skills line. Expect: each Missing or
    Not Enough Evidence, never Matched (no code/project evidence exists)."""
    added = UNSUPPORTED_SKILLS[:n]
    out = re.sub(
        r"(Skills\n[^\n]*)", lambda m: m.group(1) + ", " + ", ".join(added), text, count=1
    )
    return out, {"type": "P1", "injected_skills": added,
                 "expect": "none of the injected skills should read as Matched"}


def p2_metric_inflation(text: str) -> tuple[str, dict]:
    """Multiplies the largest percentage claim by 3x with no new evidence.
    Expect: authenticity confidence/reliability should not rise from this
    change alone (no artifact backs the new number either)."""
    m = list(re.finditer(r"(\d+)(\s*percent)", text))
    if not m:
        return text, {"type": "P2", "note": "no percentage claim found", "expect": "n/a"}
    biggest = max(m, key=lambda x: int(x.group(1)))
    inflated = min(99, int(biggest.group(1)) * 3)
    out = text[: biggest.start(1)] + str(inflated) + text[biggest.end(1) :]
    return out, {"type": "P2", "original": int(biggest.group(1)), "inflated": inflated,
                 "expect": "authenticity score should not increase from this edit alone"}


AI_REWRITE_MAP = {
    "Built": "Engineered", "Ran": "Operated", "Wrote": "Authored",
    "Tuned": "Optimized", "Owned": "Led ownership of", "Designed": "Architected",
}


def p3_ai_rewrite_same_facts(text: str) -> tuple[str, dict]:
    """Paraphrases verbs/connectives without changing any fact, number, or
    technology. Expect: score and recommendation UNCHANGED (Spec 2.7 / P3)."""
    out = text
    for k, v in AI_REWRITE_MAP.items():
        out = re.sub(rf"\b{k}\b", v, out)
    out = out.replace(". ", ", which delivered measurable impact. ")
    return out, {"type": "P3", "expect": "score and recommendation must be unchanged"}


def p4_date_contradiction(text: str) -> tuple[str, dict]:
    """Shifts a role's end year forward so two roles overlap full-time.
    Expect: a consistency finding (this build's Stage 4 covers anachronism
    only - see ASSUMPTIONS.md A-6b - so this perturbation is recorded as a
    known gap, not asserted against)."""
    years = re.findall(r"(\d{4})\s*-\s*(\d{4}|Present)", text)
    if len(years) < 2:
        return text, {"type": "P4", "note": "fewer than 2 dated roles", "expect": "n/a"}
    out = re.sub(r"(\d{4})\s*-\s*(\d{4})", lambda m: f"{m.group(1)} - {int(m.group(1))+5}", text, count=1)
    return out, {
        "type": "P4", "expect": "a consistency check should flag the overlap",
        "known_gap": "Stage 4 in this build only checks technology anachronism, "
                     "not role-overlap - see ASSUMPTIONS.md A-6b",
    }


def p6_no_github(resume_kwargs: dict) -> tuple[dict, dict]:
    """Not a text perturbation: strips github_username/portfolio_url/consent.
    Expect: authenticity band INSUFFICIENT_EVIDENCE, not NEEDS_VERIFICATION;
    overall_match_score and recommendation unaffected."""
    out = {k: v for k, v in resume_kwargs.items() if k not in ("github_username", "portfolio_url")}
    return out, {"type": "P6", "expect": "band=INSUFFICIENT_EVIDENCE, score/recommendation unchanged"}


ALL_TEXT_PERTURBATIONS = {
    "P1_skill_injection": p1_skill_injection,
    "P2_metric_inflation": p2_metric_inflation,
    "P3_ai_rewrite": p3_ai_rewrite_same_facts,
    "P4_date_contradiction": p4_date_contradiction,
}
