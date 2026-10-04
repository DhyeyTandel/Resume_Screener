"""Module B metrics on the synthetic corpus (SYNTHETIC-ONLY, Spec 16.2).

Measured on the TEST split only. Nothing here tunes anything: the pipeline runs with the shipped
config. Every rate carries a bootstrap 95% CI (candidate-level resampling) and n. Latency is the
in-process time per candidate with the mock LLM and fake fetches, so it EXCLUDES real network time.
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "eval"))

import perturb  # noqa: E402

from app.llm.client import LLMClient  # noqa: E402
from app.llm.redaction import redact_for_scoring  # noqa: E402
from app.pipeline.orchestrator import screen_candidate  # noqa: E402
from synthetic import fakes  # noqa: E402
from synthetic.generate import FIRST, LAST  # noqa: E402

STATUSES = ["VERIFIED", "CORROBORATED", "WEAK", "UNSUPPORTED", "CONTRADICTED", "UNVERIFIABLE"]
STATUS_V = {"VERIFIED": 1.0, "CORROBORATED": 0.75, "WEAK": 0.4, "UNSUPPORTED": 0.0, "CONTRADICTED": -1.0, "UNVERIFIABLE": None}
B = 1000
BOOT_SEED = 7


# ------------------------------------------------------------------ statistics (stdlib only)
def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def boot_ci(units: list, stat, *, b: int = B, seed: int = BOOT_SEED) -> tuple[float, float]:
    """Percentile bootstrap CI. `units` are per-candidate summaries, `stat(sample_units)` -> float."""
    if not units:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(units)
    vals = []
    for _ in range(b):
        v = stat([units[rng.randrange(n)] for _ in range(n)])
        if v == v:  # drop NaN
            vals.append(v)
    vals.sort()
    return _percentile(vals, 0.025), _percentile(vals, 0.975)


def sum_vec(units: list[list[float]]) -> list[float]:
    return [sum(col) for col in zip(*units, strict=True)] if units else []


def rate(flags: list[int]) -> dict:
    n = len(flags)
    val = sum(flags) / n if n else float("nan")
    lo, hi = boot_ci(flags, lambda s: sum(s) / len(s)) if n else (float("nan"), float("nan"))
    return {"value": val, "ci": (lo, hi), "n": n, "k": sum(flags)}


def auroc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rank_pos = sum(r for r, (_, lab) in zip(ranks, allv, strict=True) if lab == 1)
    n1, n0 = len(pos), len(neg)
    return (rank_pos - n1 * (n1 + 1) / 2) / (n1 * n0)


def auroc_ci(pos: list[float], neg: list[float]) -> tuple[float, float]:
    if not pos or not neg:
        return float("nan"), float("nan")
    rng = random.Random(BOOT_SEED)
    vals = sorted(
        auroc([pos[rng.randrange(len(pos))] for _ in pos], [neg[rng.randrange(len(neg))] for _ in neg])
        for _ in range(B)
    )
    return _percentile(vals, 0.025), _percentile(vals, 0.975)


def macro_f1(conf: list[list[float]]) -> float:
    """conf[i][j] = count with true status i predicted j; labels with support or predictions only."""
    k = len(conf)
    f1s = []
    for i in range(k):
        tp = conf[i][i]
        fn = sum(conf[i]) - tp
        fp = sum(conf[r][i] for r in range(k)) - tp
        if tp + fn + fp == 0:
            continue
        f1s.append(2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0)
    return sum(f1s) / len(f1s) if f1s else float("nan")


def ece_from_bins(vec: list[float], nbins: int = 10) -> float:
    total = sum(vec[0::3])
    if not total:
        return float("nan")
    err = 0.0
    for b in range(nbins):
        cnt, sc, sa = vec[3 * b: 3 * b + 3]
        if cnt:
            err += (cnt / total) * abs(sa / cnt - sc / cnt)
    return err


def _bin_vec(pairs: list[tuple[float, int]], nbins: int = 10) -> list[float]:
    vec = [0.0] * (3 * nbins)
    for conf, ok in pairs:
        b = min(nbins - 1, int(conf * nbins))
        vec[3 * b] += 1
        vec[3 * b + 1] += conf
        vec[3 * b + 2] += ok
    return vec


# ------------------------------------------------------------------ running the pipeline
async def screen(cand: dict, *, text: str | None = None, name: str | None = None,
                 world_override: dict | None = None) -> dict:
    kw = fakes.sources(cand, world_override=world_override)
    return await screen_candidate(
        jd_text=cand["jd_text"], pasted_text=text if text is not None else cand["resume_text"],
        candidate_name=name or cand["name"], llm=LLMClient("mock"), **kw,
    )


def digest(report: dict, cand: dict) -> dict:
    ext = report["extensions"]
    a = ext.get("authenticity")
    stages = ext["meta"]["stages"]
    out = {
        "recommendation": report["recommendation"], "overall": report["overall_match_score"],
        "schema_valid": ext["meta"].get("schema_valid"),
        "auth_stage": stages.get("authenticity", {}).get("status"),
        "auth_error": stages.get("authenticity", {}).get("error"),
        "band": None,
    }
    if not isinstance(a, dict):
        return out
    gh = cand["world"].get("github")
    li = cand["world"].get("linkedin")
    pf = cand["world"].get("portfolio")
    valid = set()
    if gh:
        valid |= {f"github.com/{gh['username']}/{r['name']}" for r in gh["repos"]}
    if li:
        valid |= {f"linkedin_export:role:{r['title']}" for r in li["content"]["roles"]}
    if pf:
        valid.add(pf["url"])
    cites = [e.get("citation") for c in a["claims"] for e in c["evidence"]]
    out.update(
        band=a["band"], scores=a["scores"], sources=a["sources_used"],
        claims=[{"type": c["type"], "text": c["text"], "status": c["status"], "conf": c["judge_confidence"]}
                for c in a["claims"]],
        contradictions=[{"type": c["type"], "detail": c["detail"]} for c in a["contradictions"]],
        flags=[{"flag": f["flag"], "repo": f["repo"]} for f in a["authenticity_flags"]],
        n_cites=len(cites), n_bad_cites=sum(1 for c in cites if c not in valid),
    )
    return out


def _alt_name(cand: dict, i: int) -> str:
    return f"{FIRST[(i * 7 + 3) % len(FIRST)]} {LAST[(i * 11 + 5) % len(LAST)]}"


async def evaluate_async(cands: list[dict], *, repeats: int = 3) -> list[dict]:
    """One result record per candidate: primary run, 2 repeats (determinism), and, for genuine
    candidates, the P3 rewrite, the non-native rendering, the private-heavy world and a name swap."""
    results = []
    for i, c in enumerate(cands):
        rec: dict = {"id": c["id"], "kind": c["kind"], "genuine": c["genuine"], "error": None}
        try:
            t0 = time.perf_counter()
            rep = await screen(c)
            rec["latency_s"] = time.perf_counter() - t0
            rec["primary"] = digest(rep, c)
            rec["repeat"] = []
            for _ in range(repeats - 1):
                d = digest(await screen(c), c)
                rec["repeat"].append({"band": d["band"], "recommendation": d["recommendation"]})
            if c["genuine"]:
                rec["p3"] = digest(await screen(c, text=perturb.p3_ai_rewrite_same_facts(c["resume_text"])[0]), c)
                rec["nonnative"] = digest(await screen(c, text=c["resume_text_nonnative"]), c)
                alt = _alt_name(c, i)
                swapped = c["resume_text"].replace(c["name"], alt)
                swapped = swapped.replace(c["name"].lower().replace(" ", "."), alt.lower().replace(" ", "."))
                rec["nameswap"] = digest(await screen(c, text=swapped, name=alt), c)
                pv = (c.get("variants") or {}).get("private_heavy")
                if pv:
                    rec["private"] = digest(await screen(c, world_override=pv["world"]), c)
        except Exception as exc:  # an exception is itself a finding; keep going
            rec["error"] = f"{type(exc).__name__}: {exc}"
        results.append(rec)
    return results


# ------------------------------------------------------------------ claim matching
def _norm_gt(text: str, name: str) -> str:
    return redact_for_scoring(text, candidate_name=name)


def match_claims(cand: dict, pred: list[dict], gt_claims: list[dict] | None = None
                 ) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Multiset match on (type, text). Returns (matched pairs, missed GT claims, extra predictions)."""
    pool: dict[tuple[str, str], list[dict]] = {}
    for p in pred:
        pool.setdefault((p["type"], p["text"]), []).append(p)
    pairs, missed = [], []
    for g in gt_claims if gt_claims is not None else cand["claims"]:
        key = (g["type"], _norm_gt(g["text"], cand["name"]))
        if pool.get(key):
            pairs.append((g, pool[key].pop()))
        else:
            missed.append(g)
    extra = [p for v in pool.values() for p in v]
    return pairs, missed, extra


# ------------------------------------------------------------------ metrics
def _row(name, res: dict, target: str, met: bool | None, note: str = "") -> dict:
    return {"metric": name, "value": res["value"], "ci": res["ci"], "n": res["n"], "target": target,
            "met": met, "note": note, **({"k": res["k"]} if "k" in res else {})}


def compute_metrics(cands: list[dict], results: list[dict]) -> dict:
    by_id = {c["id"]: c for c in cands}
    ok = [r for r in results if r["error"] is None and r["primary"]["band"] is not None]
    errors = [r for r in results if r["error"] is not None or r["primary"]["band"] is None]
    rows: list[dict] = []
    extra: dict = {"n_candidates": len(results), "errors": [
        {"id": r["id"], "kind": r["kind"], "error": r["error"] or r["primary"].get("auth_error") or "no authenticity block"}
        for r in errors]}

    # --- 1. claim extraction -------------------------------------------------
    per_cand, per_type = [], {}
    for r in ok:
        c = by_id[r["id"]]
        pairs, missed, extras = match_claims(c, r["primary"]["claims"])
        per_cand.append([len(pairs), len(extras), len(missed)])
        for g, _ in pairs:
            per_type.setdefault(g["type"], [0, 0])[0] += 1
        for g in missed:
            per_type.setdefault(g["type"], [0, 0])[1] += 1
    tp, fp, fn = sum_vec(per_cand)
    prec, rec_ = tp / (tp + fp) if tp + fp else float("nan"), tp / (tp + fn) if tp + fn else float("nan")

    def f1_of(units):
        a, b, c_ = sum_vec(units)
        return 2 * a / (2 * a + b + c_) if (2 * a + b + c_) else float("nan")

    f1 = f1_of(per_cand)
    lo, hi = boot_ci(per_cand, f1_of)
    for nm, val, fn_ in (("precision", prec, lambda u: sum_vec(u)[0] / max(1, sum_vec(u)[0] + sum_vec(u)[1])),
                         ("recall", rec_, lambda u: sum_vec(u)[0] / max(1, sum_vec(u)[0] + sum_vec(u)[2]))):
        l2, h2 = boot_ci(per_cand, fn_)
        rows.append({"metric": f"Claim extraction {nm} (type+text match)", "value": val, "ci": (l2, h2),
                     "n": int(tp + fn), "target": "informational", "met": None, "note": ""})
    rows.append({"metric": "Claim extraction F1 vs generated claims", "value": f1, "ci": (lo, hi), "n": int(tp + fn),
                 "target": ">= 0.85", "met": f1 >= 0.85, "note": f"n = generated claims; TP {int(tp)}, FP {int(fp)}, FN {int(fn)}"})
    extra["extraction_by_type"] = {t: {"found": v[0], "missed": v[1], "recall": v[0] / (v[0] + v[1])}
                                   for t, v in sorted(per_type.items())}

    # --- 2. claim status macro-F1 ---------------------------------------------
    idx = {s: i for i, s in enumerate(STATUSES)}
    per_cand_conf, mismatches = [], []
    for r in ok:
        c = by_id[r["id"]]
        conf = [[0] * 6 for _ in range(6)]
        pairs, _, _ = match_claims(c, r["primary"]["claims"])
        for g, p in pairs:
            if g["gt"] is None:
                continue
            conf[idx[g["gt"]]][idx[p["status"]]] += 1
            if g["gt"] != p["status"]:
                mismatches.append({"id": r["id"], "kind": r["kind"], "type": g["type"], "text": g["text"][:80],
                                   "gt": g["gt"], "pred": p["status"]})
        per_cand_conf.append([x for row in conf for x in row])
    total = sum_vec(per_cand_conf)
    mat = [[total[i * 6 + j] for j in range(6)] for i in range(6)]

    def mf1(units):
        t = sum_vec(units)
        return macro_f1([[t[i * 6 + j] for j in range(6)] for i in range(6)])

    m = mf1(per_cand_conf)
    lo, hi = boot_ci(per_cand_conf, mf1)
    n_pairs = int(sum(total))
    acc = sum(mat[i][i] for i in range(6)) / n_pairs if n_pairs else float("nan")
    rows.append({"metric": "Claim status macro-F1", "value": m, "ci": (lo, hi), "n": n_pairs, "target": ">= 0.75",
                 "met": m >= 0.75, "note": f"over matched claims with an unambiguous label; accuracy {acc:.3f}"})
    extra["confusion"] = {"labels": STATUSES, "matrix": mat}
    extra["status_by_type"] = _status_by_type(ok, by_id)

    def sev(x):
        a, b = STATUS_V.get(x["gt"]), STATUS_V.get(x["pred"])
        return abs((a if a is not None else 0.5) - (b if b is not None else 0.5))

    extra["worst_status_errors"] = sorted(mismatches, key=sev, reverse=True)[:10]
    extra["n_status_errors"] = len(mismatches)

    # --- 3. detection recall per perturbation ---------------------------------
    p1_units = []
    for r in ok:
        if r["kind"] != "P1":
            continue
        c = by_id[r["id"]]
        texts = [g["text"] for g in c["claims"] if g["type"] == "SKILL" and g["ref"].get("injected")]
        det = 0
        pred = {(p["type"], p["text"]): p["status"] for p in r["primary"]["claims"]}
        for t in texts:
            if pred.get(("SKILL", t)) in ("UNSUPPORTED", "CONTRADICTED"):
                det += 1
        p1_units.append([det, len(texts), 1 if det else 0, 1 if det == len(texts) else 0])
    if p1_units:
        d, t, anyd, alld = sum_vec(p1_units)
        lo, hi = boot_ci(p1_units, lambda u: sum_vec(u)[0] / sum_vec(u)[1])
        rows.append({"metric": "Detection recall P1 (injected skill flagged UNSUPPORTED)", "value": d / t, "ci": (lo, hi),
                     "n": int(t), "target": ">= 0.80", "met": d / t >= 0.80,
                     "note": f"claim-level over {len(p1_units)} candidates; candidate-level any-flag {anyd / len(p1_units):.2f}, all-flagged {alld / len(p1_units):.2f}"})
    p2 = []
    for r in ok:
        if r["kind"] == "P2":
            c = by_id[r["id"]]
            pred = {(p["type"], p["text"]): p["status"] for p in r["primary"]["claims"]}
            p2.append(1 if pred.get(("METRIC", c["expected"]["p2_claim_text"])) == "CONTRADICTED" else 0)
    rows.append(_row("Detection recall P2 (inflated metric flagged CONTRADICTED)", rate(p2), ">= 0.80",
                     (sum(p2) / len(p2) >= 0.8) if p2 else None,
                     "candidate-level; every P2 candidate has a README figure that conflicts with the resume"))
    p4, p4_sub = [], {}
    for r in ok:
        if r["kind"] == "P4":
            c = by_id[r["id"]]
            exp = set(c["expected"]["p4_types"])
            hit = int(any(x["type"] in exp for x in r["primary"]["contradictions"]))
            p4.append(hit)
            p4_sub.setdefault(c["expected"]["p4_subtype"], []).append(hit)
    rows.append(_row("Detection recall P4 (expected contradiction type raised)", rate(p4), ">= 0.80",
                     (sum(p4) / len(p4) >= 0.8) if p4 else None, "candidate-level"))
    extra["p4_by_subtype"] = {k: {"hit": sum(v), "n": len(v)} for k, v in sorted(p4_sub.items())}
    p5, p5_sub = [], {}
    for r in ok:
        if r["kind"] == "P5":
            c = by_id[r["id"]]
            exp = set(c["expected"]["p5_flags"])
            hit = int(any(f["repo"] == c["expected"]["p5_repo"] and f["flag"] in exp for f in r["primary"]["flags"]))
            p5.append(hit)
            p5_sub.setdefault(c["expected"]["p5_variant"], []).append(hit)
    rows.append(_row("Detection recall P5 (flag on the fork / tutorial repo)", rate(p5), ">= 0.80",
                     (sum(p5) / len(p5) >= 0.8) if p5 else None, "candidate-level"))
    extra["p5_by_variant"] = {k: {"hit": sum(v), "n": len(v)} for k, v in sorted(p5_sub.items())}

    # --- 4. false accusation ---------------------------------------------------
    gen = [r for r in ok if r["genuine"]]
    fa = [1 if r["primary"]["band"] == "NEEDS_VERIFICATION" else 0 for r in gen]
    rows.append(_row("False-accusation rate (genuine -> NEEDS_VERIFICATION)", rate(fa), "<= 0.05",
                     (sum(fa) / len(fa) <= 0.05) if fa else None, "genuine plus P6 private-work candidates"))
    gen_only = [1 if r["primary"]["band"] == "NEEDS_VERIFICATION" else 0 for r in gen if r["kind"] == "genuine"]
    rows.append(_row("False-accusation rate, genuine with public GitHub only", rate(gen_only), "<= 0.05",
                     (sum(gen_only) / len(gen_only) <= 0.05) if gen_only else None, "excludes P6"))
    extra["bands_by_kind"] = {k: dict(Counter(r["primary"]["band"] for r in ok if r["kind"] == k))
                              for k in ("genuine", "P1", "P2", "P4", "P5", "P6")}
    extra["false_accusation_by_sources"] = _fa_by_sources(gen, by_id)

    # --- 5. P3 invariance -------------------------------------------------------
    drops = [r["primary"]["scores"]["reliability"] - r["p3"]["scores"]["reliability"]
             for r in gen if r.get("p3", {}).get("band")]
    if drops:
        mean = sum(drops) / len(drops)
        lo, hi = boot_ci(drops, lambda s: sum(s) / len(s))
        rows.append({"metric": "P3 invariance: mean reliability drop after truthful AI rewrite", "value": mean, "ci": (lo, hi),
                     "n": len(drops), "target": "<= 0.05", "met": mean <= 0.05, "note": "positive = reliability fell"})
        mx = max(drops)
        over = sum(1 for d in drops if d > 0.05)
        rows.append({"metric": "P3 invariance: max reliability drop", "value": mx, "ci": (float("nan"), float("nan")),
                     "n": len(drops), "target": "<= 0.05", "met": mx <= 0.05,
                     "note": f"{over} of {len(drops)} candidates dropped by more than 0.05; band changed for "
                             f"{sum(1 for r in gen if r.get('p3', {}).get('band') and r['p3']['band'] != r['primary']['band'])}; "
                             f"recommendation changed for "
                             f"{sum(1 for r in gen if r.get('p3') and r['p3']['recommendation'] != r['primary']['recommendation'])}"})
    extra["p3_worst"] = sorted(((round(d, 3), r["id"]) for d, r in zip(drops, [r for r in gen if r.get("p3", {}).get("band")], strict=True)),
                               reverse=True)[:5]

    # --- 6. P6 ------------------------------------------------------------------
    p6 = [1 if r["primary"]["band"] == "INSUFFICIENT_EVIDENCE" else 0 for r in ok if r["kind"] == "P6"]
    rows.append(_row("P6 genuine no-GitHub -> INSUFFICIENT_EVIDENCE", rate(p6), ">= 0.95",
                     (sum(p6) / len(p6) >= 0.95) if p6 else None, ""))
    p6_sub: dict = {}
    for r in ok:
        if r["kind"] == "P6":
            c = by_id[r["id"]]
            key = f"{c['expected']['p6_variant']}, linkedin={'yes' if c['sources']['linkedin'] else 'no'}"
            p6_sub.setdefault(key, []).append(r["primary"]["band"])
    extra["p6_breakdown"] = {k: dict(Counter(v)) for k, v in sorted(p6_sub.items())}

    # --- 7. AUROC ---------------------------------------------------------------
    pos = [r["primary"]["scores"]["authenticity"] for r in ok if r["kind"] == "genuine"]
    neg = [r["primary"]["scores"]["authenticity"] for r in ok if r["kind"] in ("P1", "P2", "P4", "P5")]
    if pos and neg:
        a = auroc(pos, neg)
        lo, hi = auroc_ci(pos, neg)
        rows.append({"metric": "AUROC authenticity score, genuine vs perturbed", "value": a, "ci": (lo, hi),
                     "n": len(pos) + len(neg), "target": ">= 0.85", "met": a >= 0.85,
                     "note": f"{len(pos)} genuine vs {len(neg)} perturbed (P1/P2/P4/P5); sklearn not installed, rank-based"})
        per = {}
        for k in ("P1", "P2", "P4", "P5"):
            nk = [r["primary"]["scores"]["authenticity"] for r in ok if r["kind"] == k]
            if nk:
                per[k] = round(auroc(pos, nk), 3)
        extra["auroc_by_perturbation"] = per
        extra["mean_authenticity"] = {k: round(sum(v) / len(v), 3) for k, v in
                                      {k: [r["primary"]["scores"]["authenticity"] for r in ok if r["kind"] == k]
                                       for k in ("genuine", "P1", "P2", "P4", "P5", "P6")}.items() if v}
        extra["mean_reliability"] = {k: round(sum(v) / len(v), 3) for k, v in
                                     {k: [r["primary"]["scores"]["reliability"] for r in ok if r["kind"] == k]
                                      for k in ("genuine", "P1", "P2", "P4", "P5", "P6")}.items() if v}
        extra["mean_confidence"] = {k: round(sum(v) / len(v), 3) for k, v in
                                    {k: [r["primary"]["scores"]["assessment_confidence"] for r in ok if r["kind"] == k]
                                     for k in ("genuine", "P1", "P2", "P4", "P5", "P6")}.items() if v}

    # --- 8. calibration -----------------------------------------------------------
    units_j, units_all = [], []
    for r in ok:
        c = by_id[r["id"]]
        pairs, _, _ = match_claims(c, r["primary"]["claims"])
        pj = [(p["conf"], int(p["status"] == g["gt"])) for g, p in pairs if g["gt"] is not None and STATUS_V[p["status"]] is not None]
        pa = [(p["conf"], int(p["status"] == g["gt"])) for g, p in pairs if g["gt"] is not None]
        units_j.append(_bin_vec(pj))
        units_all.append(_bin_vec(pa))
    for nm, units, tgt in (("Calibration ECE of judge_confidence (judged claims)", units_j, "<= 0.10"),
                           ("Calibration ECE, all matched claims (UNVERIFIABLE has confidence 0)", units_all, "informational")):
        e = ece_from_bins(sum_vec(units))
        lo, hi = boot_ci(units, lambda u: ece_from_bins(sum_vec(u)))
        n = int(sum(sum_vec(units)[0::3]))
        rows.append({"metric": nm, "value": e, "ci": (lo, hi), "n": n, "target": tgt,
                     "met": (e <= 0.10) if tgt != "informational" else None, "note": "10 equal-width bins; correct = status equals ground truth"})
    # reliability diagram data
    vec = sum_vec(units_j)
    extra["calibration_bins"] = [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": int(vec[3 * b]),
                                  "mean_conf": round(vec[3 * b + 1] / vec[3 * b], 3) if vec[3 * b] else None,
                                  "accuracy": round(vec[3 * b + 2] / vec[3 * b], 3) if vec[3 * b] else None}
                                 for b in range(10)]

    # --- 9. fairness --------------------------------------------------------------
    for label, key, who in (("native vs non-native English", "nonnative", gen),
                            ("public-heavy vs private-heavy GitHub", "private", [r for r in gen if r["kind"] == "genuine"])):
        pairs_ = [(int(r["primary"]["band"] == "NEEDS_VERIFICATION"), int(r[key]["band"] == "NEEDS_VERIFICATION"))
                  for r in who if r.get(key, {}).get("band")]
        if not pairs_:
            continue
        a_rate = sum(p[0] for p in pairs_) / len(pairs_)
        b_rate = sum(p[1] for p in pairs_) / len(pairs_)
        gap = (b_rate - a_rate) * 100
        lo, hi = boot_ci(pairs_, lambda s: (sum(p[1] for p in s) - sum(p[0] for p in s)) / len(s) * 100)
        rows.append({"metric": f"Fairness false-flag gap, {label} (pp)", "value": gap, "ci": (lo, hi), "n": len(pairs_),
                     "target": "<= 5 pp (absolute)", "met": abs(gap) <= 5.0,
                     "note": f"false-flag rate {a_rate:.3f} -> {b_rate:.3f}; paired on the same candidates; gap = second minus first"})
    nn = [(r["primary"]["scores"], r["nonnative"]["scores"]) for r in gen if r.get("nonnative", {}).get("band")]
    if nn:
        extra["nonnative_mean_deltas"] = {k: round(sum(b[k] - a[k] for a, b in nn) / len(nn), 4)
                                          for k in ("authenticity", "reliability", "assessment_confidence", "inflation_index")}
    pv = [(r["primary"]["scores"], r["private"]["scores"]) for r in gen if r.get("private", {}).get("band")]
    if pv:
        extra["private_mean_deltas"] = {k: round(sum(b[k] - a[k] for a, b in pv) / len(pv), 4)
                                        for k in ("authenticity", "reliability", "assessment_confidence", "inflation_index")}
        extra["private_bands"] = dict(Counter(r["private"]["band"] for r in gen if r.get("private", {}).get("band")))
    extra["nonnative_facts_preserved"] = f"{sum(1 for c in cands if c.get('nonnative_facts_preserved'))}/{sum(1 for c in cands if 'resume_text_nonnative' in c)}"
    nn_units = []
    for r in gen:
        c = by_id[r["id"]]
        if r.get("nonnative", {}).get("band") and "claims_nonnative" in c:
            a_ = match_claims(c, r["primary"]["claims"])
            b_ = match_claims(c, r["nonnative"]["claims"], c["claims_nonnative"])
            nn_units.append([len(a_[0]), len(a_[2]), len(a_[1]), len(b_[0]), len(b_[2]), len(b_[1])])
    if nn_units:
        def _f1(v, o):
            return 2 * v[o] / (2 * v[o] + v[o + 1] + v[o + 2]) if (2 * v[o] + v[o + 1] + v[o + 2]) else float("nan")

        def _gap(u):
            v = sum_vec(u)
            return (_f1(v, 3) - _f1(v, 0)) * 100

        v = sum_vec(nn_units)
        lo, hi = boot_ci(nn_units, _gap)
        rows.append({"metric": "Fairness claim-extraction F1 gap, non-native minus native (pp)", "value": _gap(nn_units),
                     "ci": (lo, hi), "n": int(v[0] + v[2]), "target": "<= 5 pp (absolute)",
                     "met": abs(_gap(nn_units)) <= 5.0,
                     "note": f"F1 native {_f1(v, 0):.3f}, non-native {_f1(v, 3):.3f}; paired on the same genuine candidates"})
    ns = [int(r["primary"]["scores"] == r["nameswap"]["scores"] and r["primary"]["band"] == r["nameswap"]["band"]
              and r["primary"]["recommendation"] == r["nameswap"]["recommendation"])
          for r in gen if r.get("nameswap", {}).get("band")]
    rows.append(_row("Name-swap invariance (identical scores, band, recommendation)", rate(ns), "100%",
                     (sum(ns) == len(ns)) if ns else None, "Spec 16.3 fairness invariant, synthetic names"))

    # --- 10. determinism -------------------------------------------------------------
    det = [int(all(x["band"] == r["primary"]["band"] and x["recommendation"] == r["primary"]["recommendation"]
                   for x in r["repeat"])) for r in ok]
    rows.append(_row("Determinism (same input x3 -> same band + recommendation)", rate(det), "100%",
                     (sum(det) == len(det)) if det else None, ""))

    # --- 11. latency -------------------------------------------------------------------
    lats = sorted(r["latency_s"] for r in ok)
    for q, nm, tgt in ((0.5, "p50", 25.0), (0.95, "p95", 60.0)):
        v = _percentile(lats, q)
        lo, hi = boot_ci(lats, lambda s, q=q: _percentile(sorted(s), q))
        rows.append({"metric": f"Latency {nm} per candidate, seconds (mock LLM + fake fetches, EXCLUDES real network time)",
                     "value": v, "ci": (lo, hi), "n": len(lats), "target": f"<= {tgt:.0f} s with real GitHub",
                     "met": None, "note": "in-process only; says nothing about the real-network target"})

    # --- 12. extras: citations, spurious flags, schema ------------------------------------
    cites = sum(r["primary"].get("n_cites", 0) for r in ok)
    bad = sum(r["primary"].get("n_bad_cites", 0) for r in ok)
    cite_units = [[r["primary"].get("n_cites", 0), r["primary"].get("n_bad_cites", 0)] for r in ok]
    clo, chi = boot_ci(cite_units, lambda u: 1 - sum_vec(u)[1] / max(1, sum_vec(u)[0]))
    rows.append({"metric": "Citation validity (evidence citation resolves to a collected artifact)",
                 "value": (cites - bad) / cites if cites else float("nan"), "ci": (clo, chi),
                 "n": cites, "target": "100%", "met": bad == 0, "note": f"{bad} unresolved citations"})
    spurious = [int(any(f["flag"] in ("fork_claimed_as_own", "tutorial_clone") for f in r["primary"]["flags"])) for r in gen]
    rows.append(_row("Spurious fork/tutorial flag on genuine candidates", rate(spurious), "informational", None,
                     "genuine candidates never claim a fork or tutorial repo as their own"))
    extra["diagnostics"] = diagnose(cands, results)
    invalid = [r["id"] for r in ok if r["primary"].get("schema_valid") is False]
    extra["schema_invalid"] = invalid
    extra["n_ok"] = len(ok)
    return {"rows": rows, "extra": extra}


def _status_by_type(ok: list[dict], by_id: dict) -> dict:
    out: dict = {}
    for r in ok:
        c = by_id[r["id"]]
        pairs, _, _ = match_claims(c, r["primary"]["claims"])
        for g, p in pairs:
            if g["gt"] is None:
                continue
            d = out.setdefault(g["type"], [0, 0])
            d[0] += int(g["gt"] == p["status"])
            d[1] += 1
    return {t: {"correct": v[0], "n": v[1], "accuracy": round(v[0] / v[1], 3)} for t, v in sorted(out.items())}


def _fa_by_sources(gen: list[dict], by_id: dict) -> dict:
    out: dict = {}
    for r in gen:
        c = by_id[r["id"]]
        s = c["sources"]
        key = ("github" if s["github"] else "no-github") + ("+linkedin" if s["linkedin"] else "") + ("+portfolio" if s["portfolio"] else "")
        d = out.setdefault(key, [0, 0])
        d[0] += int(r["primary"]["band"] == "NEEDS_VERIFICATION")
        d[1] += 1
    return {k: {"flagged": v[0], "n": v[1]} for k, v in sorted(out.items())}


# ------------------------------------------------------------------ diagnostics
_MANIFEST_SKILLS = {"requirements.txt": {"python", "fastapi", "django", "flask"},
                    "pyproject.toml": {"python", "fastapi", "django", "flask"},
                    "package.json": {"javascript", "typescript", "react", "vue", "node"},
                    "pom.xml": {"java"}, "go.mod": {"go", "golang"}, "Dockerfile": {"docker"}}


def _authorship(repo: dict) -> float:
    tot = repo["commits"]["mine"] + repo["commits"]["others"]
    return repo["commits"]["mine"] / tot if tot else 0.0


def _surface(repo: dict) -> str:
    return " ".join([*repo["languages"], *repo["topics"], repo["readme"] or ""]).lower()


def diagnose(cands: list[dict], results: list[dict]) -> dict:
    """Name the mechanism behind each mismatch, using the world data (deterministic rules, no
    peeking at the code under test). Counts fall to zero by themselves when a mechanism is fixed."""
    by_id = {c["id"]: c for c in cands}
    buckets: dict[str, list] = {}

    def add(name: str, cid: str, what: str) -> None:
        buckets.setdefault(name, []).append((cid, what))

    for r in results:
        if r["error"] is not None or r["primary"]["band"] is None:
            continue
        c = by_id[r["id"]]
        gh = c["world"].get("github") or {"repos": []}
        repos = gh["repos"]
        pairs, missed, extras = match_claims(c, r["primary"]["claims"])
        for g in missed:
            if g["type"] == "SKILL" and len(g["text"]) < 3:
                add("skill_name_under_3_chars_never_extracted", r["id"], g["text"])
            elif g["type"] == "SKILL" and g["text"] == "CI/CD":
                add("ci_cd_in_skills_line_split_on_slash", r["id"], g["text"])
            elif g["type"] == "ACHIEVEMENT" and g["ref"].get("section") == "achievements":
                add("achievements_section_not_extracted", r["id"], g["text"][:50])
        for p in extras:
            if p["type"] == "SKILL" and ": " in p["text"]:
                add("grouped_skills_line_keeps_label_as_part_of_claim", r["id"], p["text"])
        for g, p in pairs:
            if g["gt"] is None or g["gt"] == p["status"]:
                continue
            t, gt, pr = g["type"], g["gt"], p["status"]
            if t == "SKILL" and pr == "VERIFIED":
                low = g["text"].lower()
                skill = g["ref"]["skill"]
                authored = [rp for rp in repos if not rp["fork"] and _authorship(rp) >= 0.30]
                viaman = any(skill in _MANIFEST_SKILLS.get(m, set()) for rp in authored for m in rp["manifests"])
                viasub = any(low in _surface(rp) for rp in authored)
                if viaman and not viasub:
                    add("skill_verified_from_manifest_file_name_alone", r["id"], f"{g['text']} (claimed, not used in any repo)")
                elif viasub:
                    add("skill_verified_by_substring_of_unrelated_text", r["id"], f"{g['text']!r} found inside a language, topic or README string")
                else:
                    add("skill_verified_other", r["id"], g["text"])
            elif t == "SKILL" and gt == "VERIFIED" and pr == "UNSUPPORTED":
                add("alias_spelling_not_canonicalised", r["id"], f"{g['text']} (skill {g['ref']['skill']})")
            elif t == "SKILL" and gt == "UNVERIFIABLE" and pr == "UNSUPPORTED":
                add("github_account_with_no_public_repos_treated_as_checked", r["id"], g["text"])
            elif t == "PROJECT" and gt == "UNVERIFIABLE" and pr == "UNSUPPORTED":
                add("github_account_with_no_public_repos_treated_as_checked", r["id"], g["text"][:50])
            elif t == "SKILL" and gt == "WEAK" and pr == "UNSUPPORTED":
                add("portfolio_evidence_ignored_when_github_has_repos", r["id"], g["text"])
            elif t == "PROJECT" and pr in ("VERIFIED", "WEAK") and gt in ("UNSUPPORTED", "VERIFIED"):
                if g["ref"]["repo"] is None or gt == "UNSUPPORTED":
                    add("project_matched_to_unrelated_repo_by_shared_word", r["id"], f"{g['text'][:60]} -> {pr}")
                else:
                    add("project_shadowed_by_fork_or_other_repo_named_like_a_word_in_it", r["id"], f"{g['text'][:60]} -> {pr}")
            elif t == "METRIC":
                add("metric_claims_never_checked_against_readme_figures", r["id"], f"{gt} -> {pr}")
            elif t == "ROLE" and gt == "CONTRADICTED":
                add("role_status_ignores_linkedin_conflict", r["id"], f"{g['text']} -> {pr}")
            else:
                add(f"other_{t}_{gt}_to_{pr}", r["id"], g["text"][:50])
    for r in results:
        if r["error"] is not None or not r["genuine"] or r["primary"]["band"] is None:
            continue
        c = by_id[r["id"]]
        repos = {rp["name"]: rp for rp in (c["world"].get("github") or {"repos": []})["repos"]}
        for f in r["primary"]["flags"]:
            rp = repos.get(f["repo"])
            if f["flag"] == "tutorial_clone" and rp and not rp["tutorial"] and "tutorial" in (rp["readme"] or "").lower():
                add("genuine_repo_flagged_tutorial_clone_for_the_word_tutorial_in_its_readme", r["id"], f["repo"])
            elif f["flag"] == "fork_claimed_as_own" and rp and rp["fork"]:
                add("untouched_fork_flagged_claimed_as_own_because_its_name_matches_a_skill_or_project_word", r["id"], f["repo"])
    gen_flag = []
    for r in results:
        if r["error"] is None and r["genuine"] and r["primary"]["band"] == "NEEDS_VERIFICATION":
            pr = r["primary"]
            why = "contradiction_raised" if pr["contradictions"] else (
                "low_assessment_confidence_with_high_reliability" if pr["scores"]["reliability"] >= 0.75 else "low_reliability")
            gen_flag.append((r["id"], why, pr["scores"]["assessment_confidence"], pr["scores"]["reliability"],
                             [x["type"] for x in pr["contradictions"]]))
    spurious_contra = [(r["id"], x["type"]) for r in results if r["error"] is None and r["genuine"]
                       for x in r["primary"]["contradictions"]]
    return {"buckets": {k: {"count": len(v), "examples": v[:3]} for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))},
            "false_accusations": gen_flag, "spurious_contradictions_on_genuine": spurious_contra}


# ------------------------------------------------------------------ report
def _fmt(v: float, kind: str = "") -> str:
    return "n/a" if v != v else f"{v:.3f}"


def render_markdown(out: dict, manifest: dict | None = None) -> str:
    L: list[str] = []
    ex = out["extra"]
    L.append("**SYNTHETIC-ONLY.** Every number in this section comes from invented candidates whose ground truth was "
             "set by construction (`eval/synthetic/generate.py`). The synthetic numbers do not establish real-world "
             "performance: they measure how the shipped code behaves on a corpus that I wrote, including my own "
             "modelling choices, and the real-data targets in Spec 16.1 (human-labelled resumes, two annotators, "
             "Cohen's kappa) are still unmet.")
    L.append("")
    if manifest:
        c = manifest["counts"]["test"]
        L.append(f"Corpus: {manifest['n']} candidates (seed {manifest['seed']}), fixed stratified split "
                 f"{manifest['counts']['train']['n']} train / {c['n']} test. **All numbers below use the test split only** "
                 f"({', '.join(f'{k} {c[k]}' for k in ('genuine', 'P1', 'P2', 'P4', 'P5', 'P6'))}). "
                 "Nothing was tuned on either split and the shipped `config.yaml` is used unchanged. "
                 f"Rates carry a candidate-level bootstrap 95% CI ({B} resamples).")
        L.append("")
    L.append("| Metric | Value | 95% CI | n | Target | Met |")
    L.append("|---|---|---|---|---|---|")
    for r in out["rows"]:
        met = "yes" if r["met"] is True else ("NO" if r["met"] is False else "n/a")
        ci = "n/a" if r["ci"][0] != r["ci"][0] else f"[{_fmt(r['ci'][0])}, {_fmt(r['ci'][1])}]"
        L.append(f"| {r['metric']} | {_fmt(r['value'])} | {ci} | {r['n']} | {r['target']} | {met} |")
    L.append("")
    notes = [r for r in out["rows"] if r["note"]]
    if notes:
        L.append("Notes on the rows above:")
        L.append("")
        for r in notes:
            L.append(f"- {r['metric']}: {r['note']}")
        L.append("")
    L.append(f"Latency is measured in-process per candidate with the mock LLM and in-memory fake fetches, so it excludes "
             f"real network time entirely (median {_fmt(next(r['value'] for r in out['rows'] if r['metric'].startswith('Latency p50')))} s). "
             f"The whole synthetic run (about {ex['n_candidates']} candidates, with the P3, non-native, private-heavy, name-swap and "
             f"repeat runs) took {out['wall_s']:.1f} s.")
    L.append("")
    L.append("### Claim status confusion matrix (rows = ground truth, columns = predicted)")
    L.append("")
    labels = ex["confusion"]["labels"]
    L.append("| truth \\ predicted | " + " | ".join(labels) + " |")
    L.append("|---|" + "---|" * len(labels))
    for lab, row in zip(labels, ex["confusion"]["matrix"], strict=True):
        L.append(f"| {lab} | " + " | ".join(str(int(x)) for x in row) + " |")
    L.append("")
    L.append("Claims whose label is ambiguous by construction (a fork or tutorial repo claimed as the candidate's own) are "
             "left out of the status scores.")
    L.append("")
    L.append("### Extraction and status by claim type")
    L.append("")
    L.append("| Type | Found | Missed | Extraction recall | Status accuracy (matched) |")
    L.append("|---|---|---|---|---|")
    for t, v in ex["extraction_by_type"].items():
        sa = ex["status_by_type"].get(t)
        L.append(f"| {t} | {v['found']} | {v['missed']} | {v['recall']:.3f} | "
                 f"{(str(sa['correct']) + '/' + str(sa['n']) + ' = ' + format(sa['accuracy'], '.3f')) if sa else 'n/a'} |")
    L.append("")
    L.append("### Band distribution by candidate kind")
    L.append("")
    bands = ["HIGH_TRUST", "MODERATE", "NEEDS_VERIFICATION", "INSUFFICIENT_EVIDENCE"]
    L.append("| Kind | " + " | ".join(bands) + " | mean authenticity | mean reliability | mean confidence |")
    L.append("|---|" + "---|" * (len(bands) + 3))
    for k, d in ex["bands_by_kind"].items():
        L.append(f"| {k} | " + " | ".join(str(d.get(b, 0)) for b in bands) +
                 f" | {ex.get('mean_authenticity', {}).get(k, 'n/a')} | {ex.get('mean_reliability', {}).get(k, 'n/a')} | {ex.get('mean_confidence', {}).get(k, 'n/a')} |")
    L.append("")
    L.append("### Breakdowns")
    L.append("")
    p4s = ", ".join(f"{k} {v['hit']}/{v['n']}" for k, v in ex["p4_by_subtype"].items())
    p5s = ", ".join(f"{k} {v['hit']}/{v['n']}" for k, v in ex["p5_by_variant"].items())
    fas = ", ".join(f"{k} {v['flagged']}/{v['n']}" for k, v in ex["false_accusation_by_sources"].items())
    L.append(f"- P4 recall by subtype (hit/n): {p4s}.")
    L.append(f"- P5 recall by variant (hit/n): {p5s}.")
    L.append(f"- AUROC of authenticity, genuine vs each perturbation alone: {ex.get('auroc_by_perturbation')}.")
    L.append(f"- P6 band by variant: {ex['p6_breakdown']}.")
    L.append(f"- False accusations by source availability (flagged/n): {fas}.")
    L.append(f"- Non-native English rendering: facts, numbers and technologies preserved for {ex['nonnative_facts_preserved']} candidates; "
             f"mean score change (non-native minus native) {ex.get('nonnative_mean_deltas')}.")
    L.append(f"- Private-heavy world: mean score change (private minus public) {ex.get('private_mean_deltas')}; bands {ex.get('private_bands')}.")
    L.append(f"- Reliability diagram (judged claims): {[(b['bin'], b['n'], b['mean_conf'], b['accuracy']) for b in ex['calibration_bins'] if b['n']]}.")
    if ex["errors"]:
        L.append(f"- **Exceptions or missing authenticity blocks: {len(ex['errors'])}**, e.g. {ex['errors'][:3]}.")
    else:
        L.append("- No exceptions, no missing authenticity block, and no schema-invalid report across the test split.")
    L.append("")
    d = ex["diagnostics"]
    L.append("### Mechanisms behind the misses (deterministic attribution from the synthetic world)")
    L.append("")
    L.append("Each bucket names why a claim label or extraction differs from ground truth, with the first example candidate "
             "ids in `eval/datasets/synthetic/test.json`. These are findings about the shipped code, reported as measured. "
             "Several mean that the 'ground truth' reflects the Spec rubric while the code implements a simpler rule.")
    L.append("")
    L.append("| Mechanism | Count | Example (candidate id: detail) |")
    L.append("|---|---|---|")
    for k, v in d["buckets"].items():
        exs = "; ".join(f"{a}: {b}" for a, b in v["examples"][:2])
        L.append(f"| {k} | {v['count']} | {exs} |")
    L.append("")
    fa = d["false_accusations"]
    why = Counter(x[1] for x in fa)
    L.append(f"False accusations (genuine candidates in NEEDS_VERIFICATION): {len(fa)}; by cause {dict(why)}. "
             f"Spurious contradictions raised on genuine candidates: {len(d['spurious_contradictions_on_genuine'])} "
             f"({dict(Counter(x[1] for x in d['spurious_contradictions_on_genuine']))}).")
    L.append("")
    L.append("### Ten worst claim-status errors")
    L.append("")
    L.append("| Candidate | Kind | Type | Claim | Truth | Predicted |")
    L.append("|---|---|---|---|---|---|")
    for e in ex["worst_status_errors"]:
        L.append(f"| {e['id']} | {e['kind']} | {e['type']} | {e['text'].replace('|', '/')} | {e['gt']} | {e['pred']} |")
    L.append("")
    L.append(f"Total claim-status errors on matched claims: {ex['n_status_errors']}.")
    return "\n".join(L)


# ------------------------------------------------------------------ entry points
def load_split(split: str = "test", corpus_dir: Path | None = None) -> list[dict]:
    d = corpus_dir or ROOT / "eval" / "datasets" / "synthetic"
    return json.loads((d / f"{split}.json").read_text())


async def run_split_async(cands: list[dict]) -> dict:
    t0 = time.perf_counter()
    results = await evaluate_async(cands)
    out = compute_metrics(cands, results)
    out["results"] = results
    out["wall_s"] = time.perf_counter() - t0
    return out


def fmt_num(v: float) -> str:
    return "n/a" if v != v else f"{v:.3f}"


def run_split(split: str = "test") -> dict:
    assert split == "test", "metrics are reported on the test split only; the train split exists for diagnostics"
    return asyncio.run(run_split_async(load_split(split)))
