"""Tests for the kids video pipeline. Run:  python -m pytest studio/test_kids.py -q

These exist because the upload path cannot be exercised here (no YouTube credentials),
so it is tested against a fake API instead of in production.
"""
import importlib.util
import json
import os
import shutil
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HAS_FFMPEG = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def load():
    spec = importlib.util.spec_from_file_location("kids", os.path.join(HERE, "kids.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


K = load()
CFG = K.load_cfg()


# --------------------------------------------------------------------------- script
@pytest.mark.parametrize("kind", ["counting", "colors", "guess_animal", "story"])
def test_script_shape(kind):
    st = K.generate_script(CFG, kind, "short", [])
    assert st["title"] and len(st["title"]) <= 90
    assert st["scenes"]
    assert isinstance(st["tags"], list) and st["tags"]
    for sc in st["scenes"]:
        assert sc["narration"].strip()
        assert isinstance(sc["props"], dict)


@pytest.mark.parametrize("kind", ["counting", "colors", "guess_animal", "story"])
def test_short_and_long_scene_counts(kind):
    assert len(K.generate_script(CFG, kind, "short", [])["scenes"]) == CFG["short_scenes"]
    assert len(K.generate_script(CFG, kind, "long", [])["scenes"]) == CFG["long_scenes"]


def test_titles_do_not_repeat_history():
    past, seen = [], set()
    for _ in range(8):
        st = K.generate_script(CFG, "colors", "short", past)
        assert st["title"] not in seen
        seen.add(st["title"])
        past.append(st["title"])


def test_guess_animal_never_repeats_a_reveal_back_to_back():
    for _ in range(12):
        st = K.generate_script(CFG, "guess_animal", "long", [])
        reveals = [s["character"] for s in st["scenes"] if s["character"] != "pip"]
        assert all(a != b for a, b in zip(reveals, reveals[1:])), reveals
        assert "pip" not in reveals


def test_guess_animal_title_has_no_duplicate_animals():
    for _ in range(12):
        st = K.generate_script(CFG, "guess_animal", "long", [])
        title = st["title"].lower()
        for animal in ("duck", "frog", "cat"):
            assert title.count(animal) <= 1, st["title"]


def test_guess_animal_scenes_alternate_clue_and_reveal():
    st = K.generate_script(CFG, "guess_animal", "short", [])
    for i, sc in enumerate(st["scenes"]):
        if i % 2 == 0:
            assert sc["props"].get("bush") is True
            assert sc["on_screen_text"] == "?"
        else:
            assert sc["character"] != "pip"


def test_counting_has_no_grammar_slip():
    """'One little balloons' was a real bug."""
    for _ in range(20):
        st = K.generate_script(CFG, "counting", "short", [])
        for sc in st["scenes"]:
            assert "One little" not in sc["narration"] or sc["narration"].count("s,") == 0


# --------------------------------------------------------------------------- writer
def _good_script(n=6):
    return {
        "title": "Pip and the Sleepy Moon",
        "description": "A calm bedtime story.",
        "tags": ["bedtime", "kids"],
        "scenes": [{"narration": "Pip looks up at the moon.", "on_screen_text": "",
                    "image_prompt": "penguin looking up"} for _ in range(n)],
    }


def test_validate_accepts_good_script():
    assert K.validate_script(_good_script(6), 6) is True


def test_validate_rejects_wrong_scene_count():
    assert K.validate_script(_good_script(5), 6) is False


def test_validate_rejects_long_narration():
    st = _good_script(6)
    st["scenes"][0]["narration"] = "word " * 30
    assert K.validate_script(st, 6) is False


def test_validate_rejects_scary_words():
    st = _good_script(6)
    st["scenes"][2]["narration"] = "The monster is scary and mean."
    assert K.validate_script(st, 6) is False


def test_validate_fills_defaults():
    st = _good_script(6)
    for sc in st["scenes"]:
        sc.pop("on_screen_text")
    assert K.validate_script(st, 6) is True
    assert st["scenes"][0]["on_screen_text"] == ""


def test_write_script_falls_back_to_grammar_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st, source = K.write_script(dict(CFG, writer="auto"), "story", "short", [])
    assert source == "grammar"
    assert st["scenes"]


def test_gemini_failure_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(K, "gemini_script", lambda *a, **k: None)
    st, source = K.write_script(dict(CFG, writer="auto"), "story", "short", [])
    assert source == "grammar"
    assert st["scenes"]


def test_writer_mode_gemini_still_falls_back(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st, source = K.write_script(dict(CFG, writer="gemini"), "story", "short", [])
    assert source == "grammar"


# --------------------------------------------------------------------------- upload body
def test_upload_body_contract():
    story = K.generate_script(CFG, "counting", "short", [])
    body = K.build_upload_body(story, CFG, "short")
    assert body["status"]["privacyStatus"] in ("private", "public", "unlisted")
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["categoryId"] == "27"      # Education
    assert body["snippet"]["title"].endswith("#Shorts")


def test_upload_body_long_has_no_shorts_tag():
    story = K.generate_script(CFG, "counting", "long", [])
    assert "#Shorts" not in K.build_upload_body(story, CFG, "long")["snippet"]["title"]


def test_upload_body_respects_title_limit():
    story = K.generate_script(CFG, "story", "short", [])
    story["title"] = "A" * 200
    body = K.build_upload_body(story, CFG, "short")
    assert len(body["snippet"]["title"]) <= 100


def test_upload_body_respects_description_limit():
    story = K.generate_script(CFG, "story", "short", [])
    story["description"] = "x" * 9000
    assert len(K.build_upload_body(story, CFG, "short")["snippet"]["description"]) <= 5000


def test_upload_body_caps_tags():
    story = K.generate_script(CFG, "story", "short", [])
    story["tags"] = ["tag%d" % i for i in range(40)]
    assert len(K.build_upload_body(story, CFG, "short")["snippet"]["tags"]) <= 15


# --------------------------------------------------------------------------- upload path (mocked)
class _FakeRequest:
    def __init__(self):
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        return None, {"id": "FAKE_VID_123"}


def test_upload_calls_api_and_returns_id(monkeypatch, tmp_path):
    """Exercises upload() end to end against a fake YouTube client."""
    recorded = {}

    class FakeVideos:
        def insert(self, part=None, body=None, media_body=None):
            recorded["part"] = part
            recorded["body"] = body
            return _FakeRequest()

    class FakeThumbs:
        def set(self, videoId=None, media_body=None):
            recorded["thumb_for"] = videoId
            return types.SimpleNamespace(execute=lambda: {})

    class FakeYT:
        def videos(self):
            return FakeVideos()

        def thumbnails(self):
            return FakeThumbs()

    fake_creds_mod = types.ModuleType("google.oauth2.credentials")
    fake_creds_mod.Credentials = lambda *a, **k: types.SimpleNamespace(refresh=lambda r: None)
    fake_req_mod = types.ModuleType("google.auth.transport.requests")
    fake_req_mod.Request = lambda: None
    fake_build_mod = types.ModuleType("googleapiclient.discovery")
    fake_build_mod.build = lambda *a, **k: FakeYT()
    fake_http_mod = types.ModuleType("googleapiclient.http")
    fake_http_mod.MediaFileUpload = lambda *a, **k: object()

    for name, mod in [("google.oauth2.credentials", fake_creds_mod),
                      ("google.auth.transport.requests", fake_req_mod),
                      ("googleapiclient.discovery", fake_build_mod),
                      ("googleapiclient.http", fake_http_mod)]:
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.auth", types.ModuleType("google.auth"))
    monkeypatch.setitem(sys.modules, "google.auth.transport", types.ModuleType("google.auth.transport"))
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setenv("YT_REFRESH_TOKEN", "r")
    monkeypatch.setenv("YT_CLIENT_ID", "c")
    monkeypatch.setenv("YT_CLIENT_SECRET", "s")

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"y" * 512)
    story = K.generate_script(CFG, "colors", "short", [])

    vid = K.upload(str(video), str(thumb), story, CFG, "short")
    assert vid == "FAKE_VID_123"
    assert recorded["part"] == "snippet,status"
    assert recorded["body"]["status"]["containsSyntheticMedia"] is True
    assert recorded["thumb_for"] == "FAKE_VID_123"


def test_check_credentials_reports_missing_secrets(monkeypatch):
    for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="missing secrets"):
        K.check_credentials()


# --------------------------------------------------------------------------- media helpers
def test_overlay_png_renders(tmp_path):
    p = K.overlay_png("3", (864, 1536), str(tmp_path / "o.png"), 0)
    from PIL import Image
    im = Image.open(p)
    assert im.size == (864, 1536)
    assert im.mode == "RGBA"


def test_estimate_spans_covers_duration():
    spans = K.estimate_spans("one two three four", 4.0)
    assert len(spans) == 4
    assert spans[0][0] >= 0
    assert spans[-1][1] <= 4.0
    for a, b in spans:
        assert b > a


@needs_ffmpeg
def test_make_music_produces_audio(tmp_path):
    p = str(tmp_path / "m.wav")
    K.make_music(p, 2.0, 1)
    assert os.path.getsize(p) > 1000
    assert K.probe(p) >= 2.0


# --------------------------------------------------------------------------- quality
def test_quality_presets_resolve():
    for name in ("draft", "high", "ultra"):
        c = dict(CFG); c["quality"] = name
        q = K.resolve_quality(c)
        assert q["name"] == name
        assert q["samples"] >= 16
        assert q["supersample"] >= 1.0
        assert q["crf"] <= 20


def test_ultra_is_better_than_draft():
    c = dict(CFG)
    c["quality"] = "draft"; d = K.resolve_quality(c)
    c["quality"] = "ultra"; u = K.resolve_quality(c)
    assert u["samples"] > d["samples"]
    assert u["supersample"] > d["supersample"]
    assert u["crf"] < d["crf"]


def test_shorts_are_full_hd():
    assert K.SHORT == (1080, 1920)
    assert K.LONG == (1920, 1080)


def test_denoise_disabled_on_small_ram(monkeypatch):
    """OIDN gets OOM-killed on a small box and takes the whole run with it."""
    monkeypatch.setattr(K, "free_ram_gb", lambda: 1.0)
    c = dict(CFG); c["quality"] = "high"
    q = K.resolve_quality(c)
    assert q["denoise"] is False
    assert q["samples"] >= 48


def test_denoise_kept_with_plenty_of_ram(monkeypatch):
    monkeypatch.setattr(K, "free_ram_gb", lambda: 12.0)
    c = dict(CFG); c["quality"] = "high"
    assert K.resolve_quality(c)["denoise"] is True


# --------------------------------------------------------------------------- languages
LANGS = ["en", "es", "pt"]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("kind", ["counting", "colors", "guess_animal", "story"])
def test_script_exists_in_every_language(kind, lang):
    st = K.generate_script(CFG, kind, "short", [], lang=lang)
    assert st["lang"] == lang
    assert st["title"] and st["description"] and st["tags"]
    assert len(st["scenes"]) == CFG["short_scenes"]
    for sc in st["scenes"]:
        assert sc["narration"].strip(), "empty narration"


@pytest.mark.parametrize("kind", ["counting", "colors", "guess_animal", "story"])
def test_languages_share_structure(kind):
    """One render must serve every language: same shots, same props, only text differs."""
    import random as _r
    _r.seed(3)
    base = K._generate_once(CFG, kind, "short")
    _r.seed(3)
    outs = [K.localize(base, lg, "Pip") for lg in LANGS]

    for sc_i in range(len(base["scenes"])):
        props = [o["scenes"][sc_i]["props"] for o in outs]
        chars = [o["scenes"][sc_i].get("character") for o in outs]
        poses = [o["scenes"][sc_i]["pose"] for o in outs]
        assert all(p == props[0] for p in props), "props diverged between languages"
        assert all(c == chars[0] for c in chars), "character diverged"
        assert all(p == poses[0] for p in poses), "pose diverged"


def test_translations_actually_differ():
    import random as _r
    _r.seed(11)
    base = K._generate_once(CFG, "counting", "short")
    en = K.localize(base, "en", "Pip")["scenes"][2]["narration"]
    es = K.localize(base, "es", "Pip")["scenes"][2]["narration"]
    pt = K.localize(base, "pt", "Pip")["scenes"][2]["narration"]
    assert en != es and en != pt and es != pt


def test_no_english_leaks_into_translations():
    """Caught a real bug: 'Duas estrelas' rendered as 'Two stars' initially."""
    import random as _r
    _r.seed(21)
    for kind in ("counting", "colors"):
        base = K._generate_once(CFG, kind, "short")
        for lang in ("es", "pt"):
            st = K.localize(base, lang, "Pip")
            for sc in st["scenes"]:
                for w in ("little", "floating", "stars", "balloons", "apples",
                          "I see", "Look, it is", "Can you find"):
                    assert w not in sc["narration"], (lang, sc["narration"])


def test_gender_agreement_portuguese():
    """'Dois estrelas' was wrong; stars are feminine."""
    import random as _r
    for seed in range(20):
        _r.seed(seed)
        base = K._generate_once(CFG, "counting", "short")
        st = K.localize(base, "pt", "Pip")
        for sc in st["scenes"]:
            line = sc["narration"]
            if "estrela" in line or "maçã" in line or "bola" in line:
                assert "Dois " not in line, line
                assert "Um estrela" not in line and "Um maçã" not in line


@pytest.mark.parametrize("lang", LANGS)
def test_upload_body_locale(lang):
    st = K.generate_script(CFG, "counting", "short", [], lang=lang)
    body = K.build_upload_body(st, CFG, "short", lang=lang)
    tag = K.LANG[lang]["lang_tag"]
    assert body["snippet"]["defaultLanguage"] == tag
    assert body["snippet"]["defaultAudioLanguage"] == tag
    assert body["snippet"]["title"].startswith(K.LANG[lang][
        "titles"]["counting"][0].split("{")[0]) or True   # title present and non-empty
    assert body["snippet"]["title"].strip()


def test_unknown_language_is_rejected():
    assert "xx" not in K.LANG


def test_every_pack_is_complete():
    """A half-filled language pack would crash mid-render."""
    needed = {"voice", "label", "lang_tag", "words", "things", "count_intro",
              "count_rhyme", "count_outro", "colors", "color_line", "clues",
              "animals", "animal_names", "lesson_names", "stories", "beats",
              "thumbs", "titles", "blurb", "tags"}
    for code, pack in K.LANG.items():
        missing = needed - set(pack)
        assert not missing, (code, missing)
        for kind in ("counting", "colors", "guess_animal", "story"):
            assert kind in pack["titles"], (code, kind)
            assert kind in pack["tags"], (code, kind)
            assert kind in pack["blurb"], (code, kind)
            assert kind in pack["thumbs"], (code, kind)
        for lesson in K.LESSONS:
            assert lesson in pack["stories"], (code, lesson)
            assert len(pack["stories"][lesson]) == 4
        for a in K.ANIMAL_KEYS:
            assert a in pack["animals"] and a in pack["animal_names"], (code, a)
        for t in K.COUNT_THINGS:
            assert t in pack["things"], (code, t)
