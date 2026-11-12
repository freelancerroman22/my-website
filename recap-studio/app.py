#!/usr/bin/env python3
"""
Recap Studio
============
একটা লম্বা পডকাস্ট/ভিডিও থেকে অনেকগুলো ছোট পাওয়ারফুল ক্লিপ বের করে,
প্রতিটা ক্লিপকে আলাদা ভিডিও বানায়, তারপর সেগুলো জোড়া দিয়ে "রিক্যাপ" বানায়।

ব্রাউজার প্যানেলে:
  - ভিডিও আপলোড
  - প্রতিটা ক্লিপ নাম + ভিডিও সহ দেখা যায় (টেক্সট না, আসল ভিডিও)
  - ক্লিপ সামনে-পিছনে / বাদ / যোগ
  - পছন্দের রিক্যাপ এক্সপোর্ট

লোকাল অ্যাপ: তোমার কম্পিউটারেই চলে। ট্রান্সক্রাইব + ক্লিপ খোঁজা হয় Google-এর
ফ্রি Gemini API দিয়ে।
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, render_template

# -------------------- সেটিংস --------------------
# Google-এর ফ্রি Gemini API key (https://aistudio.google.com/apikey থেকে ফ্রি)
API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
# fallback মডেল; কিন্তু আসলে get_models() key থেকে কাজের মডেল নিজে খুঁজে নেয়
GEMINI_MODELS = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest",
                 "gemini-2.0-flash-001", "gemini-2.5-flash-lite"]
_DISCOVERED = None


def _discover(client):
    """key-তে আসলে যে generateContent মডেলগুলো আছে, একবার খুঁজে ক্যাশে রাখা।"""
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED
    found = []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").split("/")[-1]
            if not name or "embedding" in name or "image" in name or "tts" in name:
                continue
            actions = getattr(m, "supported_actions", None)
            if actions is None:
                actions = getattr(m, "supported_generation_methods", None)
            if actions is None or "generateContent" in actions:
                if "flash" in name or "pro" in name:
                    found.append(name)
    except Exception:
        pass
    _DISCOVERED = sorted(set(found))
    return _DISCOVERED


def get_models(client, quality=False):
    """quality=False: দ্রুত flash আগে (ট্রান্সক্রাইব/পোস্ট)।
    quality=True: সবচেয়ে বুদ্ধিমান মডেল আগে (ক্লিপ বাছাই, ভালো ভ্যালুর জন্য)।"""
    found = list(_discover(client))

    def rank_fast(n):
        s = 0
        if "flash" in n: s -= 10
        if "2.5" in n: s -= 3
        elif "2.0" in n: s -= 2
        if "lite" in n: s += 1
        if "pro" in n: s += 2
        return s

    def rank_quality(n):
        s = 0
        if "pro" in n: s -= 6
        if "2.5" in n: s -= 4
        if "flash" in n: s -= 2
        if "lite" in n: s += 4   # lite সবচেয়ে কম বুদ্ধিমান, পেছনে
        return s

    found = sorted(found, key=(rank_quality if quality else rank_fast))
    order = found + [m for m in GEMINI_MODELS if m not in found]
    return order
TRANSCRIBE_CHUNK_SEC = 600         # অডিও কত সেকেন্ডের টুকরো করে ট্রান্সক্রাইব করবে
# ক্লিপ = একটা পুরো ভ্যালুয়েবল চিন্তা (হুক + শেষ পর্যন্ত punchline), ছোট টুকরো না
CLIP_MIN_DEFAULT = 30              # ডিফল্ট ক্লিপ দৈর্ঘ্য রেঞ্জ (সেকেন্ড)
CLIP_MAX_DEFAULT = 60
ABS_MIN_CLIP = 5                   # একদম সর্বনিম্ন/সর্বোচ্চ সীমা
ABS_MAX_CLIP = 200
LEAD_IN = 0.3                      # ক্লিপের শুরুতে সামান্য আগে (প্রথম শব্দ যেন না কাটে)
TAIL = 0.5                         # ক্লিপের শেষে সামান্য পরে পর্যন্ত
RECAP_TARGET_SEC = 120            # রিক্যাপ লক্ষ্য ~২ মিনিট

# আকার অনুযায়ী আউটপুট সাইজ (W, H)
ASPECTS = {
    "9:16": (1080, 1920),   # রিলস/শর্টস/টিকটক
    "1:1":  (1080, 1080),   # স্কয়ার
    "16:9": (1280, 720),    # ইউটিউব
}
# ------------------------------------------------

# .exe (PyInstaller) হিসেবে চললে ডেটা exe-এর পাশে, templates বান্ডিল থেকে
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent.resolve()      # uploads/output exe-এর পাশে
    RES_DIR = Path(sys._MEIPASS)                           # বান্ডিল করা templates/static
else:
    BASE_DIR = Path(__file__).parent.resolve()
    RES_DIR = BASE_DIR

UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
CLIPS_DIR = OUTPUT_DIR / "clips"
WORK_DIR = BASE_DIR / "work"
for d in (UPLOAD_DIR, OUTPUT_DIR, CLIPS_DIR, WORK_DIR):
    d.mkdir(exist_ok=True)

app = Flask(__name__,
            template_folder=str(RES_DIR / "templates"),
            static_folder=str(RES_DIR / "static"))

# ---- সেলফ-আপডেট (অ্যাপের ভেতরের "Update" বাটন) ----
# তোমার পাবলিক রিপো থেকে লেটেস্ট ফাইল নামায়। ফ্রি, পাসওয়ার্ড লাগে না।
RAW_BASE = "https://raw.githubusercontent.com/freelancerroman22/my-website/main/recap-studio/"
UPDATE_FILES = ["app.py", "requirements.txt", "VERSION",
                "templates/index.html", "static/icon.png",
                "START.bat", "MAKE_APP.bat"]
VERSION_FILE = BASE_DIR / "VERSION"


def current_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "?"

JOBS = {}   # job_id -> {status, step, clips, recaps, video_path, duration, error}


# ==================== সাহায্যকারী ====================

def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True)


def get_duration(video_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def extract_audio(video_path: Path, job_id: str) -> Path:
    audio_path = WORK_DIR / f"{job_id}.mp3"
    run(["ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)])
    return audio_path


def transcribe(audio_path: Path, job=None):
    """Google Gemini (ফ্রি) দিয়ে অডিও ট্রান্সক্রাইব, টাইমস্ট্যাম্প সহ। লম্বা অডিও
    টুকরো করে পাঠানো হয়, পরে টাইমস্ট্যাম্প জোড়া লাগে।"""
    from google import genai
    from google.genai import types
    if not API_KEY:
        raise RuntimeError("Gemini API key নেই। (GEMINI_API_KEY সেট করো)")

    client = genai.Client(api_key=API_KEY)
    total = get_duration(audio_path)
    segments = []
    t, idx = 0.0, 0

    instruction = (
        "এই অডিওটা হুবহু ট্রান্সক্রাইব করো। আউটপুট দাও শুধু JSON আকারে: "
        "{\"segments\":[{\"start\":<সেকেন্ড>,\"end\":<সেকেন্ড>,\"text\":\"...\"}]}. "
        "প্রতিটা segment ছোট রাখো (এক বাক্য)। start/end এই অডিও টুকরোর শুরু থেকে "
        "সেকেন্ডে। শুধু JSON দাও।"
    )

    while t < total:
        clen = min(TRANSCRIBE_CHUNK_SEC, total - t)
        part = WORK_DIR / f"tc_{idx}.mp3"
        run(["ffmpeg", "-y", "-ss", str(t), "-i", str(audio_path),
             "-t", str(clen), "-ac", "1", "-ar", "16000", "-b:a", "64k", str(part)])
        if job:
            job["step"] = f"ট্রান্সক্রাইব হচ্ছে... (~{int(t//60)} মিনিট পর্যন্ত)"

        audio_bytes = part.read_bytes()
        raw, last_err = None, None
        models = get_models(client)
        for model in models:
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
                        instruction,
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", max_output_tokens=8192),
                )
                raw = resp.text.strip()
                break
            except Exception as ex:
                last_err = ex
                continue
        part.unlink(missing_ok=True)
        if raw is None:
            raise RuntimeError(f"Gemini ট্রান্সক্রাইব করতে পারেনি। চেষ্টা করা মডেল: "
                               f"{models}. শেষ এরর: {last_err}")

        try:
            s0, e0 = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[s0:e0 + 1])
            for s in data.get("segments", []):
                segments.append({
                    "start": float(s["start"]) + t,
                    "end": float(s["end"]) + t,
                    "text": str(s.get("text", "")).strip(),
                })
        except Exception:
            pass

        t += clen
        idx += 1

    return segments


def build_transcript_block(segments):
    return "\n".join(
        f"[{i}] ({s['start']:.1f}-{s['end']:.1f}) {s['text']}"
        for i, s in enumerate(segments)
    )


def tidy_hook(hook: str) -> str:
    """হুক ছোট ও পাঞ্চি রাখার শেষ রক্ষা।"""
    h = (hook or "").strip().strip('"').strip()
    if ":" in h:
        before = h.split(":", 1)[0].strip()
        if len(before.split()) >= 3:
            h = before
    for sep in (". ", "? ", "! "):
        if sep in h:
            h = h.split(sep, 1)[0] + sep.strip()
            break
    words = h.split()
    if len(words) > 10:
        h = " ".join(words[:10]) + "..."
    return h


def find_clips(segments, target_clips, clip_min=CLIP_MIN_DEFAULT, clip_max=CLIP_MAX_DEFAULT):
    """Gemini দিয়ে অনেকগুলো পুরো-চিন্তার ভ্যালুয়েবল ক্লিপ + হুক বের করা।"""
    from google import genai
    if not API_KEY:
        raise RuntimeError("Gemini API key নেই।")

    client = genai.Client(api_key=API_KEY)
    transcript_block = build_transcript_block(segments)

    prompt = f"""তুমি একজন অভিজ্ঞ short-form video editor, যে Diary of a CEO,
On Purpose, Mel Robbins এর মতো পডকাস্ট থেকে ভাইরাল হাইলাইট বানায়।

নিচে একটা পডকাস্টের ট্রান্সক্রিপ্ট। প্রতিটা লাইনের শুরুতে [নম্বর] আর
(শুরু_সেকেন্ড-শেষ_সেকেন্ড)।

তোমার কাজ: সবচেয়ে ভ্যালুয়েবল, scroll-stopping মুহূর্তগুলো বেছে নাও।
কতগুলো নেবে তা তুমি নিজে ঠিক করো, মোটামুটি {target_clips}টার আশেপাশে
(কম-বেশি হতে পারে, যতগুলো সত্যিই পাওয়ারফুল)।

একটা মুহূর্ত তখনই নেবে যখন এর অন্তত একটা আছে (এটাই "ভ্যালু"):
- একটা counterintuitive/surprising সত্য বা bold দাবি (সাধারণ জ্ঞান না)।
- একটা শক্তিশালী mental model, নিয়ম, বা এক-লাইনের গভীর উপদেশ যা মানুষ সেভ/শেয়ার করতে চাইবে।
- তীব্র emotion, ব্যক্তিগত স্বীকারোক্তি, বা একটা টানটান ছোট গল্প/মুহূর্ত।
- একটা প্রশ্ন বা লাইন যা curiosity-gap তৈরি করে (শুনে মানুষ থমকে যায়)।

কঠোরভাবে বাদ দাও (এগুলো ভ্যালু না):
- সাধারণ, obvious, বা cliché কথা ("হ্যাঁ, একদম", "ভালো প্রশ্ন", ভূমিকা, ধন্যবাদ)।
- হোস্টের সেটআপ/প্রশ্ন, বা অতিথি পরিচয়, বা স্পন্সর/বিজ্ঞাপন অংশ।
- এমন লাইন যেটা context ছাড়া একা শুনলে মানে বোঝায় না।
মনে রাখো: কম কিন্তু সত্যিকারের পাওয়ারফুল ক্লিপ অনেক ভালো, দুর্বল ক্লিপে সংখ্যা ভরাট কোরো না।

ক্লিপের নিয়ম (খুব জরুরি, রেফারেন্স স্টাইল):
- প্রতিটা ক্লিপ হবে একটা **পুরো, সম্পূর্ণ চিন্তা** — একটা শক্তিশালী লাইন দিয়ে শুরু
  (hook), তারপর সেই পয়েন্টটা বুঝিয়ে শেষে একটা payoff/punchline-এ শেষ।
- দৈর্ঘ্য মোটামুটি {clip_min}-{clip_max} সেকেন্ড। চিন্তাটা সম্পূর্ণ করতে একটু কম-বেশি
  হতে পারে, কিন্তু কখনো {ABS_MAX_CLIP} সেকেন্ডের বেশি না।
- ক্লিপ যেন একটা স্বাভাবিক জায়গায় শুরু ও শেষ হয়, কথার মাঝপথে কেটে নয়।
- punchline/সিদ্ধান্তের লাইনটা অবশ্যই ক্লিপের ভেতরে রাখো, কেটে ফেলো না।
- একা দেখলে ক্লিপটা নিজে থেকেই সম্পূর্ণ ও বোধগম্য হতে হবে।

hook লেখার নিয়ম (রেফারেন্স স্টাইল, খুব জরুরি):
- hook হবে ক্লিপের শুরুর দিকের সবচেয়ে ধাক্কা-দেওয়া আসল বাক্যটাই, প্রায় হুবহু
  (verbatim)। নিজে থেকে নতুন টাইটেল বানাবে না।
- খুব ছোট রাখো, সর্বোচ্চ ৬-১০ শব্দ। দরকার হলে বড় বাক্যের সবচেয়ে পাঞ্চি অংশটুকু নাও।
- একটা bold দাবি বা curiosity-gap প্রশ্ন, যা শুনে মানুষ থমকে যায়।
- বর্ণনামূলক টাইটেল বা কোলন ( : ) লিখবে না।

ভালো hook (বক্তার আসল কথা): "There has never been a natural pandemic."
"Your phone is making you dumber." "Arguments aren't meant to be won."
"People change when the pain exceeds the fear."
খারাপ hook (বানানো টাইটেল): "Understanding How Trauma Affects Relationships"

প্রতিটা ক্লিপের একটা strength দাও (১-১০): এটা একা একটা শর্ট ভিডিও হিসেবে কতটা
scroll-stopping ও ভাইরাল হওয়ার যোগ্য। সবচেয়ে পাওয়ারফুল লাইনকে ৯-১০ দাও।

শুধু এই JSON ফরম্যাটে উত্তর দাও:
{{
  "clips": [
    {{"start": <সেকেন্ড>, "end": <সেকেন্ড>, "hook": "<হুবহু ছোট হুক>",
      "strength": <১-১০>, "topic": "<১-২ শব্দ>"}}
  ]
}}

ট্রান্সক্রিপ্ট:
{transcript_block}
"""
    raw, last_err = None, None
    models = get_models(client, quality=True)   # ভালো ভ্যালুর জন্য সবচেয়ে বুদ্ধিমান মডেল
    for model in models:
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            raw = resp.text.strip()
            break
        except Exception as ex:
            last_err = ex
            continue
    if raw is None:
        raise RuntimeError(f"Gemini থেকে উত্তর পাওয়া যায়নি: {last_err}")

    s, e = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[s:e + 1])
    clips = data.get("clips", [])

    # ট্রান্সক্রিপ্টের শেষ সীমা (হ্যালুসিনেটেড টাইমস্ট্যাম্প ক্ল্যাম্প করতে)
    max_end = max((seg["end"] for seg in segments), default=0) or 10 ** 9

    raw_clips = []
    for c in clips:
        try:
            st, en = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if st < 0:
            st = 0
        if st >= max_end:          # ট্রান্সক্রিপ্টের বাইরে, বাদ
            continue
        en = min(en, max_end)
        if en - st < ABS_MIN_CLIP:      # খুব ছোট হলে বাদ (চিন্তা অসম্পূর্ণ)
            continue
        if en - st > ABS_MAX_CLIP:      # runaway হলে কাটি
            en = st + ABS_MAX_CLIP
        if en <= st:
            continue
        try:
            strength = int(float(c.get("strength", 5)))
        except (TypeError, ValueError):
            strength = 5
        raw_clips.append({
            "start": round(st, 1), "end": round(en, 1),
            "strength": max(1, min(10, strength)),
            "hook": tidy_hook(c.get("hook", "")) or "ক্লিপ",
            "topic": c.get("topic", ""),
        })

    # সময় অনুযায়ী সাজাই, তারপর বেশি ওভারল্যাপ করা ক্লিপ (ডুপ্লিকেট) বাদ দিই
    raw_clips.sort(key=lambda c: c["start"])
    deduped = []
    for c in raw_clips:
        clash = None
        for d in deduped:
            ov = min(c["end"], d["end"]) - max(c["start"], d["start"])
            if ov > 0 and ov >= 0.5 * min(c["end"] - c["start"], d["end"] - d["start"]):
                clash = d
                break
        if clash is None:
            deduped.append(c)
        elif c["strength"] > clash["strength"]:   # বেশি স্ট্রং হলে পুরোনোটা বদলে দিই
            deduped[deduped.index(clash)] = c

    deduped.sort(key=lambda c: c["start"])
    cleaned = []
    for i, c in enumerate(deduped):
        cleaned.append({
            "id": i,
            "start": c["start"], "end": c["end"],
            "dur": round(c["end"] - c["start"], 1),
            "strength": c["strength"],
            "hook": c["hook"], "topic": c["topic"],
            "file": "",
        })
    return cleaned


def aspect_filter(aspect):
    """আকার অনুযায়ী scale/crop ফিল্টার ও আউটপুট সাইজ।"""
    w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
    if aspect == "16:9":
        # পুরো ফ্রেম রাখি, কালো প্যাড
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    else:
        # ভরে দিয়ে সেন্টার-ক্রপ (ভার্টিকাল/স্কয়ার)
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},setsar=1")
    return vf, w, h


def _ass_time(t):
    t = max(0, t)
    h = int(t // 3600); m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def chunk_words(text, n=4):
    words = text.split()
    return [" ".join(words[i:i + n]) for i in range(0, len(words), n)] or [text]


def build_ass(path, clip, segments, w, h, render_start, render_dur,
              show_captions, show_hook):
    """ক্লিপের জন্য একটা .ass ফাইল বানানো (ক্যাপশন + হুক টাইটেল)।"""
    cap_fs = int(h * 0.044)
    hook_fs = int(h * 0.046)
    outline = max(2, int(h * 0.004))
    cap_marginv = int(h * 0.14)        # নিচ থেকে ক্যাপশনের উচ্চতা
    hook_marginv = int(h * 0.07)       # উপর থেকে হুকের দূরত্ব
    side = int(w * 0.08)               # দুই পাশে মার্জিন (কেটে যাওয়া রোধ)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{cap_fs},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,{side},{side},{cap_marginv},1
Style: Hook,DejaVu Sans,{hook_fs},&H0000E5FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline+1},1,8,{side},{side},{hook_marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

    if show_hook and clip.get("hook"):
        htxt = clip["hook"].replace("\n", " ").replace("{", "(").replace("}", ")")
        hend = min(2.4, render_dur)
        events.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(hend)},Hook,,0,0,0,,{htxt.upper()}")

    if show_captions:
        cstart = clip["start"]
        cend = clip["end"]
        for seg in segments:
            if seg["end"] <= cstart or seg["start"] >= cend:
                continue
            words = chunk_words(seg.get("text", ""), 4)
            if not words or not seg.get("text"):
                continue
            seg_s = max(seg["start"], cstart)
            seg_e = min(seg["end"], cend)
            if seg_e <= seg_s:
                continue
            slice_dur = (seg_e - seg_s) / len(words)
            for k, chunk in enumerate(words):
                a = (seg_s + k * slice_dur) - render_start
                b = (seg_s + (k + 1) * slice_dur) - render_start
                a = max(0, a); b = min(render_dur, b)
                if b <= a:
                    continue
                txt = chunk.replace("{", "(").replace("}", ")").strip()
                if txt:
                    events.append(
                        f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,{txt}")

    if not events:
        return None
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def get_src_dims(video_path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True)
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def render_single_clip(job_id, clip, video_path, duration, opts, segments):
    """একটা ক্লিপ আলাদা ভিডিও বানানো।
    ডিফল্ট: আসল আকার, আসল কোয়ালিটি (stream copy, রি-এনকোড ছাড়া, দ্রুত)।
    শুধু আকার বদলালে বা ক্যাপশন/হুক দিলে তখন রি-এনকোড হয়।"""
    start = max(0, clip["start"] - LEAD_IN)
    end = clip["end"] + TAIL
    if duration:
        end = min(end, duration)
    render_dur = end - start

    aspect = opts.get("aspect", "original")
    captions = opts.get("captions", False)
    hook = opts.get("hook_title", False)
    fname = f"{job_id}_clip_{clip['id']}.mp4"
    out = CLIPS_DIR / fname

    # পরিষ্কার ক্লিপ (আসল আকার, টেক্সট নেই) → stream copy, আসল কোয়ালিটি
    if aspect in ("original", "", None) and not captions and not hook:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
             "-t", str(render_dur), "-c", "copy",
             "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)],
            check=True, capture_output=True)
        return fname

    # আকার বদল / ক্যাপশন / হুক → রি-এনকোড
    if aspect in ("original", "", None):
        w, h = get_src_dims(video_path)
        vf = "setsar=1"
    else:
        vf, w, h = aspect_filter(aspect)

    ass_name = f"{job_id}_clip_{clip['id']}.ass"
    ass_path = WORK_DIR / ass_name
    ass_made = None
    if captions or hook:
        ass_made = build_ass(ass_path, clip, segments, w, h, start, render_dur, captions, hook)
    if ass_made:
        vf = vf + f",ass={ass_name}"

    run_in = WORK_DIR if ass_made else None
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
           "-t", str(render_dur), "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, cwd=run_in)
    if ass_made:
        ass_path.unlink(missing_ok=True)
    return fname


def gen_post_copy(clips):
    """Gemini দিয়ে পোস্ট ক্যাপশন + হ্যাশট্যাগ বানানো (সোশ্যালে পোস্টের জন্য)।"""
    if not API_KEY or not clips:
        return {}
    from google import genai
    top = sorted(clips, key=lambda c: -c.get("strength", 5))[:8]
    hooks = "\n".join(f"- {c['hook']}" for c in top)
    prompt = (
        "নিচে একটা পডকাস্টের সেরা কিছু হুক দেওয়া আছে। এগুলোর ভিত্তিতে একটা "
        "short-form ভিডিও পোস্টের জন্য লেখো:\n"
        "১) caption: ১-২ লাইনের scroll-stopping ক্যাপশন (ইংরেজিতে)।\n"
        "২) hashtags: ৮টা relevant hashtag।\n"
        "শুধু JSON দাও: {\"caption\":\"...\",\"hashtags\":[\"#..\"]}\n\n"
        f"হুকগুলো:\n{hooks}"
    )
    client = genai.Client(api_key=API_KEY)
    for model in get_models(client):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            raw = resp.text.strip()
            s, e = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[s:e + 1])
            return {
                "caption": str(data.get("caption", "")).strip(),
                "hashtags": [str(x) for x in data.get("hashtags", [])][:12],
            }
        except Exception:
            continue
    return {}


def make_recaps(clips, num_recaps, target_sec=RECAP_TARGET_SEC):
    """রেফারেন্স স্টাইল রিক্যাপ: প্রতিটা রিক্যাপ শুরু হয় একটা স্ট্রং হুক ক্লিপ দিয়ে
    (cold open), তারপর বাকি ভালো ক্লিপগুলো সময়ের ক্রমে জোড়া লাগে।"""
    recaps = []
    n = len(clips)
    if n == 0:
        return recaps

    by_strength = sorted(clips, key=lambda c: (-c.get("strength", 5), c["start"]))
    num_recaps = max(1, num_recaps)

    for r in range(num_recaps):
        opener = by_strength[r % len(by_strength)]      # প্রতি রিক্যাপে আলাদা স্ট্রং ওপেনার
        chosen, seen, total = [opener], {opener["id"]}, opener["dur"]
        # বাকিটা strength অনুযায়ী ভরি target পর্যন্ত
        for c in by_strength:
            if total >= target_sec:
                break
            if c["id"] in seen:
                continue
            chosen.append(c)
            seen.add(c["id"])
            total += c["dur"]
        # ওপেনার আগে, বাকিগুলো সময়ের ক্রমে (স্বাভাবিক গল্পের প্রবাহ)
        rest = sorted([c for c in chosen if c["id"] != opener["id"]], key=lambda c: c["start"])
        ordered = [opener] + rest
        title = "সেরা হাইলাইট" if r == 0 else f"রিক্যাপ {r+1}"
        recaps.append({
            "id": r, "title": title,
            "clip_ids": [c["id"] for c in ordered],
        })
    return recaps


def render_recap(job_id, clip_ids, out_name):
    """নির্বাচিত ক্লিপের আগে থেকে বানানো ভিডিওগুলো জোড়া দিয়ে রিক্যাপ (দ্রুত, re-encode ছাড়া)।"""
    job = JOBS[job_id]
    video_path = Path(job["video_path"])
    duration = job.get("duration", 0)
    clips_by_id = {c["id"]: c for c in job["clips"]}

    files = []
    for cid in clip_ids:
        f = CLIPS_DIR / f"{job_id}_clip_{cid}.mp4"
        if not f.exists() and cid in clips_by_id:
            render_single_clip(job_id, clips_by_id[cid], video_path, duration,
                               job.get("opts", {}), job.get("segments", []))
        if f.exists():
            files.append(f)

    list_file = WORK_DIR / f"{job_id}_{out_name}_list.txt"
    list_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in files), encoding="utf-8")
    out_path = OUTPUT_DIR / f"{out_name}.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", "-movflags", "+faststart", str(out_path)])
    list_file.unlink(missing_ok=True)
    return out_path


# ==================== ব্যাকগ্রাউন্ড প্রসেস ====================

def process_video(job_id, target_clips, num_recaps, recap_sec, opts):
    job = JOBS[job_id]
    try:
        video_path = Path(job["video_path"])
        duration = job["duration"]

        job.update(status="running", step="অডিও বের করা হচ্ছে...")
        audio = extract_audio(video_path, job_id)

        job["step"] = "ট্রান্সক্রাইব করা হচ্ছে..."
        segments = transcribe(audio, job)
        job["segments"] = segments
        audio.unlink(missing_ok=True)

        if not segments:
            raise RuntimeError("ট্রান্সক্রিপ্ট পাওয়া যায়নি। অডিও ঠিক আছে কিনা দেখো।")

        job["step"] = "সেরা পুরো-চিন্তার মুহূর্ত খোঁজা হচ্ছে..."
        clips = find_clips(segments, target_clips,
                           opts.get("clip_min", CLIP_MIN_DEFAULT),
                           opts.get("clip_max", CLIP_MAX_DEFAULT))
        if not clips:
            raise RuntimeError("কোনো ভালো ক্লিপ পাওয়া যায়নি। আবার চেষ্টা করো বা অন্য ভিডিও দাও।")

        # প্রতিটা ক্লিপ আলাদা ভিডিও বানাই (আকার + ক্যাপশন + হুক টাইটেল সহ)
        for k, c in enumerate(clips, 1):
            job["step"] = f"ক্লিপ ভিডিও বানানো হচ্ছে... ({k}/{len(clips)})"
            try:
                c["file"] = render_single_clip(job_id, c, video_path, duration, opts, segments)
            except Exception:
                c["file"] = ""
        job["clips"] = clips

        job["step"] = "রিক্যাপ সাজানো হচ্ছে..."
        job["recaps"] = make_recaps(clips, num_recaps, recap_sec)

        job["step"] = "পোস্ট ক্যাপশন লেখা হচ্ছে..."
        job["post"] = gen_post_copy(clips)

        job.update(status="done", step="রেডি! প্যানেলে দেখো।")
    except Exception as ex:
        job.update(status="error", step="সমস্যা হয়েছে", error=str(ex))


# ==================== রাউট ====================

@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/upload", methods=["POST"])
def api_upload():
    file = request.files.get("video")
    if not file:
        return jsonify(error="কোনো ভিডিও পাওয়া যায়নি"), 400

    target_clips = int(request.form.get("num_clips", 20))
    num_recaps = int(request.form.get("num_recaps", 10))
    recap_sec = int(request.form.get("recap_sec", RECAP_TARGET_SEC))
    clip_min = int(request.form.get("clip_min", CLIP_MIN_DEFAULT))
    clip_max = int(request.form.get("clip_max", CLIP_MAX_DEFAULT))
    aspect = request.form.get("aspect", "original")   # ডিফল্ট: আসল আকার, রিফ্রেম নেই
    if aspect != "original" and aspect not in ASPECTS:
        aspect = "original"
    opts = {
        "aspect": aspect,
        "clip_min": clip_min,
        "clip_max": clip_max,
        "captions": request.form.get("captions", "0") == "1",
        "hook_title": request.form.get("hook_title", "0") == "1",
    }

    job_id = uuid.uuid4().hex[:8]
    ext = Path(file.filename).suffix or ".mp4"
    video_path = UPLOAD_DIR / f"{job_id}{ext}"
    file.save(str(video_path))

    JOBS[job_id] = {
        "status": "running", "step": "শুরু হচ্ছে...",
        "video_path": str(video_path), "duration": get_duration(video_path),
        "clips": [], "recaps": [], "segments": [], "post": {},
        "opts": opts, "error": "",
    }
    threading.Thread(
        target=process_video,
        args=(job_id, target_clips, num_recaps, recap_sec, opts),
        daemon=True,
    ).start()
    return jsonify(job_id=job_id)


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="job পাওয়া যায়নি"), 404
    return jsonify(
        status=job["status"], step=job["step"], error=job["error"],
        clips=job["clips"], recaps=job["recaps"],
        post=job.get("post", {}), aspect=job.get("opts", {}).get("aspect", "original"),
    )


@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.get_json(force=True)
    job_id = data["job_id"]
    clip_ids = data["clip_ids"]
    name = data.get("name", f"recap_{uuid.uuid4().hex[:4]}")
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="job পাওয়া যায়নি"), 404
    if not clip_ids:
        return jsonify(error="কোনো ক্লিপ নেই"), 400
    try:
        out_path = render_recap(job_id, clip_ids, name)
        return jsonify(ok=True, file=out_path.name)
    except Exception as ex:
        return jsonify(error=str(ex)), 500


@app.route("/output/<path:fname>")
def output_file(fname):
    return send_from_directory(OUTPUT_DIR, fname)


@app.route("/api/version")
def api_version():
    return jsonify(version=current_version())


@app.route("/api/update", methods=["POST"])
def api_update():
    """পাবলিক রিপো থেকে লেটেস্ট ফাইল নামিয়ে অ্যাপ আপডেট করা।"""
    import urllib.request
    if getattr(sys, "frozen", False):
        return jsonify(error=".exe ভার্সন এভাবে আপডেট হয় না। START ভার্সন ব্যবহার করো, "
                             "অথবা আবার MAKE_APP চালাও।"), 400
    updated, old = [], current_version()
    try:
        for rel in UPDATE_FILES:
            url = RAW_BASE + rel + "?t=" + uuid.uuid4().hex
            dst = BASE_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "RecapStudio"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            dst.write_bytes(data)
            updated.append(rel)
        return jsonify(ok=True, old=old, version=current_version(), files=updated)
    except Exception as ex:
        return jsonify(error=str(ex), files=updated), 500


def _run_server():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    import time
    url = "http://127.0.0.1:5000"
    threading.Thread(target=_run_server, daemon=True).start()
    time.sleep(1.0)
    # চেষ্টা করি নিজস্ব অ্যাপ উইন্ডোতে খুলতে (Adobe-এর মতো), না পারলে ব্রাউজারে
    try:
        import webview
        webview.create_window("Recap Studio", url, width=1240, height=820,
                              min_size=(900, 600))
        webview.start()
    except Exception:
        import webbrowser
        print("\n  Recap Studio চলছে:  " + url + "\n  (বন্ধ করতে এই উইন্ডো বন্ধ করো)\n")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
