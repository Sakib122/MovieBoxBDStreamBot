"""
🎬 MovieBoxBD — ফাইলের ভেতরে ব্র্যান্ডিং (বার্ন-ইন) সিস্টেম
==========================================================
প্রতিটা নতুন মুভি বিন-চ্যানেলে ঢুকলে ব্যাকগ্রাউন্ডে একটা কাজ (job) পাইলে যায়:

  ১. টেলিগ্রাম থেকে পুরো ভিডিও নামানো হয়
  ২. ffmpeg দিয়ে মুভির শুরুতে ইন্ট্রো কার্ড বসে (থাম্বনেইল + বড় ব্র্যান্ড-লেখা
     + আপনার ভয়েস: "This video is downloaded from MovieBoxBD.xyz")
  ৩. পুরো ভিডিওজুড়ে প্রতি ১২ মিনিটে ৫ সেকেন্ড করে ছোট "MovieBoxBD.xyz"
     ওয়াটারমার্ক ভাসে (ডান-নিচে) — মুভির পিক্সেলেই বার্ন করা
  ৪. ব্র্যান্ডেড mp4 আবার বিন-চ্যানেলে আপলোড হয়, ম্যাপিং MongoDB-তে জমে

এরপর থেকে ওই মুভির যেকোনো পুরনো স্ট্রিম/ডাউনলোড লিংক স্বয়ংক্রিয়ভাবে
ব্র্যান্ডেড ফাইল সার্ভ করে — ডাউনলোড করে অফলাইনে (MX Player/VLC)
দেখলেও ব্র্যান্ড থাকে। প্রসেসিং চলাকালীন আসল ফাইলই সার্ভ হতে থাকে —
কোনো লিংক কখনো ভাঙে না।

সেলফ-টেস্ট (টেলিগ্রাম ছাড়া লোকাল টেস্ট):
    python3 branding.py selftest input.mp4 output.mp4 \
        --voice assets/brand_voice.mp3 --interval-min 0.1 --show 5
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from functools import partial

from info import (
    BRAND_ENABLED, BRAND_TEXT, BRAND_VOICE_PATH, BRAND_FONT_PATH,
    BRAND_INTERVAL_MIN, BRAND_SHOW_SEC, BRAND_PRESET, BRAND_CRF,
    BRAND_MAX_HEIGHT, BRAND_WORKERS, BRAND_MIN_SIZE_MB, BRAND_MAX_SIZE_GB,
    BRAND_TMP_DIR, BIN_CHANNEL, BRAND_NICE,
)
from database.users_db import db

logger = logging.getLogger("branding")


# ───────────────────────── প্রোব/হেল্পার ─────────────────────────

def probe_media(path: str, need_video: bool = True) -> dict:
    """ffprobe দিয়ে রেজোলিউশন/ফ্রেমরেট/SAR/দৈর্ঘ্য/অডিও বের করে।
    need_video=False দিলে অডিও-ফাইল (ভয়েস ক্লিপ) প্রোব করা যায়।"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe ব্যর্থ: {(out.stderr or '')[:300]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None and need_video:
        raise RuntimeError("ভিডিও স্ট্রিম নেই")

    def _frac(s, default):
        try:
            num, den = str(s).split("/")
            f = float(num) / float(den)
            return f if f > 0 else default
        except Exception:
            return default

    fps = _frac((v or {}).get("avg_frame_rate") or (v or {}).get("r_frame_rate"), 25.0)
    sar_raw = ((v or {}).get("sample_aspect_ratio") or "1:1").replace(":", "/")
    if sar_raw in ("N/A", "0/1", ""):
        sar_raw = "1/1"
    dur = float((data.get("format") or {}).get("duration") or (v or a or {}).get("duration") or 0)
    return {
        "width": int((v or {}).get("width", 0) or 0),
        "height": int((v or {}).get("height", 0) or 0),
        "fps": fps,
        "fps_str": (v or {}).get("avg_frame_rate") or (v or {}).get("r_frame_rate") or "25/1",
        "sar": sar_raw,
        "duration": dur,
        "has_audio": a is not None,
        # ফাস্ট-মোডের কোডেক-যাচাইয়ের জন্য
        "vcodec": (v or {}).get("codec_name"),
        "acodec": (a or {}).get("codec_name"),
        "pix_fmt": (v or {}).get("pix_fmt"),
        "sample_rate": int((a or {}).get("sample_rate", 0) or 0),
        "channels": int((a or {}).get("channels", 0) or 0),
    }


def _escape_drawtext(t: str) -> str:
    """drawtext-এর টেক্সট-এস্কেপ (কোলন/অ্যাপস্ট্রফি/কমা/পার্সেন্ট)"""
    return (t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
             .replace(",", "\\,").replace("%", "\\%"))


def _run(cmd, tag: str):
    """ffmpeg চালায় — স্ট্রিমকে সবসময় আগে পাওয়াতে সবচেয়ে কম CPU-প্রায়োরিটিতে
    (nice 19) চালানো হয়, যেন মুভি এনকোড হতে থাকলেও স্ট্রিমিং স্মুথ থাকে।
    ব্যর্থ হলে শেষ ২০ লাইন এররসহ ব্যতিক্রম।"""
    if BRAND_NICE:
        cmd = ["nice", "-n", "19", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise RuntimeError(f"ffmpeg [{tag}] ব্যর্থ:\n{tail}")


def _as_int(v, default):
    """স্ট্রিং/বাজে মান এলেও ভেঙে না পড়ে ইন্ট ফেরত দেয় (env ভুল হলেও সেফ)"""
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_float(v, default):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ───────────────────────── কোর পাইপলাইন (টেলিগ্রাম-মুক্ত) ─────────────────────────

def process_video(in_path, out_path, voice_path=None, text=None, interval_min=None,
                  show_sec=None, preset=None, crf=None, max_height=None,
                  thumb_path=None, font_path=None) -> dict:
    """ইনপুট ভিডিওর শুরুতে ইন্ট্রো কার্ড+ভয়েস জুড়ে দেয় এবং পুরো ভিডিওজুড়ে
    পিরিয়ডিক ওয়াটারমার্ক বার্ন করে। থ্রেড-সেফ পিওর ফাংশন — worker এটাকে
    run_in_executor দিয়ে চালায়।"""
    os.makedirs(BRAND_TMP_DIR, exist_ok=True)
    voice_path = voice_path or BRAND_VOICE_PATH
    text = _escape_drawtext(text or BRAND_TEXT)
    interval_min = _as_float(interval_min, BRAND_INTERVAL_MIN)
    show_sec = _as_int(show_sec, BRAND_SHOW_SEC)
    preset = preset or BRAND_PRESET
    crf = _as_int(crf, BRAND_CRF)
    max_height = _as_int(max_height, BRAND_MAX_HEIGHT)
    font_path = font_path or BRAND_FONT_PATH

    if not os.path.exists(font_path):
        raise RuntimeError(f"ফন্ট ফাইল নেই: {font_path} (assets/DejaVuSans-Bold.ttf রাখুন)")

    m = probe_media(in_path)
    W, H = m["width"], m["height"]
    # উচ্চতার ক্যাপ: 1080p-র চেয়ে বড় হলে ছোট করে এনকোড (সময়/সাইজ বাঁচে)
    scale_part = ""
    if H > max_height:
        h2 = max_height - (max_height % 2)
        w2 = int(round(W * h2 / H / 2)) * 2
        scale_part = f",scale={w2}:{h2}"
        W, H = w2, h2
    fps_str = m["fps_str"]
    sar = m["sar"]

    # ভয়েসের দৈর্ঘ্য → ইন্ট্রোর দৈর্ঘ্য (ভয়েস না থাকলেও ৫ সেকেন্ডের কার্ড দেখাবে)
    voice = None
    if voice_path and os.path.exists(voice_path):
        try:
            voice = probe_media(voice_path, need_video=False)
        except Exception as e:
            logger.warning(f"ভয়েস ফাইল প্রোব ব্যর্থ: {e}")
    intro_dur = round((voice["duration"] + 0.8) if (voice and voice.get("duration")) else 5.0, 2)

    tmp = tempfile.mkdtemp(dir=BRAND_TMP_DIR, prefix="pipe_")
    intro_path = os.path.join(tmp, "intro.mp4")
    try:
        # ── ১) ইন্ট্রো কার্ড: থাম্বনেইল (না থাকলে কালো ক্যানভাস) + বড় ব্র্যান্ড-লেখা + ভয়েস
        center_text = (f"drawtext=fontfile={font_path}:text='{text}':"
                       f"fontsize={max(28, H // 12)}:fontcolor=white:box=1:"
                       f"boxcolor=black@0.45:boxborderw=18:"
                       f"x=(w-tw)/2:y=(h-th)/2")
        fades = f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, intro_dur - 0.6)}:d=0.6"
        if thumb_path and os.path.exists(thumb_path):
            vsrc = ["-loop", "1", "-framerate", "25", "-i", thumb_path]
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar={sar},"
                  f"{center_text},{fades}")
        else:
            vsrc = ["-f", "lavfi", "-i", f"color=c=0x0a0a12:s={W}x{H}:d={intro_dur}"]
            vf = f"setsar={sar},{center_text},{fades}"
        if voice:
            a_in = ["-i", voice_path]
            af = f"[1:a]aresample=44100,apad=whole_dur={intro_dur},aformat=sample_rates=44100:channel_layouts=stereo[a]"
        else:
            a_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            af = f"[1:a]atrim=0:{intro_dur},aformat=sample_rates=44100:channel_layouts=stereo[a]"
        _run(["ffmpeg", "-y", "-v", "error", *vsrc, *a_in,
              "-filter_complex", f"[0:v]{vf}[v];{af}",
              "-map", "[v]", "-map", "[a]", "-t", str(intro_dur),
              "-r", fps_str, "-c:v", "libx264", "-preset", preset, "-crf", "20",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
              intro_path], "intro")

        # ── ২) ইন্ট্রো + মুভি জোড়া + পিরিয়ডিক ওয়াটারমার্ক বার্ন
        # ওয়াটারমার্ক: হলুদ (গোল্ড), হালকা, আর প্রতি বার ভিন্ন জায়গায় —
        # মাঝখানে → নিচ-ডান → নিচ-বাম → উপর-ডান ঘুরে ফিরে আসে
        # (কেউ এক কোণ ক্রপ করে ব্র্যান্ড সরাতে পারবে না)।
        period = max(1.0, float(interval_min) * 60.0)
        spots = [
            ("(w-tw)/2", "(h-th)/2"),   # মাঝখানে
            ("w-tw-30",  "h-th-30"),    # নিচ-ডান কোণ
            ("30",       "h-th-30"),    # নিচ-বাম কোণ
            ("w-tw-30",  "30"),         # উপর-ডান কোণ
        ]
        marks = []
        for i, (px, py) in enumerate(spots):
            # এই দাগে কেবল i-তম পজিশন-টার্নে দেখাবে: floor(t/period) % 4 == i
            enable = "if(eq(mod(floor(t/%g)\\,%d)\\,%d)\\,lt(mod(t\\,%g)\\,%d)\\,0)" % (
                period, len(spots), i, period, int(show_sec))
            marks.append(
                f"drawtext=fontfile={font_path}:text='{text}':"
                f"fontsize={max(14, H // 30)}:fontcolor=0xFFD700@0.9:box=1:"
                f"boxcolor=black@0.25:boxborderw=8:"
                f"x={px}:y={py}:enable='{enable}'")
        mark = ",".join(marks)
        if m["has_audio"]:
            ma_in = []
            ma_lbl = "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[ma]"
        else:
            # মুভিতে অডিও না থাকলে নীরব ট্র্যাক বানিয়ে দেয় (concat-এর জন্য লাগবেই)
            ma_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            ma_lbl = f"[2:a]atrim=0:{m['duration']},aformat=sample_rates=44100:channel_layouts=stereo[ma]"
        fc = (
            f"[1:v]fps={fps_str},setsar={sar}{scale_part}[mv];"
            f"{ma_lbl};"
            f"[0:v][0:a][mv][ma]concat=n=2:v=1:a=1[catv][cata];"
            f"[catv]{mark}[vt];"
            f"[cata]aformat=sample_rates=44100:channel_layouts=stereo[at]"
        )
        _run(["ffmpeg", "-y", "-v", "error", "-i", intro_path, "-i", in_path, *ma_in,
              "-filter_complex", fc,
              "-map", "[vt]", "-map", "[at]",
              "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
              "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "160k", "-ac", "2",
              "-movflags", "+faststart", out_path], "main")

        # ── ৩) যাচাই: আউটপুট দৈর্ঘ্য = ইন্ট্রো + মুভি (± ২% বা ৩ সেকেন্ড)
        out = probe_media(out_path)
        want = m["duration"] + intro_dur
        if abs(out["duration"] - want) > max(3.0, want * 0.02):
            raise RuntimeError(f"দৈর্ঘ্য মেলেনি: আউটপুট {out['duration']:.1f}s, আশা {want:.1f}s")
        return {"in": m, "out": out, "intro_dur": intro_dur}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────── টেলিগ্রাম র‍্যাপার + কিউ ─────────────────────────

_queue: asyncio.Queue = None
_worker_task = None


def _q():
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def _ensure_workers():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop())
        asyncio.create_task(_requeue_pending())
        logger.info(f"🎬 ব্র্যান্ডিং ওয়ার্কার চালু হলো ({BRAND_WORKERS} টা)")


# ───────────────────────── ⚡ ফাস্ট মোড (রি-এনকোড ছাড়া) ─────────────────────────

def fast_concat_video(in_path, out_path, thumb_path=None, voice_path=None, text=None):
    """⚡ ফাস্ট ব্র্যান্ডিং: শুরুতে ইন্ট্রো কার্ড + ভয়েস জোড়া, মুভি কপি-হয় (রি-এনকোড নেই)।

    - সার্ভারের CPU প্রায় লাগে না (শুধু ~৫ সেকেন্ডের ইন্ট্রো এনকোড + দ্রুত কপি)
    - মুভির কোয়ালিটি ১০০% অক্ষত (একটা বিটও বদলায় না)
    - পিক্সেলে ওয়াটারমার্ক বার্ন হয় না — সেটা শুধু ফুল মোডে

    ফাস্ট-কনক্যাট শুধু তখনই নিরাপদ যখন মুভি h264 + aac + yuv420p এবং
    অডিও mono/stereo — না হলে None ফেরত (কলার ফুল মোডে fallback করবে)।
    """
    m = probe_media(in_path)
    ok = (m["vcodec"] == "h264" and m["pix_fmt"] == "yuv420p"
          and m["has_audio"] and m["acodec"] == "aac"
          and m["channels"] in (1, 2) and 0 < m["sample_rate"] <= 96000
          and m["width"] and m["height"] and m["duration"] > 1)
    if not ok:
        return None

    text = _escape_drawtext(text or BRAND_TEXT)
    font_path = BRAND_FONT_PATH
    if not os.path.exists(font_path):
        raise RuntimeError(f"ফন্ট ফাইল নেই: {font_path} (assets/DejaVuSans-Bold.ttf রাখুন)")
    voice_path = voice_path or BRAND_VOICE_PATH
    voice = probe_media(voice_path, need_video=False) if (
        voice_path and os.path.exists(voice_path)) else None
    intro_dur = round((voice["duration"] + 0.8) if (voice and voice.get("duration")) else 5.0, 2)

    W, H = m["width"], m["height"]
    fps_str, sar = m["fps_str"], m["sar"]
    layout = "mono" if m["channels"] == 1 else "stereo"
    sr = m["sample_rate"]

    tmp = tempfile.mkdtemp(dir=BRAND_TMP_DIR, prefix="fast_")
    intro_path = os.path.join(tmp, "intro.mp4")
    list_path = os.path.join(tmp, "list.txt")
    try:
        center_text = (f"drawtext=fontfile={font_path}:text='{text}':"
                       f"fontsize={max(28, H // 12)}:fontcolor=white:box=1:"
                       f"boxcolor=black@0.45:boxborderw=18:"
                       f"x=(w-tw)/2:y=(h-th)/2")
        fades = f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, intro_dur - 0.6)}:d=0.6"
        if thumb_path and os.path.exists(thumb_path):
            vsrc = ["-loop", "1", "-framerate", "25", "-i", thumb_path]
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar={sar},"
                  f"{center_text},{fades}")
        else:
            vsrc = ["-f", "lavfi", "-i", f"color=c=0x0a0a12:s={W}x{H}:d={intro_dur}"]
            vf = f"setsar={sar},{center_text},{fades}"
        if voice:
            a_in = ["-i", voice_path]
            af = (f"[1:a]aresample={sr},apad=whole_dur={intro_dur},"
                  f"aformat=sample_rates={sr}:channel_layouts={layout}[a]")
        else:
            a_in = ["-f", "lavfi", "-i", f"anullsrc=channel_layout={layout}:sample_rate={sr}"]
            af = f"[1:a]atrim=0:{intro_dur},aformat=sample_rates={sr}:channel_layouts={layout}[a]"
        # ইন্ট্রো মুভির কোডেক-প্যারামের হুবহু কপি হতে হবে — নাহলে -c copy জোড়া ভাঙবে
        _run(["ffmpeg", "-y", "-v", "error", *vsrc, *a_in,
              "-filter_complex", f"[0:v]{vf}[v];{af}",
              "-map", "[v]", "-map", "[a]", "-t", str(intro_dur),
              "-r", fps_str, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
              "-ac", str(m["channels"]), "-ar", str(sr),
              intro_path], "fast-intro")

        with open(list_path, "w", encoding="utf-8") as f:
            f.write(f"file '{intro_path}'\nfile '{os.path.abspath(in_path)}'\n")
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", list_path, "-c", "copy", "-movflags", "+faststart",
              out_path], "fast-concat")

        out = probe_media(out_path)
        want = m["duration"] + intro_dur
        if abs(out["duration"] - want) > max(3.0, want * 0.02):
            raise RuntimeError(f"ফাস্ট-কনক্যাট যাচাই ব্যর্থ ({out['duration']:.1f}s != {want:.1f}s)")
        logger.info(f"⚡ ফাস্ট-ব্র্যান্ড শেষ: ইন্ট্রো {intro_dur}s + মুভি {m['duration'] / 60:.1f} মিনিট (কপি)")
        return {"out": out, "intro_dur": intro_dur, "mode": "fast"}
    except Exception:
        logger.exception("ফাস্ট-কনক্যাট ব্যর্থ — ফুল মোডে fallback করা হবে")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def enqueue(orig_id):
    """নতুন ফাইল এলে কাজ লাইনে বসায়। লিংক-ফ্লো কখনো ভাঙবে না — সব সমস্যা
    চুপচাপ লগে যায়, আসল ফাইলই ততক্ষণ সার্ভ হতে থাকে।"""
    if not BRAND_ENABLED:
        return
    try:
        asyncio.create_task(_enqueue_later(int(orig_id)))
    except Exception as e:
        logger.warning(f"enqueue ব্যর্থ #{orig_id}: {e}")


async def _enqueue_later(orig_id):
    try:
        _ensure_workers()
        doc = await db.branded_files.find_one({"orig_id": orig_id})
        if doc and doc.get("status") in ("queued", "processing", "done"):
            return
        await db.branded_files.update_one(
            {"orig_id": orig_id},
            {"$set": {"status": "queued", "ts": time.time(), "tries": 0}},
            upsert=True,
        )
        await _q().put(orig_id)
        logger.info(f"🎬 ব্র্যান্ডিং কাতারে বসলো bin#{orig_id}")
    except Exception as e:
        logger.warning(f"enqueue-later ব্যর্থ #{orig_id}: {e}")


async def _requeue_pending():
    """রিস্টার্টে কাতার/প্রসেসিং মাঝপথে মরে গেলে আবার তুলে নেয়"""
    try:
        cutoff = time.time() - 45 * 60   # এর চেয়ে তাজা 'processing' = সত্যিই চলছে
        cursor = db.branded_files.find({"status": {"$in": ["queued", "processing"]}})
        async for doc in cursor:
            if doc.get("status") == "processing" and doc.get("ts", 0) > cutoff:
                continue
            await _q().put(doc["orig_id"])
    except Exception as e:
        logger.warning(f"pending requeue ব্যর্থ: {e}")


async def _worker_loop():
    while True:
        orig_id = await _q().get()
        try:
            await _process_one(orig_id)
        except Exception as e:
            logger.error(f"❌ ব্র্যান্ডিং ব্যর্থ bin#{orig_id}: {e}")
            try:
                await db.branded_files.update_one(
                    {"orig_id": orig_id},
                    {"$set": {"status": "failed", "error": str(e)[:500], "ts": time.time()}})
            except Exception:
                pass
        finally:
            _q().task_done()


def _is_branded(status):
    return status == "done"


async def _process_one(orig_id):
    from web.bot import WebavBot                      # লেজি ইমপোর্ট (সার্কুলার এড়াতে)
    from web.utils.file_properties import get_hash, get_media_from_message
    doc = await db.branded_files.find_one({"orig_id": orig_id})
    if doc and _is_branded(doc.get("status")):
        return
    msg = await WebavBot.get_messages(int(BIN_CHANNEL), orig_id)
    if not msg or msg.empty:
        raise RuntimeError("bin মেসেজ পাওয়া যায়নি (মুছে ফেলা হয়েছে?)")
    media = get_media_from_message(msg)
    if media is None:
        await db.branded_files.update_one(
            {"orig_id": orig_id}, {"$set": {"status": "skipped_no_media", "ts": time.time()}})
        return
    size = getattr(media, "file_size", 0) or 0
    if size < BRAND_MIN_SIZE_MB * 1024 * 1024:
        await db.branded_files.update_one(
            {"orig_id": orig_id}, {"$set": {"status": "skipped_small", "ts": time.time()}})
        return
    if size > BRAND_MAX_SIZE_GB * 1024 * 1024 * 1024:
        await db.branded_files.update_one(
            {"orig_id": orig_id},
            {"$set": {"status": "skipped_large",
                      "error": f"{size / 1e9:.2f}GB > BRAND_MAX_SIZE_GB={BRAND_MAX_SIZE_GB}",
                      "ts": time.time()}})
        logger.warning(f"⏭️ bin#{orig_id} বড় বেশি ({size / 1e9:.2f}GB) — স্কিপ")
        return

    orig_hash = get_hash(msg)
    await db.branded_files.update_one(
        {"orig_id": orig_id},
        {"$set": {"status": "processing", "orig_hash": orig_hash,
                  "size": size, "ts": time.time()}})

    os.makedirs(BRAND_TMP_DIR, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=BRAND_TMP_DIR, prefix=f"file_{orig_id}_")
    in_path = os.path.join(workdir, "input.bin")
    out_path = os.path.join(workdir, "branded.mp4")
    thumb_path = None
    try:
        # থাম্বনেইল নামাও (ইন্ট্রো কার্ডের ব্যাকগ্রাউন্ড)
        thumbs = getattr(media, "thumbs", None) or []
        if thumbs:
            tp = os.path.join(workdir, "thumb.jpg")
            try:
                await WebavBot.download_media(thumbs[-1], file_name=tp)
                thumb_path = tp
            except Exception as e:
                logger.warning(f"bin#{orig_id}: থাম্ব নামা গেল না ({e}) — কালো কার্ড ব্যবহার হবে")

        # পুরো ভিডিও নামানো
        t0 = time.time()
        await WebavBot.download_media(msg, file_name=in_path)
        logger.info(f"📥 bin#{orig_id}: {size / 1e6:.0f}MB নামলো {time.time() - t0:.0f}s")

        # ডিস্ক-জায়গা: ইনপুট + আউটপুট একসাথে থাকতে হবে
        free = shutil.disk_usage(workdir).free
        if free < size * 1.3:
            raise RuntimeError(
                f"ডিস্কে জায়গা নেই ({free / 1e9:.1f}GB ফ্রি, দরকার ~{size * 1.3 / 1e9:.1f}GB)")

        # ffmpeg — এক্সিকিউটর থ্রেডে (ইভেন্ট-লুপ আটকাবে না)
        # ⚠️ partial + কীওয়ার্ড-আর্গ: পজিশনালে পাঠালে এক ঘর সরে গিয়ে
        # max_height-এ থাম্ব-পাথ (স্ট্রিং) বসে যায় — সেই বাগই ছিল!
        result = await asyncio.get_running_loop().run_in_executor(
            None, partial(process_video, thumb_path=thumb_path),
            in_path, out_path)
        o = result["out"]
        logger.info(f"🎬 bin#{orig_id}: বার্ন শেষ (ইন্ট্রো {result['intro_dur']:.1f}s, মোট {o['duration'] / 60:.1f} মিনিট)")

        # আপলোড — ক্যাপশন/নাম হুবহু আগেরটাই (পেজের নাম যেন না বদলায়)
        cap = getattr(msg, "caption", None) or ""
        f_name = getattr(media, "file_name", None) or f"Movie_{orig_id}.mp4"
        base, ext = os.path.splitext(f_name)
        if ext.lower() != ".mp4":
            f_name = base + ".mp4"
        t0 = time.time()
        kwargs = dict(
            chat_id=int(BIN_CHANNEL), video=out_path, caption=cap,
            file_name=f_name, supports_streaming=True,
            duration=int(o["duration"]), width=o["width"], height=o["height"],
        )
        if thumb_path:
            kwargs["thumb"] = thumb_path
        branded = await WebavBot.send_video(**kwargs)
        await db.branded_files.update_one(
            {"orig_id": orig_id},
            {"$set": {"status": "done", "branded_id": branded.id,
                      "branded_hash": get_hash(branded), "ts": time.time(),
                      "proc_sec": round(time.time() - t0)}})
        logger.info(f"✅ bin#{orig_id} ব্র্যান্ডেড → bin#{branded.id} (আপলোড {time.time() - t0:.0f}s)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ───────────────────────── সার্ভিং-সোয়াপ (লিংক সবসময় কাজ করে) ─────────────────────────

async def resolve(orig_id, orig_hash):
    """(আইডি, হ্যাশ) → ব্র্যান্ডেড প্রস্তুত থাকলে (ব্র্যান্ডেড_আইডি, ব্র্যান্ডেড_হ্যাশ, True)।
    আসল হ্যাশ মিললেই কেবল সোয়াপ হয় — লিংক জালিয়াতি ঠেকাতে।"""
    try:
        if BRAND_ENABLED and orig_hash:
            doc = await db.branded_files.find_one({"orig_id": int(orig_id), "status": "done"})
            if doc and doc.get("orig_hash") == orig_hash and doc.get("branded_id"):
                return int(doc["branded_id"]), doc.get("branded_hash") or orig_hash, True
    except Exception as e:
        logger.warning(f"resolve ব্যর্থ #{orig_id}: {e}")
    return int(orig_id), orig_hash, False


async def swap_id(orig_id):
    """'GET FILE' টেলিগ্রাম-কপি পথের জন্য: ব্র্যান্ডেড থাকলে তার আইডি"""
    try:
        if BRAND_ENABLED:
            doc = await db.branded_files.find_one({"orig_id": int(orig_id), "status": "done"})
            if doc and doc.get("branded_id"):
                return int(doc["branded_id"])
    except Exception as e:
        logger.warning(f"swap_id ব্যর্থ #{orig_id}: {e}")
    return int(orig_id)


# ───────────────────────── লোকাল সেলফ-টেস্ট ─────────────────────────

def _selftest():
    import sys
    # `python3 branding.py selftest input output ...` — 'selftest' শব্দটা খেয়ে ফেলো
    if sys.argv[1:2] == ["selftest"]:
        sys.argv.pop(1)
    ap = argparse.ArgumentParser(description="ব্র্যান্ডিং পাইপলাইন লোকাল টেস্ট (টেলিগ্রাম ছাড়া)")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--text", default=None)
    ap.add_argument("--interval-min", type=float, default=None)
    ap.add_argument("--show", type=int, default=None)
    ap.add_argument("--preset", default=None)
    ap.add_argument("--crf", type=int, default=None)
    ap.add_argument("--max-height", type=int, default=None)
    ap.add_argument("--thumb", default=None)
    ap.add_argument("--font", default=None)
    a = ap.parse_args()
    info = process_video(a.input, a.output, a.voice, a.text, a.interval_min,
                         a.show, a.preset, a.crf, a.max_height, a.thumb, a.font)
    slim = {
        "input": {k: info["in"][k] for k in ("width", "height", "fps", "duration", "has_audio")},
        "output": {k: info["out"][k] for k in ("width", "height", "duration")},
        "intro_dur": info["intro_dur"],
    }
    print("✅ সেলফ-টেস্ট সফল:", json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _selftest()
