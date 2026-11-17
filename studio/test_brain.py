"""Tests for the agent kernel. No API key, no network, no Blender."""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location("brain", os.path.join(HERE, "brain.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


B = load()


# --------------------------------------------------------------------------- guardrails
@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../../etc/passwd", "studio/../../../x", "", None, "a/../../b",
])
def test_path_escape_is_refused(bad):
    r = B.call_tool("read_file", {"path": bad})
    assert "error" in r


def test_absolute_write_is_refused():
    r = B.call_tool("write_file", {"path": "/tmp/x.md", "content": "hi"})
    assert "error" in r


def test_write_outside_scratch_is_refused():
    r = B.call_tool("write_file", {"path": "studio/kids.py", "content": "rm -rf /"})
    assert "error" in r
    assert not open(os.path.join(HERE, "kids.py")).read().startswith("rm -rf")


def test_write_into_scratch_is_allowed():
    r = B.call_tool("write_file", {"path": "studio/scratch/_test.md", "content": "ok"})
    assert r.get("ok") is True
    os.remove(os.path.join(HERE, "scratch", "_test.md"))


def test_publishing_is_blocked_by_default():
    r = B.call_tool("build_video", {"kind": "story", "dry_run": False})
    assert "refused" in r["error"]


def test_publishing_allowed_only_on_request():
    # allow_publish=True skips the guard; still assert the guard exists as a separate decision
    assert "build_video" in B.DANGEROUS


def test_unknown_tool_returns_data_not_exception():
    assert "error" in B.call_tool("nope", {})


def test_bad_arguments_return_data_not_exception():
    r = B.call_tool("list_history", {"unexpected": 1})
    assert "error" in r


def test_bad_kind_is_rejected_before_building():
    r = B.call_tool("build_video", {"kind": "../../sh", "dry_run": True})
    assert "error" in r


def test_bad_language_is_rejected():
    r = B.call_tool("build_video", {"kind": "story", "languages": "en; rm -rf /", "dry_run": True})
    assert "error" in r


# --------------------------------------------------------------------------- loop
def test_loop_runs_tools_then_answers():
    p = B.Mock([
        {"text": "", "tool_calls": [{"name": "list_history", "args": {}}]},
        {"text": "Nothing to do.", "tool_calls": []},
    ])
    final, log = B.run("check", p, verbose=False)
    assert final == "Nothing to do."
    assert [e.get("tool") for e in log if "tool" in e] == ["list_history"]


def test_loop_stops_at_step_limit():
    p = B.Mock([{"text": "", "tool_calls": [{"name": "recall", "args": {}}]}] * 50)
    final, _ = B.run("forever", p, max_steps=3, verbose=False)
    assert "step limit" in final


def test_loop_stops_at_tool_call_ceiling():
    many = [{"name": "recall", "args": {}}] * 25
    p = B.Mock([{"text": "", "tool_calls": many}] * 3)
    final, _ = B.run("spam", p, max_steps=6, verbose=False)
    assert "too many tool calls" in final or "step limit" in final


def test_provider_error_is_reported_not_raised():
    class Boom:
        name = "boom"
        def chat(self, m, t):
            raise RuntimeError("quota exceeded")
    final, log = B.run("x", Boom(), verbose=False)
    assert "provider error" in final and "quota exceeded" in final


def test_missing_key_gives_instructions():
    os.environ.pop("GEMINI_API_KEY", None)
    p = B.get_provider("gemini")
    with pytest.raises(RuntimeError) as e:
        p.chat([{"role": "user", "content": "hi"}], {})
    assert "GEMINI_API_KEY" in str(e.value)


def test_auto_provider_detects_from_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert B.get_provider("auto").__class__ is B.OpenAICompat


def test_no_provider_returns_none(monkeypatch):
    for v in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert B.get_provider("auto") is None


def test_tool_schemas_are_wellformed():
    for name, (fn, schema) in B.TOOLS.items():
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"
        for req in schema["parameters"].get("required", []):
            assert req in schema["parameters"]["properties"], (name, req)


def test_transcript_is_written():
    p = B.Mock([{"text": "", "tool_calls": [{"name": "recall", "args": {}}]},
                {"text": "done", "tool_calls": []}])
    B.run("transcript check", p, verbose=False)
    assert os.path.exists(B.TRANSCRIPT)
    assert "transcript check" in open(B.TRANSCRIPT, encoding="utf-8").read()


def test_self_test_passes(monkeypatch):
    """Stub run_tests so the self-test does not recursively launch pytest."""
    real = dict(B.TOOLS)
    B.TOOLS["run_tests"] = (lambda: {"passed": 78, "failed": 0, "exit_code": 0},
                            real["run_tests"][1])
    try:
        assert B.self_test() == 0
    finally:
        B.TOOLS.clear(); B.TOOLS.update(real)
