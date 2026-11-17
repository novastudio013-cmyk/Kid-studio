"""analytics.py - close the feedback loop.

agent.py balances its own output, but balancing is not learning. This reads what actually
happened on YouTube and turns it into a performance score the planner can use.

    python analytics.py --refresh      # pull the last 28 days, cache to analytics.json
    python analytics.py --show         # what the planner currently believes

Why a cache: the planner runs many times a day; the Analytics API is quota-limited and slow.
One fetch, many reads. If the fetch fails the cache is used, and if there is no cache the
planner behaves exactly as it did before - neutral, never broken.

Requires the yt-analytics.readonly scope. If your existing refresh token predates that you
will get a 403; the error tells you exactly how to re-authorise.
"""
import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "analytics.json")
HISTORY = os.path.join(HERE, "history.json")

SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
MIN_VIEWS = 10       # below this a video's retention is noise
PRIOR_RETENTION = 0.55   # what we assume when we know nothing
SHRINK_K = 3         # videos needed before the signal is fully trusted
REF_SPREAD = 0.10    # 10 percentage points of retention above the channel average = full effect


# --------------------------------------------------------------------------- fetch
def _token():
    """Exchange the stored refresh token for an access token."""
    import requests
    need = ["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing secrets: " + ", ".join(missing))
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("token refresh failed (%d): %s" % (r.status_code, r.text[:200]))
    return r.json()["access_token"]


def fetch(days=28):
    """Per-video views and average view percentage over the last `days` days."""
    import requests
    tok = _token()
    end = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400 * days))
    r = requests.get("https://youtubeanalytics.googleapis.com/v2/reports", headers={
        "Authorization": "Bearer " + tok}, params={
            "ids": "channel==MINE",
            "startDate": start, "endDate": end,
            "metrics": "views,averageViewPercentage,averageViewDuration",
            "dimensions": "video",
            "sort": "-views",
            "maxResults": 200}, timeout=60)

    if r.status_code == 403:
        raise RuntimeError(
            "403 - the refresh token lacks the analytics scope.\n"
            "Re-authorise once with this URL, adding the scope below, then update "
            "YT_REFRESH_TOKEN:\n\n"
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=%s"
            "&redirect_uri=urn:ietf:wg:oauth:2.0:oob&response_type=code"
            "&access_type=offline&prompt=consent"
            "&scope=https://www.googleapis.com/auth/youtube.upload+%s\n"
            % (os.environ.get("YT_CLIENT_ID", "<your client id>"), SCOPE))
    if r.status_code != 200:
        raise RuntimeError("analytics HTTP %d: %s" % (r.status_code, r.text[:300]))

    rows = r.json().get("rows", [])
    videos = {}
    for vid, views, ret_pct, dur in rows:
        if views < MIN_VIEWS:
            continue
        videos[vid] = {"views": int(views),
                       "retention": round(float(ret_pct) / 100.0, 4),
                       "avg_view_seconds": round(float(dur), 1)}
    return {"fetched": time.strftime("%Y-%m-%dT%H:%M:%S"), "window_days": days,
            "videos": videos}


def refresh(days=28):
    data = fetch(days)
    json.dump(data, open(CACHE, "w"), indent=2)
    print("cached %d videos with >=%d views" % (len(data["videos"]), MIN_VIEWS))
    return data


# --------------------------------------------------------------------------- join + score
def load_cache():
    if not os.path.exists(CACHE):
        return None
    try:
        return json.load(open(CACHE))
    except Exception:
        return None


def _history():
    if not os.path.exists(HISTORY):
        return []
    try:
        return json.load(open(HISTORY))
    except Exception:
        return []


def join(analytics, history=None):
    """Attach measured performance to each kind. Returns {kind: {...}}."""
    history = _history() if history is None else history
    id_to_kind = {h.get("id"): h.get("kind") for h in history if h.get("id") and h.get("kind")}
    by_kind = {}
    for vid, m in (analytics or {}).get("videos", {}).items():
        kind = id_to_kind.get(vid)
        if not kind:
            continue
        by_kind.setdefault(kind, []).append(m)
    return by_kind


def score(analytics, history=None):
    """Turn measurements into a 0-1 performance score per kind, with confidence.

    Retention is the metric that matters for a kids channel: a video watched to the end is
    what the next video gets recommended after. Views are used only to decide whether a
    retention figure is worth trusting.
    """
    by_kind = join(analytics, history)
    if not by_kind:
        return {}

    # Shrink each kind toward the prior first, THEN normalise. Normalising raw means let a
    # single lucky video stretch the scale and make everything else look bad.
    means = {k: statistics.mean(v["retention"] for v in vs) for k, vs in by_kind.items()}
    trusts = {k: len(vs) / (len(vs) + SHRINK_K) for k, vs in by_kind.items()}
    blended = {k: trusts[k] * means[k] + (1 - trusts[k]) * PRIOR_RETENTION for k in means}
    # Compare against the CHANNEL BASELINE, not against min/max. Min-max normalisation
    # forces a full swing even when two kinds differ by 2 percentage points, which let one
    # lucky video outrank eight good ones. An absolute effect vs a fixed reference spread
    # degrades gracefully: small real differences produce small nudges.
    center = statistics.mean(blended.values())

    out = {}
    for kind, samples in by_kind.items():
        n = len(samples)
        views = sum(s["views"] for s in samples)
        trust = trusts[kind]
        observed = means[kind]
        b = blended[kind]
        effect = max(-1.0, min(1.0, (b - center) / REF_SPREAD))
        perf = 0.5 + effect / 2.0          # 0.5 = channel average
        out[kind] = {"n": n, "views": views,
                     "retention": round(observed, 4),
                     "blended": round(b, 4),
                     "perf": round(perf, 4),
                     "effect": round(effect, 4),
                     "confidence": round(trust, 3)}
    return out


def weight(analytics, history=None, max_boost=1.5):
    """The multiplier the planner adds per kind. Neutral (0) when there is no data."""
    s = score(analytics, history)
    if not s:
        return {}
    return {k: round(max_boost * v["effect"] * v["confidence"], 3)
            for k, v in s.items()}


# --------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="fetch from the YouTube API")
    ap.add_argument("--show", action="store_true", help="show what the planner believes")
    ap.add_argument("--days", type=int, default=28)
    a = ap.parse_args()

    if a.refresh:
        try:
            refresh(a.days)
        except Exception as e:
            print("refresh failed:", e)
            return 1
    if a.show or not a.refresh:
        cache = load_cache()
        if not cache:
            print("no analytics cache - the planner will treat every kind as equal.\n"
                  "Run: python analytics.py --refresh")
            return 0
        print("fetched %s, %d days, %d videos"
              % (cache["fetched"], cache["window_days"], len(cache["videos"])))
        s = score(cache)
        w = weight(cache)
        if not s:
            print("\nno analytics rows matched a published video in history.json yet.")
            return 0
        print("\n%-14s %4s %8s %10s %6s %8s" %
              ("kind", "n", "views", "retention", "trust", "bonus"))
        for kind in sorted(s, key=lambda k: -s[k]["perf"]):
            v = s[kind]
            print("%-14s %4d %8d %9.1f%% %6.2f %+8.2f"
                  % (kind, v["n"], v["views"], v["retention"] * 100,
                     v["confidence"], w.get(kind, 0)))
        best = max(s, key=lambda k: s[k]["perf"])
        print("\nbest: %s (retention %.1f%%, %d videos)"
              % (best, s[best]["retention"] * 100, s[best]["n"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
