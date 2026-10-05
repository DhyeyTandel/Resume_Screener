"""PDF parsing runs in a killable child process (A-28 item 3). No real malicious PDF is used:
the function the child runs is replaced with one that hangs, spins or dies."""
import dataclasses
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from app import config
from app.parsing import loader
from app.parsing.loader import NoTextLayer, UnreadableFile

PDFS = sorted((Path(__file__).parent / "fixtures" / "pdfs").glob("*.pdf"))
CLEAN = (Path(__file__).parent / "fixtures" / "pdfs" / "clean.pdf").read_bytes()


def _outcome(fn, data):
    try:
        return ("ok", dataclasses.asdict(fn(data, "x.pdf")))
    except UnreadableFile as exc:
        return (type(exc).__name__, exc.reason, exc.remediation)


@pytest.mark.parametrize("path", PDFS, ids=lambda p: p.name)
def test_sandboxed_result_equals_in_process_result(path):
    data = path.read_bytes()
    assert _outcome(loader._pdf_sandboxed, data) == _outcome(loader._pdf_inprocess, data)


def test_no_text_layer_keeps_its_type_across_the_process_boundary(monkeypatch):
    monkeypatch.setattr(loader, "_ocr_pdf", lambda data: None)
    data = (PDFS[0].parent / "scanned_no_text.pdf").read_bytes()
    with pytest.raises(NoTextLayer):
        loader.load("scan.pdf", data)


@pytest.mark.parametrize("method", ["spawn", "forkserver"])
def test_parity_with_non_fork_start_methods_and_config_snapshot(monkeypatch, method):
    if method not in multiprocessing.get_all_start_methods():
        pytest.skip("start method unavailable")
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_sandbox_start", method)
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_max_spans", 3)
    # The spawned child must see the parent's effective limit, not the file default.
    with pytest.raises(UnreadableFile, match="far more text"):
        loader._pdf_sandboxed(CLEAN, "x.pdf")
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_max_spans", 200000)
    assert _outcome(loader._pdf_sandboxed, CLEAN) == _outcome(loader._pdf_inprocess, CLEAN)


def test_load_uses_the_sandbox_by_default_and_the_switch_disables_it(monkeypatch):
    calls = []
    monkeypatch.setattr(loader, "_pdf_sandboxed", lambda d, f: calls.append("box") or "S")
    monkeypatch.setattr(loader, "_pdf_inprocess", lambda d, f: calls.append("in") or "I")
    assert loader.load("a.pdf", CLEAN) == "S"
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_sandbox", False)
    assert loader.load("a.pdf", CLEAN) == "I"
    assert calls == ["box", "in"]


# Module-level so a spawned child can import them by name.
def _sleeper(conn, data, filename, ingest, mem, cpu):
    time.sleep(60)


def _spinner(conn, data, filename, ingest, mem, cpu):
    while True:
        pass


def _killed(conn, data, filename, ingest, mem, cpu):
    os.kill(os.getpid(), signal.SIGKILL)


def _silent_exit(conn, data, filename, ingest, mem, cpu):
    os._exit(0)


def _garbage_reply(conn, data, filename, ingest, mem, cpu):
    conn.send_bytes(b"not a pickle")


def _no_children():
    return multiprocessing.active_children() == []


@pytest.mark.parametrize("target", [_sleeper, _spinner])
def test_hang_or_spin_is_killed_and_rejected_quickly(monkeypatch, target):
    monkeypatch.setattr(loader, "_SANDBOX_TARGET", target)
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_timeout_s", 0.5)
    t = time.monotonic()
    with pytest.raises(UnreadableFile) as ei:
        loader.load("a.pdf", CLEAN)
    assert time.monotonic() - t < 3
    assert "longer than" in ei.value.reason and ei.value.remediation
    assert _no_children()


@pytest.mark.parametrize("target", [_killed, _silent_exit, _garbage_reply])
def test_child_crash_surfaces_as_unreadable_file(monkeypatch, target):
    monkeypatch.setattr(loader, "_SANDBOX_TARGET", target)
    t = time.monotonic()
    with pytest.raises(UnreadableFile) as ei:
        loader.load("a.pdf", CLEAN)
    assert time.monotonic() - t < 5  # a dead child is noticed at once, not at the timeout
    assert "stopped unexpectedly" in ei.value.reason and ei.value.remediation
    assert _no_children()


def test_memory_error_in_child_is_a_plain_limit_message(monkeypatch):
    def boom(data, filename):
        raise MemoryError

    monkeypatch.setattr(loader, "_pdf_inprocess", boom)
    with pytest.raises(UnreadableFile, match="more memory"):
        loader.load("a.pdf", CLEAN)
    assert _no_children()


def test_no_zombies_after_many_parses_and_kills(monkeypatch):
    for _ in range(3):
        assert loader.load("a.pdf", CLEAN).visible_text
    monkeypatch.setattr(loader, "_SANDBOX_TARGET", _sleeper)
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_timeout_s", 0.2)
    pids = []
    real_ctx = loader._sandbox_context

    class _Ctx:
        def __getattr__(self, n):
            return getattr(real_ctx(), n)

        def Process(self, *a, **k):
            p = real_ctx().Process(*a, **k)
            orig = p.start
            p.start = lambda: (orig(), pids.append(p.pid))[0]
            return p

    monkeypatch.setattr(loader, "_sandbox_context", lambda: _Ctx())
    for _ in range(2):
        with pytest.raises(UnreadableFile):
            loader.load("a.pdf", CLEAN)
    assert len(pids) == 2 and _no_children()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # reaped: the pid no longer exists, not even as a zombie


def test_memory_limit_helper_sets_rlimit_as_where_supported(monkeypatch):
    import resource

    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, lim: calls.append((which, lim)))
    ok = loader._apply_memory_limit(512 * 1024 * 1024)
    if sys.platform == "darwin":
        assert ok is False and calls == []  # macOS ignores RLIMIT_AS: the timeout is the guard
    else:
        assert ok is True
        which, (soft, hard) = calls[0]
        assert which == resource.RLIMIT_AS and soft == hard >= 512 * 1024 * 1024


def test_memory_limit_helper_is_safe_when_setrlimit_fails(monkeypatch):
    import resource

    def bad(*a):
        raise ValueError("nope")

    monkeypatch.setattr(resource, "setrlimit", bad)
    assert loader._apply_memory_limit(1 << 20) is False
