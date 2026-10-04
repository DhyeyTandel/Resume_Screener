"""Smoke test for the synthetic Module B evaluation (eval/synthetic/). SYNTHETIC-ONLY data.

Generates a tiny corpus (10 candidates), runs the metrics code on its test split and checks that every
value is in a valid range. It makes no network call: a socket guard fails the test if one is attempted.
"""
from __future__ import annotations

import math
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))

from synthetic import generate as gen  # noqa: E402
from synthetic import metrics as met  # noqa: E402

N = 10


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the synthetic evaluation must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_generator_is_deterministic_and_split_is_fixed():
    a, b = gen.generate(N, seed=11), gen.generate(N, seed=11)
    assert [c["resume_text"] for c in a] == [c["resume_text"] for c in b]
    assert [c["split"] for c in a] == [c["split"] for c in b]
    assert {c["split"] for c in a} <= {"train", "test"}
    assert {c["kind"] for c in a if c["split"] == "test"} == {"genuine", "P1", "P2", "P4", "P5", "P6"}
    assert all(c["nonnative_facts_preserved"] for c in a if c["genuine"])
    for c in a:
        assert len(c["claims"]) == len({id(x) for x in c["claims"]})
        assert all(x["gt"] in (None, *met.STATUSES) for x in c["claims"])


def test_metrics_run_and_values_are_in_valid_ranges():
    cands = gen.generate(N, seed=11)
    test = [c for c in cands if c["split"] == "test"]
    import asyncio

    out = asyncio.run(met.run_split_async(test))
    rows = {r["metric"]: r for r in out["rows"]}
    assert rows, "no metric rows produced"
    for r in out["rows"]:
        v, lo, hi, n = r["value"], r["ci"][0], r["ci"][1], r["n"]
        assert n >= 0
        if math.isnan(v):
            continue
        name = r["metric"]
        if "(pp)" in name:
            assert -100.0 <= v <= 100.0
        elif name.startswith("Latency"):
            assert v >= 0.0
        elif "P3 invariance" in name:
            assert -1.0 <= v <= 1.0
        else:
            assert 0.0 <= v <= 1.0, name
        if not math.isnan(lo):
            assert lo <= hi
    det = next(r for r in out["rows"] if r["metric"].startswith("Determinism"))
    assert det["value"] == 1.0 and det["n"] == len(test)
    auc = [r for r in out["rows"] if r["metric"].startswith("AUROC")]
    assert not auc or 0.0 <= auc[0]["value"] <= 1.0
    assert out["extra"]["errors"] == []
    assert len(out["extra"]["confusion"]["matrix"]) == 6
    assert met.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert met.auroc([0.5], [0.5]) == 0.5
    md = met.render_markdown(out)
    assert "SYNTHETIC-ONLY" in md


def test_metrics_refuse_the_train_split():
    with pytest.raises(AssertionError):
        met.run_split("train")
