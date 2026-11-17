# The agent

`kids.py` is a pipeline: give it a kind and a format, it makes a video.
`agent.py` is the decision layer: it works out **what to make, whether to make anything at
all, and what to do when a build fails.**

```bash
python agent.py --explain          # show the reasoning, build nothing  (safe, ~0.1s)
python agent.py --run --dry-run    # decide + build, never upload
python agent.py --run              # decide + build + publish
python agent.py --run --force      # ignore the governor (manual override)
```

In GitHub Actions it is now the entry point. The schedule calls `agent.py --run`; naming a
`kind` in a manual dispatch bypasses it and builds exactly what you asked for.

---

## The loop

```
observe  →  read history, past outcomes, today's activity
guard    →  HARD STOPS. decide not to run
plan     →  score the content kinds, pick one, pick languages
act      →  run kids.py as a subprocess
verify   →  classify what happened (not just "did it crash")
reflect  →  update memory so tomorrow's decision is better
```

## Observe

Collects: how many videos published, how many today, hours since the last run, failures
today, how many of each kind exist, and whether it is the weekend (→ long format).

## Guard — the part that matters most

An agent that cannot stop is a liability, not an agent. It refuses to run when:

| Condition | Why |
|---|---|
| Daily limit reached (3) | a crash loop must not burn your quota or upload 40 videos |
| Less than 6 h since the last run | stops retry storms |
| 2 failed builds today | something is genuinely broken; a human should look |
| (state file corrupt) | starts fresh instead of crashing |

Verified in tests: all four paths behave.

## Plan — how it scores

Four signals, each explainable:

| Signal | Weight | Meaning |
|---|---|---|
| **Coverage** | 2.0 | favour kinds you have fewer of |
| **Reliability** | 1.5 | penalise kinds that have been failing |
| **Recency** | 1.0 | strongly avoid whatever you just made |
| **Affinity** | 0.3 | per-language suitability |

Plus **exploration**: with probability `explore_rate` (default 0.15) it ignores the ranking and
tries something random. Without this the agent converges on one kind forever.

Worked example — a channel that has published 12 counting videos and nothing else:

```
scores: story=4.91, guess_animal=4.88, colors=4.85, counting=1.94
```

`counting` gets crushed by coverage and recency, and the agent switches. That is the single
behaviour that stops the channel becoming 200 near-identical videos.

**Languages** rotate towards whichever has been published least, so `en,es,pt` stay balanced
instead of piling up in English.

## Act, verify, reflect

`act` shells out to `kids.py` — the pipeline stays independently runnable, and a pipeline crash
cannot corrupt agent state.

`verify` classifies the outcome into four values rather than pass/fail:

| Verdict | Meaning | Agent response |
|---|---|---|
| `ok` | built and passed QA | record success |
| `quality_rejected` | built, but the QA gate refused it | count as failure, retry another kind later |
| `environment` | Blender/ffmpeg missing | count as failure, stop after 2 |
| `crash` | unexpected exception | count as failure, stop after 2 |

`reflect` writes `agent_state.json` (committed by CI): per-kind counts, success rate, average
build seconds, last used, and plain-language notes when it pauses.

---

## Learning from what actually happened

`agent.py` balances its own output. **Balancing is not learning.** `analytics.py` reads real
retention from YouTube so the planner can prefer what children actually finish watching.

```bash
python analytics.py --refresh     # pull 28 days, cache to analytics.json
python analytics.py --show        # what the planner currently believes
```

```
kind              n    views  retention  trust    bonus
story             6     5550      72.0%   0.67    +0.72
colors            6     5550      55.0%   0.67    +0.17
guess_animal      6     5550      49.0%   0.67    -0.02
counting          6     5550      31.0%   0.67    -0.61
```

Retention is the metric, not views: for a kids channel, a video watched to the end is what
the next one gets recommended after.

### Three ways this could have gone wrong, and did

1. **One lucky video hijacking the planner.** A single 95%-retention video outranked eight
   honest 65% ones. Fixed with shrinkage — each kind is pulled toward the channel prior in
   proportion to how few videos back it up.
2. **Min-max normalisation.** Ranking best-vs-worst forces a full swing even when the real
   difference is 2 percentage points. Replaced with an absolute effect measured against a
   fixed reference spread (10 points = full effect), so small differences produce small
   nudges.
3. **Silent breakage when YouTube is unreachable.** No cache means the planner behaves
   exactly as before — neutral, never broken. Verified by test.

A cache, not a live call: the planner may run several times a day, and the Analytics API is
quota-limited. One fetch, many reads.

### The scope catch

The Analytics API needs `yt-analytics.readonly`, which your upload token may not have. If you
get a 403, the error prints the exact re-authorisation URL with both scopes.

## Is an LLM needed?

**No.** The scorer above is deterministic and free, and it is the default. If `GEMINI_API_KEY`
is set, `ask_llm()` adds one sentence of commentary to the log — useful for reading the
agent's reasoning over time, never required, and a failure there is ignored.

That is deliberate: an agent whose decisions depend on a paid API is an agent that stops when
the bill or the quota does.

---

## Watching it work

Each run prints its reasoning, so the log is the audit trail:

```
=== observe ===
  published: 12   today: 1/3   last run: 9.4h ago   failures today: 0
  per kind: {'counting': 12}
=== plan ===
  kind: story   format: short   languages: en,es,pt   quality: high
  why: highest score: story=4.91, guess_animal=4.88, colors=4.85
  language: least published language first: pt
=== act ===
  $ kids.py --format short --kind story --quality high --languages pt,en,es --upload
=== verify ===
  ok in 412s
=== reflect ===
  stats: {"n": 3, "ok": 3, "fail": 0, "avg_seconds": 408.1, ...}
```

## What it still does not do

- **No self-repair beyond retry.** It will not patch its own code, and it should not.
- **No topic invention.** Everything it picks from is in the grammar or written by Gemini.
- **No self-repair beyond retry.** It will not patch its own code, and it should not.
- **No topic invention.** Everything it picks from is in the grammar or written by Gemini.
