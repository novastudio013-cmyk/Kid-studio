"""Tests for the decision layer. Run: python -m pytest studio/ -q"""
import importlib.util
import json
import os
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("agent", os.path.join(HERE, "agent.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A = load()
CFG = dict(A.DEFAULTS)


def run(monkeypatch, state, history, cfg=None):
    monkeypatch.setattr(A, "load_state", lambda: state)
    monkeypatch.setattr(A, "save_state", lambda s: None)
    monkeypatch.setattr(A, "load_history", lambda: history)
    monkeypatch.setattr(A, "ask_llm", lambda *a, **k: None)
    cfg = cfg or CFG
    obs = A.observe(state, history, cfg)
    return obs, A.guard(obs, cfg), (A.plan(obs, state, cfg, history) if not A.guard(obs, cfg) else None)


def fresh():
    return dict(A.blank_state())


# --------------------------------------------------------------------------- observe
def test_observe_counts_today(monkeypatch):
    today = time.strftime("%Y-%m-%d")
    st = fresh()
    st["runs"] = [{"ts": time.time() - 3600, "date": today, "kind": "story", "verdict": "ok"}]
    obs = A.observe(st, [], CFG)
    assert obs["runs_today"] == 1
    assert obs["hours_since_last"] < 2


def test_day_rollover_clears_failures():
    st = fresh()
    st["day"] = "2026-01-01"
    st["failures_today"] = 5
    A.observe(st, [], CFG)
    assert st["failures_today"] == 0


def test_weekend_asks_for_long():
    st = fresh()
    import unittest.mock as M
    with M.patch("time.localtime", lambda: time.struct_time((2026, 11, 15, 10, 0, 0, 6, 319, 0))):
        obs = A.observe(st, [], CFG)
    assert obs["weekend"] is True
    assert obs["want_format"] == "long"


# --------------------------------------------------------------------------- guard
@pytest.mark.parametrize("state,expect", [
    ({"runs": [{"ts": time.time() - 3600, "date": "2026-11-14"}], "day": "2026-11-14"}, "min_gap"),
    ({"runs": [{"ts": time.time() - 7200, "date": time.strftime("%Y-%m-%d")}] * 3,
      "day": time.strftime("%Y-%m-%d")}, "daily"),
    ({"runs": [], "day": time.strftime("%Y-%m-%d"), "failures_today": 2}, "failures"),
    ({"runs": [], "day": time.strftime("%Y-%m-%d"), "failures_today": 0}, None),
])
def test_guard(monkeypatch, state, expect):
    st = fresh()
    st.update(state)
    obs, stop, _ = run(monkeypatch, st, [])
    if expect is None:
        assert stop is None
    else:
        assert stop is not None
        assert {"min_gap": "since the last run", "daily": "daily limit",
                "failures": "failed builds"}[expect] in stop


def test_guard_stops_the_agent_looping(monkeypatch):
    """An agent that cannot stop is a liability."""
    st = fresh()
    t = time.strftime("%Y-%m-%d")
    st["runs"] = [{"ts": time.time() - 60 * 60 * 24 * i, "date": t} for i in range(1, 4)]
    st["day"] = t
    obs, stop, _ = run(monkeypatch, st, [])
    assert stop and "daily limit" in stop


# --------------------------------------------------------------------------- plan
def test_first_run_picks_something(monkeypatch):
    obs, stop, dec = run(monkeypatch, fresh(), [])
    assert stop is None
    assert dec["kind"] in CFG["kinds"]


def test_agent_avoids_overused_kind(monkeypatch):
    """The key behaviour: 12 counting videos should push it to something else."""
    hist = [{"title": "c%d" % i, "kind": "counting"} for i in range(12)]
    st = fresh()
    st["stats"]["counting"] = {"n": 12, "ok": 12, "fail": 0,
                               "avg_seconds": 90, "last_used": time.time() - 60}
    monkeypatch.setattr(A.random, "random", lambda: 1.0)      # never explore
    obs, stop, dec = run(monkeypatch, st, hist)
    assert dec["kind"] != "counting"
    assert dec["scores"]["counting"] < dec["scores"][dec["kind"]]


def test_agent_backs_off_a_failing_kind(monkeypatch):
    st = fresh()
    st["stats"]["story"] = {"n": 6, "ok": 1, "fail": 5, "avg_seconds": 90,
                            "last_used": time.time() - 60}
    obs = A.observe(st, [], CFG)
    scores = A.score_kinds(obs, st, CFG)
    assert scores["story"] == min(scores.values())


def test_recent_kind_is_penalised(monkeypatch):
    st = fresh()
    st["stats"]["colors"] = {"n": 1, "ok": 1, "fail": 0, "avg_seconds": 90,
                             "last_used": time.time() - 60}
    obs = A.observe(st, [], CFG)
    scores = A.score_kinds(obs, st, CFG)
    assert scores["colors"] < max(scores.values())


def test_explore_rate_can_be_forced(monkeypatch):
    monkeypatch.setattr(A.random, "random", lambda: 0.0)      # always < explore_rate
    obs, stop, dec = run(monkeypatch, fresh(), [])
    assert dec["explore"] is True


def test_languages_rotate_towards_least_published(monkeypatch):
    cfg = dict(CFG); cfg["languages"] = ["en", "es", "pt"]
    hist = [{"title": "a", "kind": "story", "lang": "en"},
            {"title": "b", "kind": "story", "lang": "en"},
            {"title": "c", "kind": "story", "lang": "es"}]
    obs, stop, dec = run(monkeypatch, fresh(), hist, cfg)
    assert dec["languages"][0] == "pt"


# --------------------------------------------------------------------------- verify + reflect
def _proc(out="", err=""):
    return type("P", (), {"stdout": out, "stderr": err})()


def test_verify_classifies_outcomes():
    assert A.verify(True, 10, _proc()) == "ok"
    assert A.verify(False, 10, _proc("STOPPED: quality check failed: audio silent")) == "quality_rejected"
    assert A.verify(False, 10, _proc("Blender not found.")) == "environment"
    assert A.verify(False, 10, _proc("Traceback ... ValueError")) == "crash"


def test_reflect_updates_stats_and_counts_failures(monkeypatch):
    st = fresh()
    monkeypatch.setattr(A, "save_state", lambda s: None)
    dec = {"kind": "story", "format": "short", "languages": ["en"]}
    A.reflect(st, dec, "ok", 100, [])
    assert st["stats"]["story"]["ok"] == 1
    A.reflect(st, dec, "crash", 5, [])
    assert st["stats"]["story"]["fail"] == 1
    assert st["failures_today"] == 1
    assert st["stats"]["story"]["avg_seconds"] > 0


def test_two_failures_pause_the_agent(monkeypatch):
    st = fresh()
    monkeypatch.setattr(A, "save_state", lambda s: None)
    dec = {"kind": "story", "format": "short", "languages": ["en"]}
    for _ in range(2):
        A.reflect(st, dec, "crash", 5, [])
    assert st["failures_today"] == 2
    assert A.guard(A.observe(st, [], CFG), CFG)


def test_state_survives_a_bad_json(tmp_path, monkeypatch):
    bad = tmp_path / "agent_state.json"
    bad.write_text("{not json")
    monkeypatch.setattr(A, "STATE", str(bad))
    st = A.load_state()
    assert st["runs"] == [] and st["stats"] == {}
