"""Module C: semantic matching + transferable-skill reasoning (Spec 10).

Assumption A-4: sentence-transformers is optional. When it is not installed the
engine uses a deterministic lexical+graph similarity so the demo runs offline at
zero cost. The interface is identical either way.
"""
from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path

from ...config import cfg
from ...schemas.vocab import EXACT, MODERATELY, NO_EVIDENCE, STRONGLY, WEAKLY

_GRAPH_PATH = Path(__file__).with_name("graph.json")


@lru_cache(maxsize=1)
def graph() -> dict:
    g = json.loads(_GRAPH_PATH.read_text())
    alias = {}
    for name, node in g["skills"].items():
        alias[name] = name
        for a in node.get("aliases", []):
            alias[a] = name
    g["_alias"] = alias
    g["_edges"] = {}
    for e in g["edges"]:
        g["_edges"].setdefault(e["to"], {})[e["from"]] = e
        g["_edges"].setdefault(e["from"], {}).setdefault(
            e["to"], {**e, "learning_curve": min(1.0, e["learning_curve"] + 0.1)}
        )
    return g


def canonical(skill: str) -> str | None:
    s = skill.strip().lower()
    g = graph()
    if s in g["_alias"]:
        return g["_alias"][s]
    for alias, name in g["_alias"].items():
        if re.search(rf"\b{re.escape(alias)}\b", s):
            return name
    return None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", text.lower()))


def lexical_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def semantic_level(score: float) -> str:
    lv = cfg("semantic.levels")
    if score >= lv["very_high"]:
        return "Very High"
    if score >= lv["high"]:
        return "High"
    if score >= lv["moderate"]:
        return "Moderate"
    return "Low"


def analyze_skill(required_skill: str, resume: dict) -> dict:
    """Returns the Spec 10.4 object for one required skill."""
    g = graph()
    target = canonical(required_skill)
    resume_skills = [s for s in resume.get("skills", [])]
    canon_resume = {canonical(s): s for s in resume_skills if canonical(s)}
    evidence_text = " ".join(
        [p.get("description", "") + " " + p.get("name", "") for p in resume.get("projects", [])]
        + [
            f"{r.get('title','')} {r.get('company','')} " + " ".join(r.get("relevant_points", []))
            for r in resume.get("experience", [])
        ]
    )

    # Exact match: the skill itself is on the resume.
    direct = target in canon_resume if target else False
    if not direct and re.search(rf"\b{re.escape(required_skill.lower())}\b", evidence_text.lower()):
        direct = True

    supporting: list[str] = []
    factors = {
        "skill_similarity": 0.0,
        "concept_overlap": 0.0,
        "experience_years": 0.0,
        "project_evidence": 0.0,
        "tech_proximity": 0.0,
        "learning_curve": 0.0,
    }
    missing_concepts: list[str] = []
    supporting_projects: list[str] = []

    if target:
        target_concepts = set(g["skills"].get(target, {}).get("concepts", []))
        covered: set[str] = set()
        edges: list[dict] = []
        for rc, original in sorted(canon_resume.items()):
            edge = g["_edges"].get(target, {}).get(rc)
            if not edge:
                continue
            supporting.append(original)
            edges.append(edge)
            covered |= set(g["skills"].get(rc, {}).get("concepts", []))
        missing_concepts = sorted(target_concepts - covered)
        if edges:
            # Several adjacent skills accumulate evidence rather than one winning:
            # noisy-or over the graph edges, so Python+Flask+Django+REST -> FastAPI
            # reads as stronger transfer than any single one of them.
            product = 1.0
            for e in edges:
                product *= 1 - e["concept_overlap"]
            factors["skill_similarity"] = round(1 - product, 3)
            factors["tech_proximity"] = max(e["tech_proximity"] for e in edges)
            factors["learning_curve"] = 1.0 - min(e["learning_curve"] for e in edges)
            factors["concept_overlap"] = (
                len(target_concepts & covered) / len(target_concepts) if target_concepts else 0.0
            )
        for p in resume.get("projects", []):
            blob = f"{p.get('name','')} {p.get('description','')}".lower()
            if any(s.lower() in blob for s in supporting):
                supporting_projects.append(p.get("name", "project"))
        factors["project_evidence"] = 1.0 if supporting_projects else 0.0
        years = float(resume.get("total_years", 0) or 0)
        factors["experience_years"] = min(1.0, years / 5.0)

    weights = cfg("semantic.transfer_weights")
    transferability = round(sum(weights[k] * v for k, v in factors.items()), 3)

    tc = cfg("semantic.transfer_classes")
    if direct:
        classification = EXACT
        transferability = 1.0
    elif transferability >= tc["strongly"]:
        classification = STRONGLY
    elif transferability >= tc["moderately"]:
        classification = MODERATELY
    elif transferability >= tc["weakly"]:
        classification = WEAKLY
    else:
        classification = NO_EVIDENCE

    semantic_match = 1.0 if direct else round(
        max([lexical_similarity(required_skill, s) for s in resume_skills] + [transferability]), 3
    )

    # Traceability (Spec 10.3): a cited supporting skill must exist in the resume
    # AND have a graph edge to the target. Anything else is dropped.
    supporting = [s for s in supporting if canonical(s) in g["_edges"].get(target or "", {})]

    if classification == EXACT:
        suggestion = f"Direct evidence of {required_skill} was found in the resume."
    elif classification == NO_EVIDENCE:
        suggestion = (
            f"Exact Match: No - no adjacent experience found for {required_skill} either."
        )
    else:
        suggestion = (
            f"Exact Match: No - Transferability: {classification.split()[0]} - "
            f"Supporting: {', '.join(supporting) or 'none cited'} - "
            f"Remaining gap: {', '.join(missing_concepts[:4]) or required_skill + ' experience'}."
        )

    return {
        "required_skill": required_skill,
        "semantic_match": semantic_match,
        "semantic_level": semantic_level(semantic_match),
        "transferability_score": transferability,
        "classification": classification,
        "supporting_skills": sorted(set(supporting)),
        "supporting_projects": sorted(set(supporting_projects)),
        "missing_concepts": missing_concepts,
        "recruiter_suggestion": suggestion,
        "reasoning": _reasoning(classification, required_skill, supporting, missing_concepts),
        "confidence": "High" if direct or supporting else "Medium" if target else "Low",
        "factors": {k: round(v, 3) for k, v in factors.items()},
        "evidence": [],
    }


def _reasoning(classification: str, skill: str, supporting: list[str], gaps: list[str]) -> str:
    if classification == EXACT:
        return f"{skill} appears directly in the resume's own wording."
    if classification == NO_EVIDENCE:
        return (
            f"Neither {skill} nor any skill the graph relates to it appears in the resume."
        )
    return (
        f"{', '.join(supporting) or 'Adjacent experience'} shares core concepts with {skill}; "
        f"the remaining gap is {', '.join(gaps[:3]) or 'framework-specific practice'}."
    )
