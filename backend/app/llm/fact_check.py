"""Deterministic, conservative fact-consistency check for LLM-written narrative prose.

Spec 2.8 / 9.7: the LLM only writes summary, rationale, strengths and risks from the
structured facts, and code computes the numbers. Nothing else verifies that the prose
matches the facts, so this module does, with no LLM and no network.

    check_narrative(narrative, facts, known_skills=None) -> list[dict]

Each violation is {"type", "detail", "text", "severity"}; an empty list means consistent.
Types: strength_contradicts_facts, risk_contradicts_facts, score_mismatch, count_mismatch,
unknown_skill (severity "low"), accusatory_language, hiring_decision.

Design rule: prefer missing a violation over raising a false one, because a false violation
throws away good prose. Every rule therefore needs an explicit, local cue (a skill mention in
the same clause as a positive or negative cue) before it fires.
"""
from __future__ import annotations

import re

from ..modules.integrity_guard.interpreter import GUILT_RE
from ..modules.skill_intelligence.transfer import canonical, graph

HIGH = "high"
LOW = "low"

# Graph aliases that are ordinary English words or too short to trust as a mention.
_AMBIGUOUS_TERMS = {
    "go", "ci", "cd", "rest", "testing", "unit testing", "containers", "py", "ts", "js", "es6",
}

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")
_CLAUSE_SPLIT = re.compile(
    r"[;]|,?\s+(?:but|however|although|though|while|whereas|yet|except)\b|\s+[-–]\s+", re.I
)

_POSITIVE = re.compile(
    r"\b(?:has|have|having|with|expertise|experience[ds]?|strong\w*|proficien\w+|skilled|"
    r"background|possess\w*|solid|demonstrat\w+|knowledge|overlap|evidence|excel\w*|"
    r"hands-on|extensive|including|uses?|used|using|built|worked|familiar\w*)\b",
    re.I,
)
# Any negation: blocks a positive reading.
_NEGATION = re.compile(
    r"\b(?:no|not|never|none|nothing|without|lack\w*|missing|miss(?:es|ed)?|absen\w+|gaps?|"
    r"unable|limited|insufficient|unproven|unverified|unclear|fewer|short of)\b|n't\b",
    re.I,
)
# Explicit gap claims: needed before a matched skill may be called a gap.
_GAP_CUE = re.compile(
    r"\b(?:lack\w*|missing|absen\w+|without|no|gaps?|unable)\b"
    r"|\bnot (?:found|shown|demonstrated|evidenced|listed|present|mentioned|have|has)\b"
    r"|\b(?:does|do|did)(?:n't| not) (?:have|show|list|mention|demonstrate)\b",
    re.I,
)

_DECISION_RE = re.compile(
    r"\b(?:should (?:not )?be (?:hired|rejected|considered|advanced|interviewed|shortlisted)"
    r"|(?:do|does|did|would|will) not hire|don't hire|(?:must|should|shall|can) not be considered"
    r"|reject(?:s|ed|ing|ion)?|disqualif\w+|no[- ]hire|strong[- ]hire"
    r"|(?:we|you|they) should hire|hire (?:this|the) candidate"
    r"|recommend(?:s|ed)? (?:to )?(?:hir\w*|reject\w*|advanc\w*|proceed\w*))\b",
    re.I,
)

_SCORE_RE = re.compile(
    r"\b(?:score|scor(?:es|ed|ing)|match|rating)"
    r"(?:\s+(?:of|is|was|at|to|came to|equals?|totals?|reached|about|around|roughly|approximately))*"
    r"\s*[:=]?\s*(\d+(?:\.\d+)?)(?!\s*\+)(?!\.?\d)(?!\s*(?:of|out of)\s+(?:the\s+)?\d)"
    r"(?!\s*(?:years?|requirements?|criteria|skills?))",
    re.I,
)
_OUT_OF_100_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:/\s*100|out of 100)\b", re.I)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_NUM = r"(\d+|" + "|".join(_WORD_NUM) + r")"
_REQ_WORD = r"(?:requirements?|criteria|qualifications?|skills?)"
_N_OF_M_RE = re.compile(
    rf"\b{_NUM}\s+(?:of|out of)\s+(?:the\s+|all\s+)?{_NUM}\b(?:\W+\w+){{0,3}}?\W+{_REQ_WORD}\b", re.I
)
_TOTAL_COUNT_RE = re.compile(
    rf"\b(?:total of\s+)?{_NUM}\s+(?:total|job|required|listed|key)\s+(?:(?:job|required|listed|key)\s+)*"
    rf"{_REQ_WORD}\b",
    re.I,
)


def _num(tok: str) -> int:
    t = tok.lower()
    return _WORD_NUM[t] if t in _WORD_NUM else int(t)


# --------------------------------------------------------------------------- skills

def _vocabulary(known_skills: set[str] | None) -> dict[str, str]:
    """term (lowercase) -> key. Keys are canonical graph names where one exists."""
    vocab: dict[str, str] = {}
    for term, name in graph()["_alias"].items():
        if term not in _AMBIGUOUS_TERMS and len(term) >= 2:
            vocab[term] = name
    # "go" the language is only trusted as "golang".
    for s in known_skills or ():
        s_l = s.strip().lower()
        if s_l:
            vocab[s_l] = canonical(s_l) or s_l
    return vocab


def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w+#./-]){re.escape(term)}(?![\w+#]|\.\w)", re.I)


def _mentions(text: str, vocab: dict[str, str]) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, key) mentions, longest term first."""
    taken: list[tuple[int, int, str]] = []
    for term in sorted(vocab, key=len, reverse=True):
        for m in _term_re(term).finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te, _ in taken):
                continue
            taken.append((s, e, vocab[term]))
    return sorted(taken)


def _fact_keys(reqs: list[str], vocab: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    for r in reqs:
        r_l = str(r).strip().lower()
        if not r_l:
            continue
        keys.add(r_l)
        keys.update(k for _, _, k in _mentions(r_l, vocab))
        if len(r_l.split()) <= 3:
            c = canonical(r_l)
            if c:
                keys.add(c)
    return keys


def _known_keys(known_skills: set[str] | None) -> set[str]:
    """Keys that count as 'a known skill' for the unknown_skill rule."""
    known = set(graph()["skills"])
    for s in known_skills or ():
        s_l = s.strip().lower()
        known.add(canonical(s_l) or s_l)
    return known


# --------------------------------------------------------------------------- text helpers

def _clauses(text: str) -> list[str]:
    out: list[str] = []
    for sent in _SENT_SPLIT.split(text):
        out.extend(c for c in _CLAUSE_SPLIT.split(sent) if c and c.strip())
    return out


def _v(type_: str, detail: str, text: str, severity: str = HIGH) -> dict:
    return {"type": type_, "detail": detail, "text": text.strip(), "severity": severity}


def _display(clause: str, s: int, e: int) -> str:
    return clause[s:e]


# --------------------------------------------------------------------------- the check

def check_narrative(narrative: dict, facts: dict, known_skills: set[str] | None = None) -> list[dict]:
    """Return fact-consistency violations for the prose; [] means consistent."""
    if not isinstance(narrative, dict) or not isinstance(facts, dict):
        return []
    vocab = _vocabulary(known_skills)
    matched_reqs = [str(x) for x in facts.get("matched", []) or []]
    missing_reqs = [str(x) for x in facts.get("missing", []) or []]
    partial_reqs = [str(x) for x in facts.get("partial", []) or []]
    matched_k = _fact_keys(matched_reqs, vocab)
    missing_k = _fact_keys(missing_reqs, vocab)
    partial_k = _fact_keys(partial_reqs, vocab)
    # A key present on both sides is ambiguous: never judge it.
    ambiguous = matched_k & missing_k
    known = _known_keys(known_skills)

    # Make whole requirement strings matchable too (e.g. "3+ years of professional experience").
    full_vocab = dict(vocab)
    for r in matched_reqs + missing_reqs + partial_reqs:
        r_l = r.strip().lower()
        if len(r_l.split()) > 1:
            full_vocab.setdefault(r_l, r_l)

    raw_summary, raw_rationale = narrative.get("summary"), narrative.get("rationale")
    summary: str = raw_summary if isinstance(raw_summary, str) else ""
    rationale: str = raw_rationale if isinstance(raw_rationale, str) else ""
    strengths = [s for s in narrative.get("strengths", []) or [] if isinstance(s, str)]
    risks = [s for s in narrative.get("risks", []) or [] if isinstance(s, str)]

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(v: dict) -> None:
        sig = (v["type"], v["detail"])
        if sig not in seen:
            seen.add(sig)
            out.append(v)

    # Rule 1 (strengths list): every skill named must be matched.
    for item in strengths:
        if _NEGATION.search(item):
            continue  # "No gaps in Kafka" style phrasing: do not guess
        for s, e, key in _mentions(item.lower(), full_vocab):
            if key in ambiguous:
                continue
            if key in missing_k and key not in matched_k:
                add(_v("strength_contradicts_facts",
                       f"'{item[s:e]}' is listed as a strength but is a missing requirement", item))
            elif key not in matched_k and key not in missing_k and key not in partial_k and key in known:
                add(_v("unknown_skill",
                       f"'{item[s:e]}' is listed as a strength but is not in the screening facts",
                       item, LOW))

    # Rule 2 (risks list): a gap claim about a matched skill.
    for item in risks:
        for clause in _clauses(item):
            if not _GAP_CUE.search(clause):
                continue
            for s, e, key in _mentions(clause.lower(), full_vocab):
                if key in matched_k and key not in missing_k and key not in partial_k:
                    add(_v("risk_contradicts_facts",
                           f"'{clause[s:e]}' is named as a gap or risk but is a matched requirement",
                           item))
            for s, e, key in _mentions(clause.lower(), full_vocab):
                if key not in matched_k and key not in missing_k and key not in partial_k and key in known:
                    add(_v("unknown_skill",
                           f"'{clause[s:e]}' appears in a risk but is not in the screening facts",
                           item, LOW))

    # Rules 1, 2 and 4 on the prose fields, per clause with positional cues.
    for prose in (summary, rationale):
        for clause in _clauses(prose):
            low = clause.lower()
            for s, e, key in _mentions(low, full_vocab):
                if key in ambiguous:
                    continue
                before = low[:s]
                neg_before = bool(_NEGATION.search(before))
                gap_before = bool(_GAP_CUE.search(before))
                pos_before = bool(_POSITIVE.search(before))
                name = clause[s:e]
                if key in missing_k and key not in matched_k:
                    if pos_before and not neg_before:
                        add(_v("strength_contradicts_facts",
                               f"'{name}' is described positively but is a missing requirement",
                               clause))
                elif key in matched_k and key not in missing_k:
                    if gap_before:
                        add(_v("risk_contradicts_facts",
                               f"'{name}' is described as a gap but is a matched requirement", clause))
                elif key not in partial_k and key in known:
                    add(_v("unknown_skill",
                           f"'{name}' is mentioned but is in neither matched nor missing", clause, LOW))

    # Rule 3: numbers in summary and rationale.
    base = facts.get("base_score")
    total = facts.get("total_requirements")
    n_matched, n_missing = len(matched_reqs), len(missing_reqs)
    allowed_counts: set[int] = set()
    if isinstance(total, int | float) and not isinstance(total, bool):
        total = int(total)
        allowed_counts = {total, n_matched, n_missing, total - n_missing, total - n_matched}
    for prose in (summary, rationale):
        if not prose:
            continue
        if isinstance(base, int | float) and not isinstance(base, bool):
            claimed: list[tuple[float, str]] = []
            for m in _SCORE_RE.finditer(prose):
                claimed.append((float(m.group(1)), m.group(0)))
            for m in _OUT_OF_100_RE.finditer(prose):
                claimed.append((float(m.group(1)), m.group(0)))
            for val, snippet in claimed:
                if abs(val - float(base)) > 0.5:
                    add(_v("score_mismatch",
                           f"prose states a score of {val:g} but base_score is {base:g}", snippet))
        if allowed_counts:
            for m in _N_OF_M_RE.finditer(prose):
                n, tot = _num(m.group(1)), _num(m.group(2))
                if tot != total or n not in allowed_counts:
                    add(_v("count_mismatch",
                           f"prose says {n} of {tot} requirements; facts have {n_matched} matched, "
                           f"{n_missing} missing, {total} total", m.group(0)))
            for m in _TOTAL_COUNT_RE.finditer(prose):
                if _num(m.group(1)) != total:
                    add(_v("count_mismatch",
                           f"prose says {_num(m.group(1))} requirements; total_requirements is {total}",
                           m.group(0)))

    # Rule 5: accusatory or decision language anywhere in the prose.
    for text in [summary, rationale, *strengths, *risks]:
        if not text:
            continue
        g = GUILT_RE.search(text)
        if g:
            add(_v("accusatory_language", f"accusatory wording '{g.group(0)}'", text))
        d = _DECISION_RE.search(text)
        if d:
            add(_v("hiring_decision", f"states or implies a hiring decision ('{d.group(0)}')", text))
    return out
