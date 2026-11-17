"""agent.py - the decision layer.

kids.py is a pipeline: give it a kind and a format, it makes a video. This is the agent that
decides *what* to make and *whether* to make anything at all.

    python agent.py --explain            # show the reasoning, build nothing
    python agent.py --run                # decide, then build and upload
    python agent.py --run --dry-run      # decide and build, never upload

The loop:

    observe  -> read history, past outcomes, channel signal
    plan     -> choose kind / format / languages / quality, or decide to skip
    act      -> run kids.py
    verify   -> read what actually came out
    reflect  -> update memory so the next decision is better

Everything here is free: no LLM required. If GEMINI_API_KEY is set the planner explains itself
in prose; otherwise it uses the deterministic scorer below, which is the default.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "agent_state.json")
HISTORY = os.path.join(HERE, "history.json")
KIDS = os.path.join(HERE, "kids.py")

try:
    import analytics as AN
except Exception:          # analytics is optional - the agent must run without it
    AN = None

DEFAULTS = {
    "max_per_day": 3,          # hard governor: never exceed this
    "min_gap_hours": 6,        # never run twice within this window
    "max_failures_per_day": 2,  # stop trying after this many failed builds
    "explore_rate": 0.15,      # chance of ignoring the scorer and trying something new
    "languages": ["en"],
    "quality": "high",
    "kinds": ["counting", "colors", "guess_animal", "story"],
    "long_on_weekend": True,
}

KIND_AFFINITY = {
    # how well a kind suits a language, where it is not obvious
    "guess_animal": {"en": 1.0},
}


# --------------------------------------------------------------------------- state
def blank_state():
    return {
        "runs": [],          # [{date, kind, format, langs, ok, seconds, titles}]
        "stats": {},         # kind -> {n, ok, fail, avg_seconds, last_used}
        "failures_today": 0,
        "day": "",
        "notes": [],
    }


def load_state():
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
            for k, v in blank_state().items():
                st.setdefault(k, v)
            return st
        except Exception as e:
            print("agent_state.json unreadable, starting fresh:", e)
    return blank_state()


def save_state(st):
    json.dump(st, open(STATE, "w"), indent=2, ensure_ascii=False)


def load_history():
    if os.path.exists(HISTORY):
        try:
            return json.load(open(HISTORY))
        except Exception:
            return []
    return []


# --------------------------------------------------------------------------- observe
def observe(state, history, cfg):
    """Collect the facts the decision depends on."""
    today = time.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["failures_today"] = 0

    runs_today = [r for r in state["runs"] if r.get("date") == today]
    last_run = max((r.get("ts", 0) for r in state["runs"]), default=0)
    hours_since = (time.time() - last_run) / 3600 if last_run else 999

    # Count what we actually built, not only what uploaded successfully: a failed upload
    # must not make the agent think a kind is still unexplored and repeat it forever.
    per_kind = Counter(h.get("kind") for h in history if h.get("kind"))
    per_kind.update(r.get("kind") for r in state["runs"]
                    if r.get("kind") and r.get("verdict") == "ok")
    published = len(history)
    fails_today = int(state.get("failures_today", 0))

    perf = {}
    if AN is not None:
        try:
            cache = AN.load_cache()
            if cache:
                perf = AN.weight(cache, history)
        except Exception as e:
            print("  (analytics ignored: %s)" % e)

    weekend = time.localtime().tm_wday in (5, 6)
    want_format = "long" if (cfg.get("long_on_weekend") and weekend) else "short"

    return {
        "today": today,
        "runs_today": len(runs_today),
        "hours_since_last": hours_since,
        "fails_today": fails_today,
        "published": published,
        "per_kind": dict(per_kind),
        "want_format": want_format,
        "weekend": weekend,
        "perf": perf,
    }


def guard(obs, cfg):
    """Hard stops. An agent that cannot stop is a liability, not an agent."""
    if obs["runs_today"] >= cfg["max_per_day"]:
        return "daily limit reached (%d/%d)" % (obs["runs_today"], cfg["max_per_day"])
    if obs["hours_since_last"] < cfg["min_gap_hours"]:
        return ("only %.1fh since the last run (min %dh)"
                % (obs["hours_since_last"], cfg["min_gap_hours"]))
    if obs["fails_today"] >= cfg["max_failures_per_day"]:
        return ("%d failed builds today - pausing until tomorrow. Something is probably "
                "broken; check the last artifact." % obs["fails_today"])
    return None


# --------------------------------------------------------------------------- plan
def score_kinds(obs, state, cfg):
    """Rank the content kinds. Deterministic, explainable, no API needed."""
    scores = {}
    total = max(1, obs["published"])
    for kind in cfg["kinds"]:
        s = state["stats"].get(kind, {})
        n = s.get("n", 0)
        ok = s.get("ok", 0)
        fail = s.get("fail", 0)
        used = obs["per_kind"].get(kind, 0)

        # 1. coverage: favour kinds we have made fewer of
        coverage = 1.0 - (used / total) if total else 1.0
        # 2. reliability: penalise kinds that have been failing
        reliability = 1.0 if (ok + fail) == 0 else ok / (ok + fail)
        # 3. recency: strongly avoid whatever we just did
        last_used = s.get("last_used", 0)
        recency = 0.0 if (time.time() - last_used) < 6 * 3600 else 1.0
        # 4. affinity: some kinds carry better in some languages
        aff = KIND_AFFINITY.get(kind, {}).get("en", 1.0)

        # 5. measured: what actually held attention on YouTube (0 when no data)
        measured = float(obs.get("perf", {}).get(kind, 0.0))

        scores[kind] = round(
            (2.0 * coverage) + (1.5 * reliability) + (1.0 * recency) + (0.3 * aff)
            + measured + random.random() * 0.15, 4)
    return dict(sorted(scores.items(), key=lambda kv: -kv[1]))


def plan(obs, state, cfg, history):
    """Decide what to build. Returns a decision dict with its reasoning."""
    scores = score_kinds(obs, state, cfg)
    ranked = list(scores.items())

    explore = random.random() < float(cfg.get("explore_rate", 0.15))
    if explore:
        kind = random.choice(cfg["kinds"])
        why = "exploring (deliberately ignoring the ranking)"
    else:
        kind = ranked[0][0]
        why = "highest score: %s" % ", ".join("%s=%.2f" % kv for kv in ranked[:3])

    # languages: rotate the one we have published least
    langs = list(cfg.get("languages") or ["en"])
    if len(langs) > 1:
        counts = Counter(h.get("lang", "en") for h in history)
        langs.sort(key=lambda lg: counts.get(lg, 0))
        reason_lang = "least published language first: %s" % langs[0]
    else:
        reason_lang = "single language configured"

    titles = {h.get("title", "") for h in history}
    return {
        "kind": kind,
        "format": obs["want_format"],
        "languages": langs,
        "quality": cfg.get("quality", "high"),
        "titles_already_used": len(titles),
        "reason": why,
        "reason_language": reason_lang,
        "scores": scores,
        "explore": explore,
    }


def ask_llm(decision, obs, cfg):
    """Optional: let a model review the choice. Never required, never blocking."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        import requests
        prompt = ("You run a preschool YouTube channel. Facts: %s. The scheduler chose kind=%s, "
                  "format=%s, languages=%s. In ONE short sentence, say whether that is sensible "
                  "and why. Reply with plain text only."
                  % (json.dumps(obs), decision["kind"], decision["format"],
                     ",".join(decision["languages"])))
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": key},
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()[:300]
    except Exception as e:
        print("  (llm commentary skipped: %s)" % e)
    return None


# --------------------------------------------------------------------------- act
def act(decision, dry_run, proxy=False):
    """Run the pipeline. Returns (ok, output_paths, seconds)."""
    cmd = [sys.executable, KIDS, "--format", decision["format"],
           "--kind", decision["kind"], "--quality", decision["quality"],
           "--languages", ",".join(decision["languages"])]
    cmd.append("--dry-run" if dry_run else "--upload")
    if proxy:
        cmd.append("--no-blender")     # flat placeholder art: tests the plumbing only
    print("  $ " + " ".join(cmd[1:]))
    t = time.time()
    r = subprocess.run(cmd, cwd=HERE, text=True, capture_output=True)
    secs = time.time() - t
    ok = r.returncode == 0
    if not ok:
        tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-800:]
        print("  build failed:\n" + tail)
    else:
        for line in (r.stdout or "").splitlines()[-8:]:
            print("  | " + line)
    return ok, secs, r


# --------------------------------------------------------------------------- verify
def verify(ok, secs, proc):
    """Decide what the outcome means. Distinct from "did it crash"."""
    if not ok:
        out = (proc.stdout or "") + (proc.stderr or "")
        if "STOPPED: quality check failed" in out:
            return "quality_rejected"
        if "Blender not found" in out or "ffmpeg" in out:
            return "environment"
        return "crash"
    return "ok"


# --------------------------------------------------------------------------- reflect
def reflect(state, decision, verdict, secs, history):
    k = decision["kind"]
    s = state["stats"].setdefault(k, {"n": 0, "ok": 0, "fail": 0, "avg_seconds": 0, "last_used": 0})
    s["n"] += 1
    s["last_used"] = time.time()
    if verdict == "ok":
        s["ok"] += 1
    else:
        s["fail"] += 1
        state["failures_today"] = int(state.get("failures_today", 0)) + 1
    s["avg_seconds"] = round((s["avg_seconds"] * (s["n"] - 1) + secs) / s["n"], 1)

    state["runs"].append({
        "ts": time.time(),
        "date": time.strftime("%Y-%m-%d"),
        "kind": k,
        "format": decision["format"],
        "langs": decision["languages"],
        "verdict": verdict,
        "seconds": round(secs, 1),
    })
    state["runs"] = state["runs"][-200:]

    if verdict != "ok" and state["failures_today"] >= 2:
        state["notes"].append(
            "%s failed %d times today - pausing. Look at the artifact."
            % (time.strftime("%Y-%m-%d"), state["failures_today"]))
        state["notes"] = state["notes"][-20:]
    save_state(state)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually build")
    ap.add_argument("--explain", action="store_true", help="decide and print reasoning only")
    ap.add_argument("--dry-run", action="store_true", help="build but never upload")
    ap.add_argument("--force", action="store_true", help="ignore the governor")
    ap.add_argument("--language", help="override languages")
    ap.add_argument("--proxy", action="store_true", help="skip Blender, use placeholder art")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    cpath = os.path.join(HERE, "config.json")
    if os.path.exists(cpath):
        try:
            user = json.load(open(cpath))
            for k in list(DEFAULTS):
                if k in user:
                    cfg[k] = user[k]
        except Exception:
            pass
    if a.language:
        cfg["languages"] = [x.strip() for x in a.language.split(",") if x.strip()]

    state = load_state()
    history = load_history()
    obs = observe(state, history, cfg)

    print("=== observe ===")
    print("  published: %d   today: %d/%d   last run: %.1fh ago   failures today: %d"
          % (obs["published"], obs["runs_today"], cfg["max_per_day"],
             obs["hours_since_last"], obs["fails_today"]))
    print("  per kind: %s" % (obs["per_kind"] or "nothing yet"))

    stop = guard(obs, cfg)
    if stop and not a.force:
        print("\n=== decision: SKIP ===")
        print("  reason: " + stop)
        return 0

    decision = plan(obs, state, cfg, history)
    print("\n=== plan ===")
    print("  kind: %s   format: %s   languages: %s   quality: %s"
          % (decision["kind"], decision["format"],
             ",".join(decision["languages"]), decision["quality"]))
    print("  why: " + decision["reason"])
    print("  language: " + decision["reason_language"])
    print("  scores: " + ", ".join("%s=%.2f" % kv for kv in list(decision["scores"].items())[:4]))
    if obs.get("perf"):
        print("  measured: " + ", ".join("%s=%+.2f" % (k, v)
                                         for k, v in sorted(obs["perf"].items(),
                                                            key=lambda kv: -kv[1])))
    else:
        print("  measured: no analytics yet - all kinds treated as equal")

    note = ask_llm(decision, obs, cfg) if os.environ.get("GEMINI_API_KEY") else None
    if note:
        print("  review: " + note)

    if a.explain or not a.run:
        print("\n(nothing built - pass --run to execute)")
        return 0

    print("\n=== act ===")
    ok, secs, proc = act(decision, a.dry_run, a.proxy)
    verdict = verify(ok, secs, proc)
    print("\n=== verify ===\n  %s in %.0fs" % (verdict, secs))

    reflect(state, decision, verdict, secs, history)
    print("\n=== reflect ===")
    print("  stats: " + json.dumps(state["stats"].get(decision["kind"]), ensure_ascii=False))
    if state["notes"]:
        print("  notes: " + state["notes"][-1])
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
