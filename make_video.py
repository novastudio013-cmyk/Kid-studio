"""Kids Auto v4 - quality-first, free kids video maker (one file).

  python make_video.py --format short --upload      (make one video, upload it as PRIVATE)
  python make_video.py --format short --kind counting
  python make_video.py --test                       (offline check: no keys, no internet)

Kinds: counting, colors, guess_animal, story
"""
import argparse, array, asyncio, base64, glob, io, json, math, os, random, re, shutil, subprocess, sys, time, urllib.parse, wave
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --------------------------------------------------------------------------- config
DEFAULTS = {
    "channel_name": "Starlit Stories",
    "character_name": "Pip",
    "character_bible": ("Pip, a small round baby penguin with soft blue-grey feathers, a white belly, "
                        "big shiny dark eyes, tiny orange feet and a bright orange knitted beanie hat"),
    "image_style": ("3D animated children's cartoon, Pixar-like soft rounded shapes, very bright saturated "
                    "colors, high contrast, big friendly eyes, soft studio lighting, clean simple background, "
                    "cute and safe for toddlers"),
    "voice": "en-US-JennyNeural",
    "voice_rate": "-8%",
    "voice_pitch": "+4Hz",
    "kinds": ["counting", "colors", "guess_animal", "story"],
    "short_scenes": 7,
    "long_scenes": 14,
    "fps": 25,
    "candidates": 2,          # pictures tried per scene; an AI judge keeps the best
    "judge": True,
    "music": True,
    "outro_line": "See you next time!",
    "made_for_kids": True,
    "privacy": "private",
    "language": "en",
}


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists("config.json"):
        try:
            cfg.update(json.load(open("config.json")))
        except Exception as e:
            print("config.json problem, using defaults:", e)
    return cfg


# --------------------------------------------------------------------------- Gemini helper
_GEM = {"model": None}


def _candidate_models(key):
    names = []
    if os.environ.get("GEMINI_MODEL"):
        names.append(os.environ["GEMINI_MODEL"])
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=60)
        r.raise_for_status()
        found = []
        for m in r.json().get("models", []):
            n = m["name"].split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(x in n for x in ("image", "tts", "live", "audio", "embed", "robotics", "computer")):
                continue
            found.append(n)
        found.sort(key=lambda n: (("preview" in n or "exp" in n), "lite" in n, [-int(x) for x in re.findall(r"\d+", n)]))
        names += found
    except Exception as e:
        print("could not list models:", e)
    names += ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def gemini(parts, temperature=0.9):
    """Send parts to Gemini, expect JSON back. Tries several models. Returns parsed JSON."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    order = ([_GEM["model"]] if _GEM["model"] else []) + _candidate_models(key)[:8]
    seen, last = set(), None
    for model in order:
        if model in seen:
            continue
        seen.add(model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, params={"key": key}, json=body, timeout=180)
                if r.status_code in (429, 500, 503) and attempt == 0:
                    time.sleep(20)
                    continue
                if r.status_code != 200:
                    last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                    break
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                obj = json.loads(text)
                _GEM["model"] = model
                return obj
            except Exception as e:
                last = f"{model}: {e}"
                break
        print("model failed ->", last)
    raise RuntimeError("No Gemini model worked. Last error: " + str(last))


# --------------------------------------------------------------------------- story
KIND_GUIDE = {
    "counting": ("COUNTING RHYME. Scenes count up 1, 2, 3 ... Each scene shows EXACTLY that many of one kind of "
                 "friendly object (for example 3 red balloons) together with {c}. Narration is a bouncy repeating "
                 "refrain, e.g. 'One little star, shining so bright. Two little stars, shining so bright.' "
                 "on_screen_text is the digit (for example '3'). 'check' says exactly what must be visible, "
                 "e.g. 'exactly 3 red balloons'. Add one last recap scene that counts them all."),
    "colors": ("COLOR HUNT. Each scene is about one color. {c} finds 2-3 things of that color. Narration repeats "
               "'I see red, red, red! Red apples, red balloon, red shoes!'. on_screen_text is the color word. "
               "'check' says what must be visible, e.g. 'mostly red objects'."),
    "guess_animal": ("GUESS THE ANIMAL. Pairs of scenes: a clue scene then a reveal scene. Clue narration gives two "
                     "easy hints and asks 'Who am I?'. The clue picture shows a cute dark silhouette or the animal "
                     "half hidden behind a bush, with {c} peeking curiously. Reveal narration says the animal sound and "
                     "name, e.g. 'Roar! It is a lion!'. on_screen_text is '?' on clue scenes and the animal name on "
                     "reveal scenes. 'check' says what must be visible."),
    "story": ("MINI STORY with a kind lesson (sharing, helping, trying again, being brave, saying sorry). {c} is the "
              "hero. Simple problem, kind solution, happy ending, one short repeated phrase that returns in 3 scenes. "
              "on_screen_text is one key word on some scenes, otherwise empty. 'check' may be empty."),
}


def story_prompt(cfg, kind, fmt, past):
    n = cfg["short_scenes"] if fmt == "short" else cfg["long_scenes"]
    secs = "about 40-55 seconds in total" if fmt == "short" else "about 3 minutes in total"
    guide = KIND_GUIDE[kind].format(c=cfg["character_name"])
    avoid = "; ".join(past[-40:]) or "none yet"
    return f"""You are a award-winning writer of preschool videos for ages 2-5 on the channel "{cfg['channel_name']}".
Main character (always the same): {cfg['character_bible']}. Refer to the character only as "{cfg['character_name']}".
Kind of video: {guide}
Length: exactly {n} scenes, {secs}. Do not add an ending scene, one is added automatically.
Rules: completely original; gentle, warm, positive; no scary, violent or sad-ending content; very simple English
(words a 3 year old knows); narration of each scene is 1-2 SHORT sentences with a natural rhythm and, where it fits,
a rhyme; repeat a catchy phrase; no brand names or existing characters or songs.
image_prompt: describe only what we SEE (character pose and happy expression, objects, simple bright background).
Never ask for text, letters or numbers inside the picture.
Do NOT reuse these earlier titles: {avoid}.
Return ONLY JSON:
{{"title": "friendly hook title under 70 characters",
  "thumb_text": "2-3 big words for the thumbnail",
  "thumb_prompt": "close-up of {cfg['character_name']} smiling with the main object of this video",
  "description": "2-3 warm sentences for parents",
  "tags": ["8 to 12 tags"],
  "scenes": [{{"narration": "...", "image_prompt": "...", "on_screen_text": "", "check": ""}}]}}"""


def edit_prompt(cfg, draft):
    return f"""You are a strict senior editor of preschool (age 2-5) videos. Improve this draft JSON.
Checklist: strong first-second hook; rhythm and gentle rhyme; repetition kids can chant; words simple enough for a
3 year old; each narration 1-2 short sentences; clear happy ending with a tiny lesson; picture prompts concrete and
bright; the character is called "{cfg['character_name']}" only; nothing scary; same JSON schema and same number of scenes.
Return ONLY the improved JSON.
DRAFT:
{json.dumps(draft)}"""


def validate(story, expected=None):
    for k in ("title", "description", "tags", "scenes"):
        if k not in story:
            raise ValueError(f"story missing '{k}'")
    if not story["scenes"] or any("narration" not in s or "image_prompt" not in s for s in story["scenes"]):
        raise ValueError("story scenes are malformed")
    if expected and len(story["scenes"]) != expected:
        raise ValueError(f"expected {expected} scenes, got {len(story['scenes'])}")
    for s in story["scenes"]:
        s.setdefault("on_screen_text", "")
        s.setdefault("check", "")
    story.setdefault("thumb_text", story["title"][:24])
    story.setdefault("thumb_prompt", "")


def generate_story(cfg, kind, fmt, past):
    n = cfg["short_scenes"] if fmt == "short" else cfg["long_scenes"]
    draft = gemini([{"text": story_prompt(cfg, kind, fmt, past)}], temperature=1.0)
    validate(draft)
    try:
        better = gemini([{"text": edit_prompt(cfg, draft)}], temperature=0.6)
        validate(better, expected=len(draft["scenes"]))
        print("story edited by second pass")
        return better
    except Exception as e:
        print("editing pass skipped:", e)
        return draft


def fake_story(cfg):
    return {
        "title": "Test: Pip Counts Balloons", "thumb_text": "COUNT WITH PIP", "thumb_prompt": "", "description": "Test.",
        "tags": ["kids"],
        "scenes": [
            {"narration": "One little balloon, floating up so high!", "image_prompt": "one balloon", "on_screen_text": "1", "check": ""},
            {"narration": "Two little balloons, waving in the sky!", "image_prompt": "two balloons", "on_screen_text": "2", "check": ""},
            {"narration": "Three little balloons, hooray, hooray!", "image_prompt": "three balloons", "on_screen_text": "3", "check": ""},
        ],
    }


# --------------------------------------------------------------------------- images
def size_for(fmt):
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def _placeholder(path, size, idx):
    cols = [(255, 214, 165), (189, 224, 254), (205, 240, 200), (255, 200, 221)]
    img = Image.new("RGB", size, cols[idx % 4])
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w * .2, h * .3, w * .8, h * .75), fill=(120, 170, 230))
    d.ellipse((w * .35, h * .2, w * .65, h * .4), fill=(250, 250, 250))
    img.save(path, "JPEG", quality=90)


def _save(content, path, w, h):
    img = Image.open(io.BytesIO(content)).convert("RGB")
    ImageOps.fit(img, (w, h), Image.LANCZOS).save(path, "JPEG", quality=93)


def _pollinations(full, w, h, seed, path, model):
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
    params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": model}
    for attempt in range(4):
        wait = 0
        try:
            r = requests.get(url, params=params, timeout=240)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                _save(r.content, path, w, h)
                return True
            print(f"  pollinations/{model} try {attempt+1}: HTTP {r.status_code}")
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 0) or 0)
        except requests.RequestException as e:
            print(f"  pollinations/{model} try {attempt+1}: {e}")
        time.sleep(max(wait, 8 * (attempt + 1)))
    return False


def _gemini_image(full, w, h, path):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return False
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=60)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "image" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]
        names.sort(key=lambda n: ("preview" in n, n))
    except Exception as e:
        print("  gemini image: could not list models:", e)
        return False
    for n in names[:3]:
        try:
            body = {"contents": [{"parts": [{"text": full}]}],
                    "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{n}:generateContent",
                              params={"key": key}, json=body, timeout=180)
            if r.status_code != 200:
                print(f"  gemini image {n}: HTTP {r.status_code}")
                continue
            for part in r.json()["candidates"][0]["content"]["parts"]:
                data = part.get("inlineData") or part.get("inline_data")
                if data:
                    _save(base64.b64decode(data["data"]), path, w, h)
                    return True
        except Exception as e:
            print(f"  gemini image {n}: {e}")
    return False


def make_image(cfg, prompt, fmt, path, seed, idx, offline=False):
    """True if a real picture was saved. No fake 'unavailable' pictures in real runs."""
    w, h = size_for(fmt)
    if offline:
        _placeholder(path, (w, h), idx)
        return True
    full = (f"{cfg['image_style']}. Main character: {cfg['character_bible']}. Scene: {prompt}. "
            f"No text, no letters, no numbers, no watermark, no extra limbs.")
    return (_pollinations(full, w, h, seed, path, "flux")
            or _pollinations(full, w, h, seed + 1, path, "turbo")
            or _gemini_image(full, w, h, path))


def judge(paths, scene_text, check):
    """Ask Gemini (vision) to score candidate pictures. Returns list of scores or None."""
    parts = [{"text": (
        "You check illustrations for a preschool video. Scene: " + scene_text +
        ". Must be true: " + (check or "nothing special") + ". Score EACH image 0-10 on: matches the scene, cute 3D "
        "cartoon look, one clear main character, correct anatomy (no extra limbs or odd faces), NO text/letters/numbers "
        "anywhere, and the 'must be true' condition (if it clearly fails, score 3 or less). "
        'Return ONLY JSON: {"scores": [one number per image, in order]}')}]
    for p in paths:
        im = Image.open(p).convert("RGB")
        im.thumbnail((640, 640))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}})
    try:
        sc = gemini(parts, temperature=0.1)["scores"]
        if len(sc) == len(paths):
            return [float(x) for x in sc]
    except Exception as e:
        print("  judge skipped:", e)
    return None


def best_picture(cfg, scene, fmt, work, i, seed, offline):
    """Make several candidates, keep the best. Returns (path, ok_flag)."""
    final = os.path.join(work, f"s{i}.jpg")
    cands, scores_all = [], []
    rounds = 1 if offline else 2
    for rnd in range(rounds):
        new = []
        for k in range(1 if offline else cfg["candidates"]):
            p = os.path.join(work, f"s{i}_r{rnd}_c{k}.jpg")
            if make_image(cfg, scene["image_prompt"], fmt, p, seed + i * 50 + rnd * 10 + k, i, offline):
                new.append(p)
            if not offline:
                time.sleep(4)
        if not new:
            continue
        sc = None
        if cfg["judge"] and not offline and (len(new) > 1 or scene.get("check")):
            sc = judge(new, scene["image_prompt"], scene.get("check", ""))
            time.sleep(3)
        if sc is None:
            sc = [7.0] * len(new)  # judge not used: accept the first pictures
        cands += new
        scores_all += sc
        if max(scores_all) >= 7:
            break
        print(f"  scene {i+1}: best score {max(scores_all):.0f}, trying more pictures")
    if not cands:
        return None, False
    bi = max(range(len(cands)), key=lambda x: scores_all[x])
    shutil.copy(cands[bi], final)
    return final, scores_all[bi] >= 5


# --------------------------------------------------------------------------- text overlay
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
PALETTE = [(231, 76, 60), (243, 156, 18), (22, 160, 133), (41, 128, 185), (142, 68, 173), (233, 30, 120)]
COLOR_WORDS = {"red": (231, 40, 40), "blue": (30, 110, 230), "yellow": (240, 180, 0), "green": (30, 160, 70),
               "orange": (245, 130, 20), "purple": (140, 60, 200), "pink": (240, 80, 150), "brown": (140, 90, 50)}


def font(size):
    for p in FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def overlay_png(text, size, path, idx, y_frac=0.13):
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base = h * 0.09 if h > w else h * 0.14
    if len(text.strip()) <= 3:      # digits and '?' should be BIG for little kids
        base = h * 0.17 if h > w else h * 0.26
    fs = int(base)
    while True:
        f = font(fs)
        box = d.textbbox((0, 0), text, font=f, stroke_width=max(4, fs // 14))
        if box[2] - box[0] <= w * 0.82 or fs < 40:
            break
        fs = int(fs * 0.9)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = int(fs * 0.35)
    cx, cy = w // 2, int(h * y_frac) + th // 2
    d.rounded_rectangle((cx - tw // 2 - pad, cy - th // 2 - pad, cx + tw // 2 + pad, cy + th // 2 + pad),
                        radius=int(fs * 0.5), fill=(255, 255, 255, 235), outline=(40, 40, 70, 255), width=max(4, fs // 16))
    col = COLOR_WORDS.get(text.strip().lower(), PALETTE[idx % len(PALETTE)])
    d.text((cx - tw // 2 - box[0], cy - th // 2 - box[1]), text, font=f, fill=col + (255,),
           stroke_width=max(3, fs // 20), stroke_fill=(40, 40, 70, 255))
    img.save(path, "PNG")
    return path


# --------------------------------------------------------------------------- voice
def probe_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def make_voice(cfg, text, path, offline=False):
    if offline:
        secs = max(2.0, len(text) / 14)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=24000",
                        "-af", "volume=0.1", "-t", f"{secs:.2f}", "-c:a", "libmp3lame", path], check=True)
        return
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, cfg["voice"], rate=cfg["voice_rate"], pitch=cfg["voice_pitch"]).save(path)

    for attempt in range(3):
        try:
            asyncio.run(run())
            return
        except Exception as e:
            print("  voice try", attempt + 1, e)
            time.sleep(5)
    raise RuntimeError("voice generation failed")


# --------------------------------------------------------------------------- music (original, synthesized)
SCALE = [523.25, 587.33, 659.25, 783.99, 880.00, 1046.50]  # C major pentatonic
BASS = [130.81, 110.00, 87.31, 98.00]                          # C  Am  F  G


def _note(freq, dur, sr=44100):
    n = int(sr * dur)
    out = []
    for i in range(n):
        t = i / sr
        env = math.exp(-t * 5.5) * min(1.0, t * 200)
        v = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(2 * math.pi * freq * 4 * t) * math.exp(-t * 14) \
            + 0.15 * math.sin(2 * math.pi * freq * 2 * t)
        out.append(v * env)
    return out


def make_music(path, seconds, seed):
    sr = 44100
    rnd = random.Random(seed)
    total = int(sr * (seconds + 1))
    buf = [0.0] * total
    cache = {}

    def add(freq, start, dur, amp):
        key = (round(freq, 2), round(dur, 2))
        if key not in cache:
            cache[key] = _note(freq, dur, sr)
        s = int(start * sr)
        for i, v in enumerate(cache[key]):
            if s + i >= total:
                break
            buf[s + i] += v * amp

    beat = 60 / 100.0
    phrase = [rnd.randrange(len(SCALE)) for _ in range(8)]
    bar = beat * 4
    t, b = 0.0, 0
    while t < seconds:
        chord = BASS[b % 4]
        add(chord, t, 1.6, 0.55)
        add(chord * 2, t + beat * 2, 1.2, 0.3)
        for q in range(4):
            idx = phrase[(b * 4 + q) % 8]
            if (b // 4) % 2 == 1 and q == 3:  # variation in every second phrase
                idx = min(len(SCALE) - 1, idx + 1)
            if rnd.random() < 0.85:
                add(SCALE[idx], t + q * beat, 0.9, 0.42)
        t += bar
        b += 1
    peak = max(abs(x) for x in buf) or 1.0
    scale = 0.8 * 32767 / peak
    fade = int(sr * 2)
    data = array.array("h")
    for i, v in enumerate(buf):
        g = 1.0
        if i > total - fade:
            g = max(0.0, (total - i) / fade)
        s = int(v * scale * g)
        data.append(s)
        data.append(s)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())
    return path


# --------------------------------------------------------------------------- video
def _run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode(errors="ignore")[-600:])


LEAD, TAIL, XF = 0.35, 0.85, 0.45
TRANSITIONS = ["fade", "slideleft", "circleopen", "wiperight", "fade", "slideup"]


def scene_clip(img, audio, overlay, out, size, fps, idx):
    w, h = size
    dur = max(3.2, probe_duration(audio) + LEAD + TAIL)
    frames = int(dur * fps)
    kinds = [("min(zoom+0.0012,1.28)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
             ("if(eq(on,0),1.28,max(zoom-0.0012,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
             ("1.18", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"),
             ("1.18", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)")]
    z, x, y = kinds[idx % 4]
    fc = f"[0:v]scale={w*2}:{h*2},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}[bg];"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", img]
    if overlay:
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.2f}", "-i", overlay]
        fc += "[1:v]format=rgba,fade=in:st=0.3:d=0.35:alpha=1[ov];[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]"
        ai = 2
    else:
        fc += "[bg]format=yuv420p[v]"
        ai = 1
    cmd += ["-i", audio, "-filter_complex", fc, "-map", "[v]", "-map", f"{ai}:a",
            "-af", f"adelay={int(LEAD*1000)}:all=1,apad=whole_dur={dur:.2f}", "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(fps),
            "-c:a", "aac", "-ar", "44100", "-ac", "2", out]
    _run(cmd)
    return dur


def join_with_crossfades(clips, durs, out):
    if len(clips) == 1:
        shutil.copy(clips[0], out)
        return
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", c]
    fc, vprev, aprev, acc = [], "[0:v]", "[0:a]", durs[0]
    for i in range(1, len(clips)):
        off = acc - XF
        vo, ao = f"[v{i}]", f"[a{i}]"
        fc.append(f"{vprev}[{i}:v]xfade=transition={TRANSITIONS[i % len(TRANSITIONS)]}:duration={XF}:offset={off:.3f}{vo}")
        fc.append(f"{aprev}[{i}:a]acrossfade=d={XF}{ao}")
        vprev, aprev = vo, ao
        acc = acc + durs[i] - XF
    cmd += ["-filter_complex", ";".join(fc), "-map", vprev, "-map", aprev,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", out]
    _run(cmd)


def mix_music(video, music, out):
    total = probe_duration(video)
    fc = (f"[1:a]volume=0.20,afade=t=in:d=1.5,afade=t=out:st={max(0, total-2.5):.2f}:d=2.5[m];"
          f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
          f"loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", music, "-filter_complex", fc,
          "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])


def thumbnail(image, text, size, out):
    img = Image.open(image).convert("RGB")
    img = ImageOps.fit(img, size, Image.LANCZOS)
    d = ImageDraw.Draw(img)
    w, h = size
    bw = max(10, w // 60)
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 214, 10), width=bw)
    words = text.upper().split() or ["HELLO"]
    fs = int(h * (0.085 if h > w else 0.17))
    maxw = w * 0.86
    while True:
        f = font(fs)
        sw = max(6, fs // 9)
        lines, cur = [], ""
        for wd in words:
            trial = (cur + " " + wd).strip()
            if d.textbbox((0, 0), trial, font=f, stroke_width=sw)[2] <= maxw or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = wd
        lines.append(cur)
        widest = max(d.textbbox((0, 0), ln, font=f, stroke_width=sw)[2] for ln in lines)
        if (widest <= maxw and len(lines) <= 3) or fs < 30:
            break
        fs = int(fs * 0.9)
    y = int(h * 0.05)
    for ln in lines[:3]:
        d.text((w // 2, y), ln, font=f, fill=(255, 255, 255), anchor="ma", stroke_width=sw, stroke_fill=(30, 30, 80))
        y += int(fs * 1.2)
    img.save(out, "JPEG", quality=92)


def check_video(path, fmt, offline=False):
    info = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", path])
    streams = json.loads(info)["streams"]
    types = [s["codec_type"] for s in streams]
    dur = probe_duration(path)
    problems = []
    if "video" not in types:
        problems.append("no video stream")
    if "audio" not in types:
        problems.append("no audio stream")
    if offline:
        return problems, dur
    if fmt == "short" and not (20 <= dur <= 175):
        problems.append(f"short length {dur:.0f}s is outside 20-175s")
    if fmt == "long" and dur < 60:
        problems.append(f"long video too short ({dur:.0f}s)")
    if os.path.getsize(path) < 200_000:
        problems.append("file is tiny")
    return problems, dur


# --------------------------------------------------------------------------- upload
def upload(video, thumb, story, cfg, fmt):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"], client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    title = story["title"][:90] + (" #Shorts" if fmt == "short" else "")
    desc = (story["description"] + "\n\nOriginal story, characters and music by " + cfg["channel_name"] +
            ". Pictures and voice are created with AI tools.\n#kids #learning #nurseryrhymes")
    body = {"snippet": {"title": title, "description": desc, "tags": story["tags"], "categoryId": "27",
                        "defaultLanguage": cfg["language"]},
            "status": {"privacyStatus": cfg["privacy"], "selfDeclaredMadeForKids": cfg["made_for_kids"],
                       "containsSyntheticMedia": True}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(video, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
    except Exception as e:
        print("thumbnail skipped:", e)
    return vid


# --------------------------------------------------------------------------- main
def pick_kind(cfg, history, wanted):
    if wanted:
        return wanted
    used = [h.get("kind") for h in history[-12:]]
    return min(cfg["kinds"], key=lambda k: (used.count(k), random.random()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=["short", "long"], default="short")
    ap.add_argument("--kind", choices=list(KIND_GUIDE))
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--test", action="store_true", help="offline: fake story, placeholder pictures, simple voice")
    a = ap.parse_args()

    cfg = load_cfg()
    hist_path = "history.json"
    history = json.load(open(hist_path)) if os.path.exists(hist_path) else []
    kind = pick_kind(cfg, history, a.kind)
    run = time.strftime("%Y%m%d-%H%M%S")
    work = os.path.join("output", run)
    os.makedirs(work, exist_ok=True)
    size = size_for(a.format)
    offline = a.test

    print(f"[1/6] Story: {kind}, {a.format}")
    st = fake_story(cfg) if offline else generate_story(cfg, kind, a.format, [h["title"] for h in history])
    json.dump(st, open(os.path.join(work, "story.json"), "w"), indent=2)

    scenes = list(st["scenes"])
    scenes.append({"narration": cfg["outro_line"], "image_prompt": "", "on_screen_text": "See you soon!", "check": "", "outro": True})

    print("[2/6] Pictures, voice, text, motion")
    clips, durs, seed = [], [], random.randint(1, 10**6)
    last_img, bad = None, 0
    for i, sc in enumerate(scenes):
        print(f"  scene {i+1}/{len(scenes)}")
        if sc.get("outro"):
            img = last_img
        else:
            img, ok = best_picture(cfg, sc, a.format, work, i, seed, offline)
            if img is None:
                bad += 1
                if last_img is None:
                    sys.exit("STOPPED: could not make the first picture. Nothing was uploaded.")
                img = last_img
            elif not ok:
                bad += 1
                print(f"  scene {i+1}: picture quality is doubtful")
            last_img = img
        aud = os.path.join(work, f"a{i}.mp3")
        make_voice(cfg, sc["narration"], aud, offline)
        ov = None
        if sc.get("on_screen_text"):
            ov = overlay_png(sc["on_screen_text"], size, os.path.join(work, f"t{i}.png"), i)
        clip = os.path.join(work, f"c{i}.mp4")
        durs.append(scene_clip(img, aud, ov, clip, size, cfg["fps"], i))
        clips.append(clip)
    if bad > max(1, len(scenes) // 4):
        sys.exit(f"STOPPED: {bad} of {len(scenes)} scenes had failed or doubtful pictures. Nothing was uploaded. Try again later.")

    print("[3/6] Crossfade the scenes")
    joined = os.path.join(work, "joined.mp4")
    join_with_crossfades(clips, durs, joined)

    print("[4/6] Music and loudness")
    out = os.path.join(work, "video.mp4")
    total = probe_duration(joined)
    tracks = glob.glob("music/*.mp3") + glob.glob("music/*.wav")
    if cfg["music"]:
        music = random.choice(tracks) if tracks else make_music(os.path.join(work, "music.wav"), total, seed)
        mix_music(joined, music, out)
    else:
        shutil.copy(joined, out)

    print("[5/6] Thumbnail and checks")
    thumb_src = os.path.join(work, "thumb_src.jpg")
    if offline or not st.get("thumb_prompt") or not make_image(cfg, st["thumb_prompt"], a.format, thumb_src, seed + 999, 0, offline):
        thumb_src = os.path.join(work, "s0.jpg")
    thumb = os.path.join(work, "thumb.jpg")
    thumbnail(thumb_src, st["thumb_text"], size, thumb)
    problems, dur = check_video(out, a.format, offline)
    print(f"  length {dur:.0f}s, problems: {problems or 'none'}")
    if problems:
        sys.exit("STOPPED: quality check failed: " + "; ".join(problems) + ". Nothing was uploaded.")
    print("Video:", out)

    if a.upload and not offline:
        print("[6/6] Upload (private)")
        vid = upload(out, thumb, st, cfg, a.format)
        print("Uploaded (private). Review at https://studio.youtube.com/video/%s/edit" % vid)
        history.append({"title": st["title"], "kind": kind, "id": vid, "date": run})
        json.dump(history, open(hist_path, "w"), indent=2)


if __name__ == "__main__":
    main()
