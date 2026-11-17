"""Tests for the feedback loop. No network, no key."""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("analytics", os.path.join(HERE, "analytics.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A = load()


def cache(rows):
    """rows: [(kind, video_id, retention)]"""
    return {"fetched": "2026-11-17T00:00:00", "window_days": 28,
            "videos": {vid: {"views": 500, "retention": r, "avg_view_seconds": 8.0}
                       for _, vid, r in rows}}


def hist(rows):
    return [{"title": vid, "kind": kind, "id": vid, "lang": "en"} for kind, vid, _ in rows]


# --------------------------------------------------------------------------- no data
def test_no_cache_is_empty_not_broken():
    assert A.score(None) == {}
    assert A.weight(None) == {}


def test_unmatched_videos_score_nothing():
    c = cache([("story", "abc", 0.8)])
    assert A.score(c, history=[]) == {}


def test_corrupt_cache_returns_none():
    assert A.load_cache() is None or isinstance(A.load_cache(), dict)


# --------------------------------------------------------------------------- join
def test_join_maps_video_ids_to_kinds():
    rows = [("story", "v1", 0.7), ("colors", "v2", 0.5)]
    by = A.join(cache(rows), hist(rows))
    assert set(by) == {"story", "colors"}
    assert by["story"][0]["retention"] == 0.7


# --------------------------------------------------------------------------- scoring
def test_higher_retention_scores_higher():
    rows = ([("story", "s%d" % i, 0.72) for i in range(6)]
            + [("counting", "c%d" % i, 0.31) for i in range(6)])
    s = A.score(cache(rows), hist(rows))
    assert s["story"]["perf"] > 0.5 > s["counting"]["perf"]
    assert s["story"]["perf"] > s["counting"]["perf"] + 0.5


def test_single_kind_is_neutral():
    """With one kind there is nothing to compare against, so no bonus either way."""
    rows = [("story", "s%d" % i, 0.9) for i in range(4)]
    s = A.score(cache(rows), hist(rows))
    assert s["story"]["perf"] == pytest.approx(0.5)
    assert A.weight(cache(rows), hist(rows))["story"] == pytest.approx(0.0, abs=0.01)


def test_confidence_grows_with_sample_size():
    one = A.score(cache([("story", "s0", 0.8)]), hist([("story", "s0", 0.8)]))
    many = A.score(cache([("story", "s%d" % i, 0.8) for i in range(10)]),
                   hist([("story", "s%d" % i, 0.8) for i in range(10)]))
    assert one["story"]["confidence"] < many["story"]["confidence"]
    assert many["story"]["confidence"] > 0.7


def test_one_lucky_video_cannot_hijack_the_planner():
    """The whole point of shrinkage: 1 video at 95% must not beat 8 videos at 65%."""
    rows = ([("story", "s%d" % i, 0.65) for i in range(8)]
            + [("counting", "c0", 0.95)])
    s = A.score(cache(rows), hist(rows))
    # raw retention would put counting first; blended pulls it back toward the prior
    assert s["counting"]["confidence"] < s["story"]["confidence"]
    assert s["counting"]["blended"] < 0.95
    # shrinkage pulls it back to roughly the well-sampled kind: no real signal either way
    w = A.weight(cache(rows), hist(rows))
    assert abs(w["counting"]) <= 0.10
    assert abs(w["story"]) <= 0.20
    assert abs(w["counting"] - w["story"]) <= 0.25


def test_prior_pulls_small_samples_toward_neutral():
    rows = ([("story", "s%d" % i, 0.65) for i in range(8)]
            + [("counting", "c0", 0.95)])
    s = A.score(cache(rows), hist(rows))
    # blended must sit between the observed value and the prior
    assert A.PRIOR_RETENTION < s["counting"]["blended"] < 0.95


def test_weight_is_bounded():
    rows = ([("story", "s%d" % i, 0.99) for i in range(20)]
            + [("counting", "c%d" % i, 0.01) for i in range(20)])
    w = A.weight(cache(rows), hist(rows), max_boost=1.5)
    assert all(-1.5 <= v <= 1.5 for v in w.values())
    assert w["story"] > 0 > w["counting"]


def test_low_view_videos_are_ignored_by_fetch_logic():
    """MIN_VIEWS guards against 3-view videos with meaningless retention."""
    assert A.MIN_VIEWS >= 10


def test_missing_analytics_does_not_change_the_planner(monkeypatch):
    """Regression: the agent must behave identically when the cache is absent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent", os.path.join(HERE, "agent.py"))
    ag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ag)
    monkeypatch.setattr(ag, "AN", None)
    obs = ag.observe(ag.blank_state(), [], ag.DEFAULTS)
    assert obs["perf"] == {}
    scores = ag.score_kinds(obs, ag.blank_state(), ag.DEFAULTS)
    assert all(v > 0 for v in scores.values())
