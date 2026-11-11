"""Kids Auto - one-file free kids video maker. Run: python make_video.py --format short --upload
Offline check: python make_video.py --test"""
import argparse, asyncio, glob, io, json, os, random, re, subprocess, sys, time, urllib.parse
import requests
from PIL import Image, ImageDraw, ImageFont

# ===== voice =====


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def make_voice(text, voice, rate, path, offline=False):
    if offline:  # silent clip, length based on text, for pipeline testing
        secs = max(2.0, len(text) / 14)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", f"{secs:.2f}", "-c:a", "libmp3lame", path], check=True)
        return
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice, rate=rate).save(path)

    asyncio.run(run())

# ===== story =====

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _prompt(cfg, category, fmt, past_titles):
    n = cfg["short_scenes"] if fmt == "short" else cfg["long_scenes"]
    secs = "about 45 seconds total" if fmt == "short" else "about 4-6 minutes total"
    avoid = "; ".join(past_titles[-40:]) or "none yet"
    return f"""You write original children's video scripts for ages 2-8. Theme: {cfg['channel_theme']}.
Category: {category}. Format: {fmt} ({secs}). Exactly {n} scenes.
Rules: completely original characters and story (no existing franchises or characters), gentle,
positive, no violence or scary content, simple English, a clear little lesson at the end.
Do NOT reuse these earlier titles/ideas: {avoid}.
Return ONLY JSON with this shape:
{{"title": "catchy title under 70 chars",
 "description": "2-3 sentence YouTube description",
 "tags": ["8 to 12 tags"],
 "character_description": "one sentence describing the main character's look, reused in every image",
 "scenes": [{{"narration": "1-3 short sentences read aloud", "image_prompt": "what the picture shows"}}]}}"""


def generate_story(cfg, category, fmt, past_titles):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": _prompt(cfg, category, fmt, past_titles)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    r = requests.post(url, params={"key": key}, json=body, timeout=120)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    story = json.loads(text)
    validate(story)
    return story


def validate(story):
    for k in ("title", "description", "tags", "character_description", "scenes"):
        if k not in story:
            raise ValueError(f"story missing '{k}'")
    if not story["scenes"] or any("narration" not in s or "image_prompt" not in s for s in story["scenes"]):
        raise ValueError("story scenes are malformed")


def fake_story(fmt):
    """Offline sample used by --test so the video step can be checked without any API."""
    n = 3
    return {
        "title": "Test Story: Pip the Little Star",
        "description": "A short test story.",
        "tags": ["kids", "story"],
        "character_description": "a small yellow star with a smiling face",
        "scenes": [{"narration": f"This is test scene number {i+1}. Pip smiles and waves.",
                    "image_prompt": "Pip waves hello"} for i in range(n)],
    }

# ===== images =====


def size_for(fmt):
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def _placeholder(path, size, text, idx):
    colors = [(255, 214, 165), (189, 224, 254), (205, 240, 200), (255, 200, 221)]
    img = Image.new("RGB", size, colors[idx % len(colors)])
    d = ImageDraw.Draw(img)
    d.text((size[0] // 2 - 60, size[1] // 2), text[:40], fill=(60, 60, 60))
    img.save(path, "JPEG", quality=90)


def make_image(prompt, style, character, fmt, path, seed, idx, offline=False):
    w, h = size_for(fmt)
    if offline:
        _placeholder(path, (w, h), "test image", idx)
        return False
    full = f"{style}. Main character: {character}. Scene: {prompt}"
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
    params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": "flux"}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                Image.open(io.BytesIO(r.content)).convert("RGB").resize((w, h)).save(path, "JPEG", quality=92)
                return True
        except requests.RequestException:
            pass
        time.sleep(5 * (attempt + 1))
    _placeholder(path, (w, h), "image unavailable", idx)
    return False

# ===== video =====


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def scene_clip(img, audio, out, size, fps, idx):
    w, h = size
    dur = duration(audio) + 0.6
    frames = int(dur * fps)
    # alternate zoom in / zoom out / drift for variety
    if idx % 3 == 0:
        z, x, y = "min(zoom+0.0010,1.25)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif idx % 3 == 1:
        z, x, y = "if(eq(on,0),1.25,max(zoom-0.0010,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.15", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    vf = f"scale={w*2}:{h*2},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},format=yuv420p"
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", img, "-i", audio, "-vf", vf,
          "-af", "apad=pad_dur=0.6", "-t", f"{dur:.2f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-ar", "44100", "-ac", "2", out])
    return dur


def _srt_time(t):
    ms = int(t * 1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


def write_srt(narrations, durs, path):
    t, lines = 0.0, []
    for i, (txt, d) in enumerate(zip(narrations, durs), 1):
        lines += [str(i), f"{_srt_time(t)} --> {_srt_time(t + d - 0.3)}", txt, ""]
        t += d
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def assemble(clips, srt, music_dir, out, workdir):
    listing = os.path.join(workdir, "clips.txt")
    open(listing, "w").write("".join(f"file '{os.path.abspath(c)}'\n" for c in clips))
    joined = os.path.join(workdir, "joined.mp4")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", joined])
    tracks = glob.glob(os.path.join(music_dir, "*.mp3")) + glob.glob(os.path.join(music_dir, "*.wav"))
    if tracks:  # optional quiet background music (use only music you have the rights to)
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", joined, "-stream_loop", "-1", "-i", random.choice(tracks),
              "-filter_complex", "[1:a]volume=0.08[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
              "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", out])
    else:
        os.replace(joined, out)


def thumbnail(first_image, title, size, out):
    img = Image.open(first_image).convert("RGB").resize(size)
    d = ImageDraw.Draw(img)
    fs = size[0] // 12
    font = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(p):
            font = ImageFont.truetype(p, fs)
            break
    font = font or ImageFont.load_default()
    words, lines, cur = title.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) > 16:
            lines.append(cur); cur = wd
        else:
            cur = (cur + " " + wd).strip()
    lines.append(cur)
    y = size[1] // 12
    for ln in lines[:3]:
        d.text((size[0] // 14, y), ln, font=font, fill="white", stroke_width=max(4, fs // 12), stroke_fill="black")
        y += int(fs * 1.2)
    img.save(out, "JPEG", quality=90)

# ===== upload =====

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(video, thumb, story, cfg, fmt):
    from googleapiclient.http import MediaFileUpload
    yt = _service()
    title = story["title"][:95] + (" #Shorts" if fmt == "short" else "")
    body = {
        "snippet": {"title": title, "description": story["description"] + "\n\nOriginal story made with AI tools.",
                    "tags": story["tags"], "categoryId": "27", "defaultLanguage": cfg["language"]},
        "status": {"privacyStatus": cfg["privacy"], "selfDeclaredMadeForKids": cfg["made_for_kids"],
                   "containsSyntheticMedia": True},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(video, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
    except Exception as e:  # thumbnails need a verified channel; not fatal
        print("thumbnail skipped:", e)
    return vid

# ===== main =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=["short", "long"], default="short")
    ap.add_argument("--category")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--test", action="store_true", help="offline: fake story, placeholder images, silent audio")
    a = ap.parse_args()

    cfg = json.load(open("config.json"))
    hist_path = "history.json"
    history = json.load(open(hist_path)) if os.path.exists(hist_path) else []
    category = a.category or random.choice(cfg["categories"])
    run = time.strftime("%Y%m%d-%H%M%S")
    work = os.path.join("output", run)
    os.makedirs(work, exist_ok=True)

    print(f"[1/5] Story ({category}, {a.format})")
    st = fake_story(a.format) if a.test else generate_story(cfg, category, a.format, [h["title"] for h in history])
    json.dump(st, open(os.path.join(work, "story.json"), "w"), indent=2)

    size = size_for(a.format)
    clips, durs, seed = [], [], random.randint(1, 10**6)
    for i, sc in enumerate(st["scenes"]):
        print(f"[2-4/5] Scene {i+1}/{len(st['scenes'])}")
        img = os.path.join(work, f"s{i}.jpg"); aud = os.path.join(work, f"s{i}.mp3"); clip = os.path.join(work, f"s{i}.mp4")
        make_image(sc["image_prompt"], cfg["image_style"], st["character_description"], a.format, img, seed, i, a.test)
        make_voice(sc["narration"], cfg["voice"], cfg["voice_rate"], aud, a.test)
        durs.append(scene_clip(img, aud, clip, size, cfg["fps"], i))
        clips.append(clip)

    print("[5/5] Assemble")
    out = os.path.join(work, "video.mp4")
    write_srt([s["narration"] for s in st["scenes"]], durs, os.path.join(work, "captions.srt"))
    assemble(clips, None, "music", out, work)
    thumb = os.path.join(work, "thumb.jpg")
    thumbnail(os.path.join(work, "s0.jpg"), st["title"], size, thumb)
    print("Video:", out)

    if a.upload and not a.test:
        vid = upload(out, thumb, st, cfg, a.format)
        print("Uploaded (private). Review at https://studio.youtube.com/video/%s/edit" % vid)
        history.append({"title": st["title"], "category": category, "id": vid, "date": run})
        json.dump(history, open(hist_path, "w"), indent=2)


if __name__ == "__main__":
    main()
