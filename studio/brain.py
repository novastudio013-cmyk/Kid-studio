"""brain.py - a model-agnostic agent kernel.

The intelligence lives in two places: the model (rented, free tiers exist) and the harness
(this file: the loop, the tools, the guardrails, the memory). The harness is the part that
turns "chatbot" into "agent", and it is the part you can own outright.

    python brain.py "make next week's video" --dry-run
    python brain.py "why did yesterday's build fail?" --provider gemini
    python brain.py --self-test              # runs offline, no key, no network

Free providers, all with a free tier that does not need a card:
    gemini     GEMINI_API_KEY      aistudio.google.com  (best free tool-calling)
    groq       GROQ_API_KEY        console.groq.com     (fastest)
    openrouter OPENROUTER_API_KEY  openrouter.ai        (models ending in :free)
    github     GITHUB_TOKEN        models.github.ai     (free with any GitHub account)
    ollama     (no key)            local only - needs a machine with RAM
    mock       (no key)            deterministic, for tests

Nothing here is locked to one vendor. Swap --provider and it keeps working.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRATCH = os.path.join(HERE, "scratch")
MEMORY = os.path.join(HERE, "brain_memory.jsonl")
TRANSCRIPT = os.path.join(HERE, "scratch", "transcript.md")

MAX_STEPS = 12
MAX_TOOL_CALLS = 20
MAX_TOKENS_EST = 120_000


# =========================================================================== tools
# Every tool is a plain function plus a JSON schema. The model sees only the schema.
# Guardrails live here, not in the prompt: a prompt is a suggestion, code is a wall.

def _safe_path(p, allow_write=False):
    """Resolve a path and refuse anything outside the sandbox."""
    if not isinstance(p, str) or not p:
        raise ValueError("path must be a non-empty string")
    if p.startswith("/") or ".." in p.split("/"):
        raise ValueError("absolute paths and '..' are not allowed: %r" % p)
    full = os.path.realpath(os.path.join(REPO, p))
    if not full.startswith(REPO + os.sep):
        raise ValueError("path escapes the repo: %r" % p)
    if allow_write:
        ok = (full.startswith(SCRATCH + os.sep) or full.endswith(".json")
              or full.endswith(".md"))
        if not ok:
            raise ValueError("writes are only allowed into studio/scratch/, *.json or *.md")
    return full


def t_list_history(path="studio/history.json"):
    """What has already been published."""
    try:
        h = json.load(open(_safe_path(path)))
    except Exception as e:
        return {"error": str(e), "published": 0, "items": []}
    return {"published": len(h),
            "items": [{"title": x.get("title"), "kind": x.get("kind"),
                       "lang": x.get("lang", "en"), "date": x.get("date")}
                      for x in h][-15:]}


def t_read_file(path, max_chars=4000):
    """Read a text file from the repo."""
    try:
        p = _safe_path(path)
        if os.path.getsize(p) > 400_000:
            return {"error": "file too large"}
        return {"path": path, "content": open(p, encoding="utf-8", errors="replace").read()[:max_chars]}
    except Exception as e:
        return {"error": str(e)}


def t_write_file(path, content):
    """Write a note or report. Restricted to studio/scratch/ and .json/.md files."""
    try:
        p = _safe_path(path, allow_write=True)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(str(content))
        return {"ok": True, "path": path, "bytes": len(str(content))}
    except Exception as e:
        return {"error": str(e)}


def t_run_tests():
    """Run the test suite. Never builds a video."""
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "studio/", "-q"],
                           cwd=REPO, capture_output=True, text=True, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(\d+) passed", out)
        f = re.search(r"(\d+) failed", out)
        return {"exit_code": r.returncode,
                "passed": int(m.group(1)) if m else 0,
                "failed": int(f.group(1)) if f else 0,
                "tail": out[-800:]}
    except Exception as e:
        return {"error": str(e)}


def t_build_video(kind="story", fmt="short", languages="en", dry_run=True):
    """Build a video through the existing pipeline. dry_run=true never uploads."""
    allowed_kinds = {"counting", "colors", "guess_animal", "story"}
    if kind not in allowed_kinds:
        return {"error": "kind must be one of %s" % sorted(allowed_kinds)}
    if fmt not in ("short", "long"):
        return {"error": "format must be short or long"}
    langs = ",".join(x for x in re.split(r"[,\s]+", str(languages)) if x)
    if not all(re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", x) for x in langs.split(",")):
        return {"error": "bad language code"}
    cmd = [sys.executable, "kids.py", "--kind", kind, "--format", fmt,
           "--languages", langs, "--no-blender"]
    cmd += ["--dry-run"] if dry_run else ["--upload"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"error": "build timed out"}
    out = (r.stdout or "") + (r.stderr or "")
    videos = re.findall(r"(video_[a-z]{2}\.mp4)", out)
    return {"exit_code": r.returncode, "seconds": round(time.time() - t0),
            "videos": sorted(set(videos)), "uploaded": not dry_run,
            "problems": "none" if "problems: none" in out else "see tail",
            "tail": out[-700:]}


def t_inspect_video(path):
    """Duration, resolution and whether there is real audio."""
    try:
        p = _safe_path(path)
        if not os.path.exists(p):
            return {"error": "no such file"}
        r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                            "-of", "json", p], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
        v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
        a = next((s for s in d.get("streams", []) if s["codec_type"] == "audio"), {})
        return {"duration": float(d.get("format", {}).get("duration", 0)),
                "resolution": "%sx%s" % (v.get("width"), v.get("height")),
                "has_audio": bool(a), "audio_codec": a.get("codec_name"),
                "size_mb": round(int(d.get("format", {}).get("size", 0)) / 1e6, 2)}
    except Exception as e:
        return {"error": str(e)}


def t_remember(lesson):
    """Store one durable lesson for future runs."""
    lesson = str(lesson).strip()[:500]
    if not lesson:
        return {"error": "empty lesson"}
    os.makedirs(SCRATCH, exist_ok=True)
    with open(MEMORY, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "lesson": lesson}) + "\n")
    return {"ok": True, "stored": lesson}


def t_recall():
    """Read back what earlier runs learned."""
    if not os.path.exists(MEMORY):
        return {"lessons": []}
    out = []
    for line in open(MEMORY, encoding="utf-8"):
        try:
            out.append(json.loads(line)["lesson"])
        except Exception:
            pass
    return {"lessons": out[-15:]}


TOOLS = {
    "list_history": (t_list_history, {
        "description": "List the videos already published, to avoid repeating topics.",
        "parameters": {"type": "object", "properties": {}}}),
    "read_file": (t_read_file, {
        "description": "Read a text file from the repo (e.g. README.md, studio/config.json).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "repo-relative path"}},
            "required": ["path"]}}),
    "write_file": (t_write_file, {
        "description": "Write a report or note. Only studio/scratch/, *.json and *.md.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}),
    "run_tests": (t_run_tests, {
        "description": "Run the project's test suite and get pass/fail counts.",
        "parameters": {"type": "object", "properties": {}}}),
    "build_video": (t_build_video, {
        "description": ("Build a kids video with the existing pipeline. Set dry_run=false "
                        "ONLY when the human has explicitly asked to publish."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["counting", "colors", "guess_animal", "story"]},
            "fmt": {"type": "string", "enum": ["short", "long"]},
            "languages": {"type": "string", "description": "e.g. en or en,es,pt"},
            "dry_run": {"type": "boolean"}}, "required": ["kind"]}}),
    "inspect_video": (t_inspect_video, {
        "description": "Check a rendered video: duration, resolution, real audio present.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}),
    "remember": (t_remember, {
        "description": "Store one durable lesson so future runs do not repeat a mistake.",
        "parameters": {"type": "object", "properties": {"lesson": {"type": "string"}},
                       "required": ["lesson"]}}),
    "recall": (t_recall, {
        "description": "Read lessons stored by earlier runs.",
        "parameters": {"type": "object", "properties": {}}}),
}

DANGEROUS = {"build_video"}


def call_tool(name, args, allow_publish=False):
    """Execute a tool. Every failure is returned as data, never raised at the model."""
    if name not in TOOLS:
        return {"error": "unknown tool %r" % name}
    fn = TOOLS[name][0]
    if name in DANGEROUS and not allow_publish and args.get("dry_run") is False:
        return {"error": "refused: publishing is disabled. Set dry_run=true, or run with "
                         "--allow-publish after the human has confirmed."}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": "bad arguments for %s: %s" % (name, e)}
    except Exception as e:
        return {"error": "%s failed: %s" % (name, e)}


# =========================================================================== providers
class Mock:
    """Deterministic provider so the harness can be tested with no key and no network."""
    name = "mock"

    def __init__(self, script=None):
        self.script = script or []
        self.i = 0
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.i < len(self.script):
            step = self.script[self.i]
            self.i += 1
            return step
        return {"text": "Done.", "tool_calls": []}


class OpenAICompat:
    """Groq, OpenRouter, Cerebras, Together, GitHub Models - all the same wire format."""
    def __init__(self, base_url, model, key_env):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.key_env = key_env
        self.name = model

    def chat(self, messages, tools):
        import requests
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError("%s is not set. Free key: see the docstring at the top."
                               % self.key_env)
        payload = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            payload["tools"] = [{"type": "function",
                                 "function": {"name": n, "description": s["description"],
                                              "parameters": s["parameters"]}}
                                for n, (_, s) in tools.items()]
        r = requests.post(self.base_url + "/chat/completions",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"},
                          json=payload, timeout=120)
        if r.status_code != 200:
            raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:300]))
        msg = r.json()["choices"][0]["message"]
        calls = [{"name": c["function"]["name"],
                  "args": json.loads(c["function"]["arguments"] or "{}")}
                 for c in (msg.get("tool_calls") or [])]
        return {"text": msg.get("content") or "", "tool_calls": calls}


class Gemini:
    """Google AI Studio. Best free tier for tool calling."""
    def __init__(self, model="gemini-2.5-flash", key_env="GEMINI_API_KEY"):
        self.model = model
        self.key_env = key_env
        self.name = model
        self.history = []

    def chat(self, messages, tools):
        import requests
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set. Get one free at "
                               "aistudio.google.com/apikey")
        # convert once
        contents = []
        for m in messages:
            if m["role"] == "tool":
                contents.append({"role": "function", "parts": [
                    {"functionResponse": {"name": m["name"],
                                          "response": {"result": m["content"]}}}]})
            else:
                contents.append({"role": "user" if m["role"] == "user" else "model",
                                 "parts": [{"text": m["content"]}]})
        body = {"contents": contents, "generationConfig": {"temperature": 0.3}}
        if tools:
            body["tools"] = [{"function_declarations": [
                {"name": n, "description": s["description"],
                 "parameters": s["parameters"]} for n, (_, s) in tools.items()]}]
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % self.model,
            params={"key": key}, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:300]))
        parts = r.json()["candidates"][0]["content"].get("parts", [])
        calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                 for p in parts if "functionCall" in p]
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        return {"text": text, "tool_calls": calls}


def get_provider(name=None, model=None):
    name = (name or os.environ.get("BRAIN_PROVIDER") or "auto").lower()
    if name == "mock":
        return Mock()
    if name == "gemini" or (name == "auto" and os.environ.get("GEMINI_API_KEY")):
        return Gemini(model or os.environ.get("BRAIN_MODEL") or "gemini-2.5-flash")
    if name == "groq" or (name == "auto" and os.environ.get("GROQ_API_KEY")):
        return OpenAICompat("https://api.groq.com/openai/v1",
                            model or "llama-3.3-70b-versatile", "GROQ_API_KEY")
    if name == "openrouter" or (name == "auto" and os.environ.get("OPENROUTER_API_KEY")):
        return OpenAICompat("https://openrouter.ai/api/v1",
                            model or "meta-llama/llama-3.3-70b-instruct:free",
                            "OPENROUTER_API_KEY")
    if name == "github" or (name == "auto" and os.environ.get("GITHUB_TOKEN")):
        return OpenAICompat("https://models.github.ai/inference",
                            model or "openai/gpt-4o-mini", "GITHUB_TOKEN")
    if name == "ollama":
        return OpenAICompat("http://localhost:11434/v1", model or "llama3.1", "OLLAMA_KEY")
    return None


# =========================================================================== the loop
SYSTEM = """You are the operator of a children's YouTube channel called {channel}.

You have tools. Use them instead of guessing: never claim something happened unless a tool
returned it. If a tool returns an error, read it and try a different approach - do not repeat
the same failing call more than once.

Rules:
- Publishing is off unless the human explicitly asked for it. Prefer dry_run=true.
- Prefer boring, reliable choices over clever ones.
- When you are done, reply with a plain-text summary of what you did and what you learned.
- You have at most {steps} steps. Spend them on the task, not on describing the task.

Lessons from earlier runs (may be empty):
{lessons}"""


def run(task, provider, allow_publish=False, max_steps=MAX_STEPS, verbose=True):
    """The agent loop. Returns (final_text, transcript)."""
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json")))
    except Exception:
        pass
    lessons = t_recall().get("lessons", [])
    messages = [{"role": "system",
                 "content": SYSTEM.format(channel=cfg.get("channel_name", "the channel"),
                                          steps=max_steps,
                                          lessons="\n".join("- " + x for x in lessons) or "(none)")},
                {"role": "user", "content": task}]

    log = []
    tool_calls = 0
    est_tokens = sum(len(m["content"]) for m in messages) // 4
    final = ""

    for step in range(1, max_steps + 1):
        try:
            reply = provider.chat(messages, TOOLS)
        except Exception as e:
            final = "provider error: %s" % e
            log.append({"step": step, "error": final})
            if verbose:
                print("  [%02d] %s" % (step, final))
            break

        text = (reply.get("text") or "").strip()
        calls = reply.get("tool_calls") or []
        est_tokens += (len(text) + 200) // 4
        if est_tokens > MAX_TOKENS_EST:
            final = "stopped: token budget exhausted"
            log.append({"step": step, "error": final})
            break

        if verbose and text:
            print("  [%02d] %s" % (step, text[:200].replace("\n", " ")))

        if not calls:
            final = text or "(no output)"
            log.append({"step": step, "text": text})
            break

        messages.append({"role": "assistant", "content": text or "(calling tools)"})
        for c in calls:
            tool_calls += 1
            if tool_calls > MAX_TOOL_CALLS:
                final = "stopped: too many tool calls"
                break
            name = c.get("name")
            args = c.get("args") or {}
            if verbose:
                print("       -> %s(%s)" % (name, json.dumps(args)[:140]))
            result = call_tool(name, args, allow_publish=allow_publish)
            snippet = json.dumps(result, ensure_ascii=False)
            if verbose:
                print("       <- %s" % snippet[:180])
            log.append({"step": step, "tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "name": name,
                             "content": snippet[:6000]})
        if final:
            break
    else:
        final = "stopped: step limit reached without a final answer"

    _write_transcript(task, log, final)
    return final, log


def _write_transcript(task, log, final):
    try:
        os.makedirs(SCRATCH, exist_ok=True)
        with open(TRANSCRIPT, "a", encoding="utf-8") as f:
            f.write("\n\n## %s\n\n**Task:** %s\n\n" % (time.strftime("%Y-%m-%d %H:%M"), task))
            for e in log:
                if "tool" in e:
                    f.write("```\n%s(%s)\n-> %s\n```\n" % (
                        e["tool"], json.dumps(e["args"], ensure_ascii=False),
                        json.dumps(e["result"], ensure_ascii=False)[:600]))
                elif "text" in e:
                    f.write(e["text"] + "\n")
                elif "error" in e:
                    f.write("**" + e["error"] + "**\n")
            f.write("\n**Result:** %s\n" % final)
    except Exception:
        pass


# =========================================================================== self test
def self_test():
    """Prove the harness works with no key and no network."""
    print("=== self test (mock provider, no network) ===")
    p = Mock([
        {"text": "", "tool_calls": [{"name": "list_history", "args": {}}]},
        {"text": "", "tool_calls": [{"name": "run_tests", "args": {}}]},
        {"text": "Checked history and tests. Nothing else needed.", "tool_calls": []},
    ])
    final, log = run("Check the channel is healthy", p, max_steps=6)
    print("\nfinal:", final)
    print("tool calls:", sum(1 for e in log if "tool" in e))
    assert "Nothing else needed" in final

    print("\n=== guardrail: path escape ===")
    for bad in ["../../etc/passwd", "/etc/passwd", "studio/../../x"]:
        print("  %-22s -> %s" % (bad, call_tool("read_file", {"path": bad}).get("error", "ALLOWED!")))
        assert "error" in call_tool("read_file", {"path": bad})

    print("\n=== guardrail: publishing blocked ===")
    r = call_tool("build_video", {"kind": "story", "dry_run": False})
    print("  ", r.get("error", r))
    assert "refused" in r.get("error", "")

    print("\n=== guardrail: step limit ===")
    p2 = Mock([{"text": "", "tool_calls": [{"name": "recall", "args": {}}]}] * 30)
    final, log = run("loop forever", p2, max_steps=4, verbose=False)
    print("  ", final)
    assert "step limit" in final

    print("\n=== guardrail: unknown tool is data, not a crash ===")
    print("  ", call_tool("rm_minus_rf", {}))
    assert "error" in call_tool("rm_minus_rf", {})

    print("\nALL SELF TESTS PASSED - harness works with zero API keys.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="*")
    ap.add_argument("--provider", help="gemini|groq|openrouter|github|ollama|mock")
    ap.add_argument("--model")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--allow-publish", action="store_true")
    ap.add_argument("--steps", type=int, default=MAX_STEPS)
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.task:
        ap.print_help()
        return 2

    prov = get_provider(a.provider, a.model)
    if prov is None:
        print("No provider configured. Pick one - all have a free tier:\n"
              "  export GEMINI_API_KEY=...       aistudio.google.com/apikey\n"
              "  export GROQ_API_KEY=...         console.groq.com\n"
              "  export OPENROUTER_API_KEY=...   openrouter.ai (models ending :free)\n"
              "  export GITHUB_TOKEN=...         models.github.ai\n"
              "Or run offline: python brain.py --self-test")
        return 2
    print("provider: %s\n" % prov.name)
    final, log = run(" ".join(a.task), prov, allow_publish=a.allow_publish,
                     max_steps=a.steps)
    print("\n=== result ===\n" + final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
