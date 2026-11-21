"""
🎬 /brand + ⚡ /brandfast + 🔗 /stream — অ্যাডমিন ফাইল-টুল (কোয়েবেই চলে)
================================================================
সিস্টেমগুলো আলাদা কমান্ডে (অ্যাডমিনদের জন্য; সাধারণ ইউজার আগের মতোই):

  /brand            → ফুল ব্র্যান্ডিং: নামানো → নাম/থাম্ব প্যানেল → ইন্ট্রো কার্ড+
                      ভয়েস+ঘুরন্ত হলুদ ওয়াটারমার্ক বার্ন → ব্র্যান্ডেড ফাইল ফেরত
  /brandfast        → ফাস্ট ব্র্যান্ডিং: ইন্ট্রো কার্ড+ভয়েস জোড়া, মুভি রি-এনকোড নেই —
                      CPU প্রায় শূন্য, মিনিটখানেকে শেষ (কোডেক মানলে; নাহলে ফুল মোড)
  /stream           → স্ট্রিম মোড: ফাইল দিলে স্ট্রিম লিংক+পেজ (আগের সিস্টেম)
  কমান্ড ছাড়া ফাইল  → বট হিন্ট দেবে, অটো-স্ট্রিম আর হবে না (অ্যাডমিনের ক্ষেত্রে)

নোট:
  • ব্র্যান্ড-মোডগুলো স্ট্রিম/লিংক-সোয়াপ পাইপলাইনের বাইরে — পুরনো স্ট্রিম অক্ষত
  • ffmpeg nice 19-এ চলে — স্ট্রিম সবসময় CPU-তে আগে পাবে
  • টেলিগ্রাম বট-অ্যাকাউন্টের হার্ড লিমিট: ডাউনলোড/আপলোড সর্বোচ্চ 2 GB
"""
import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from functools import partial

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

from info import ADMINS, BRAND_INTERVAL_MIN, BRAND_SHOW_SEC, BRAND_TMP_DIR
from branding import probe_media, process_video, fast_concat_video

logger = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2
BOT_LIMIT_GB = 2.0        # টেলিগ্রাম বট-অ্যাকাউন্টের হার্ড লিমিট
AWAIT_TTL = 600           # /brand বা /stream দেওয়ার পর কত সেকেন্ডের ভেতরে ফাইল পাঠাতে হবে
QUEUE_MAX = 3             # একসাথে কিউতে সর্বোচ্চ কয়টা ফাইল

HINT_TEXT = (
    "⚠️ এখন সরাসরি ফাইল দিলে আর কিছু হয় না — সিস্টেম কমান্ডে আলাদা করা হয়েছে:\n\n"
    "🔗 **স্ট্রিম লিংক** চাইলে: আগে `/stream` লিখো → তারপর ফাইল পাঠাও\n"
    "🎬 **ব্র্যান্ডেড ফাইল** চাইলে: আগে `/brand` লিখো → তারপর ফাইল পাঠাও\n"
    "⚡ **ফাস্ট ব্র্যান্ড** চাইলে: আগে `/brandfast` লিখো → তারপর ফাইল পাঠাও")

STATE: dict = {}          # user_id → {"stage": "file", "exp": ts}
JOBS: dict = {}           # "user_id:msg_id" → জব ডিকশনারি
QUEUE: asyncio.Queue = None
_POS = {"n": 0}


# ───────────────────────── হেল্পার ─────────────────────────

def _size(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= GB else f"{n / MB:.2f} MB"


def _dur(sec: float) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s"))


def _bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "█" * filled + "─" * (width - filled) + f"] {pct:.1f}%"


def _make_prog(msg, prefix: str):
    """ডাউনলোড/আপলোড প্রোগ্রেস — সাধারণ async def ক্লোজার।

    🐞 আগে ক্লাস-ইনস্ট্যান্স ব্যবহার করেছিলাম — pyrogram-এর
    `inspect.iscoroutinefunction(progress)` চেক তাতে False হয়, ফলে
    callback কখনো await হতো না এবং বার ০%-এই আটকে থাকত। সাধারণ
    async def ফাংশন হলে pyrogram সরাসরি await করে — % দেখা যায়।
    প্রতি ২ সেকেন্ডে আপডেট; শুরুর আপডেট সাথে সাথে, শেষে ১০০% সবসময়।
    """
    t0 = time.time()
    last = {"t": 0.0, "cur": 0, "text": ""}

    async def prog(current: int, total: int):
        now = time.time()
        if not (total and current >= total) and now - last["t"] < 2:
            return
        dt = (now - last["t"]) if last["t"] else max(0.1, now - t0)
        speed = max(0, (current - last["cur"]) / dt) if last["t"] else 0
        last["t"], last["cur"] = now, current
        pct = current / total * 100 if total else 0
        sp = f" | ⚡ {_size(speed)}/s" if speed else ""
        text = (f"{prefix}\n{_bar(pct)}\n"
                f"📦 সাইজ: {_size(current)} / {_size(total)}{sp}\n"
                f"⏱ লেগেছে: {_dur(now - t0)}")
        if text == last["text"]:
            return
        last["text"] = text
        try:
            await msg.edit(text)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            pass

    return prog


def _video_of(m):
    d = m.video or m.document
    if d is None:
        return None
    if m.document:
        mime = (m.document.mime_type or "")
        ext = os.path.splitext(m.document.file_name or "")[1].lower()
        if not (mime.startswith("video/") or
                ext in (".mkv", ".mp4", ".avi", ".webm", ".mov", ".ts")):
            return None
    return d


def _panel(job) -> str:
    mode_line = ("⚡ মোড: ফাস্ট (ইন্ট্রো+ভয়েস, মুভি রি-এনকোড নেই)"
                 if job.get("mode") == "fast"
                 else "🎬 মোড: ফুল (ঘুরন্ত হলুদ ওয়াটারমার্ক বার্নসহ)")
    thumb_state = ("🖼 থাম্বনেইল: তোমার দেওয়া ✅" if job.get("custom_thumb")
                   else ("🖼 থাম্বনেইল: ফাইলের নিজস্ব" if job.get("thumb_path")
                         else "🖼 থাম্বনেইল: নেই (ইন্ট্রো কার্ড কালো হবে)"))
    return ("✅ ডাউনলোড সফলভাবে হয়েছে!\n\n"
            f"{mode_line}\n"
            f"🎬 নাম: {job['name']}\n"
            f"⏱ সময়কাল: {_dur(job['duration'])}\n"
            f"📦 সাইজ: {_size(job['size'])}\n"
            f"{thumb_state}\n\n"
            "👇 বাটন দিয়ে নাম ও থাম্বনেইল বদলাতে পারো, তারপর Done চাপো")


def _buttons(mid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"bren:{mid}"),
         InlineKeyboardButton("🖼 Thumbnail", callback_data=f"bthu:{mid}")],
        [InlineKeyboardButton("❌ বাতিল", callback_data=f"bcan:{mid}")],
        [InlineKeyboardButton("🚀 Done (Upload Now)", callback_data=f"bgo:{mid}")],
    ])


def _ensure_worker():
    global QUEUE
    if QUEUE is None:
        QUEUE = asyncio.Queue()
        asyncio.create_task(_worker())


def _cleanup(key):
    job = JOBS.pop(key, None)
    if job:
        shutil.rmtree(os.path.dirname(job["path"]), ignore_errors=True)


# ───────────────────────── ওয়ার্কার (সিরিয়াল কিউ) ─────────────────────────

async def _worker():
    while True:
        key = await QUEUE.get()
        _POS["n"] = max(0, _POS["n"] - 1)
        job = JOBS.get(key)
        if not job or job.get("cancelled"):
            QUEUE.task_done()
            continue
        chat_id = int(key.split(":")[0])
        status = job["panel_msg"]
        workdir = os.path.dirname(job["path"])
        out_path = os.path.join(workdir, "branded.mp4")
        try:
            is_fast = job.get("mode") == "fast"
            if is_fast:
                await status.edit(
                    "⚡ ফাস্ট ব্র্যান্ডিং চলছে...\n\n"
                    f"🎬 নাম: {job['name']}\n"
                    "• শুরুতে ইন্ট্রো কার্ড + ভয়েস বসবে\n"
                    "• মুভি রি-এনকোড হবে না — কোয়ালিটি ১০০% অক্ষত\n\n"
                    "⏳ মিনিটখানেকের মধ্যেই শেষ হওয়ার কথা — বট বন্ধ করো না।")
            else:
                await status.edit(
                    "⚙️ ব্র্যান্ডিং চলছে...\n\n"
                    f"🎬 নাম: {job['name']}\n"
                    "• শুরুতে ইন্ট্রো কার্ড + ভয়েস বসবে\n"
                    f"• প্রতি {BRAND_INTERVAL_MIN:g} মিনিটে {BRAND_SHOW_SEC} সেকেন্ড "
                    "হলুদ ওয়াটারমার্ক — মাঝ → নিচ-ডান → নিচ-বাম → উপর-ডান ঘুরে\n\n"
                    "⏳ ফাইলের সাইজ অনুযায়ী সময় লাগবে (ছোট ফাইল মিনিটেই শেষ) —\n"
                    "বট বন্ধ করো না, শেষ হলে নিজেই আপলোড শুরু হবে।")
            t0 = time.time()
            free = shutil.disk_usage(workdir).free
            if free < job["size"] * 1.3:
                raise RuntimeError(f"ডিস্কে জায়গা নেই ({free / GB:.1f}GB ফ্রি, "
                                   f"দরকার ~{job['size'] * 1.3 / GB:.1f}GB)")
            loop = asyncio.get_running_loop()
            result = None
            if is_fast:
                result = await loop.run_in_executor(
                    None, partial(fast_concat_video, thumb_path=job.get("thumb_path")),
                    job["path"], out_path)
                if result is None:
                    await status.edit(
                        "ℹ️ এই ফাইলের কোডেক ফাস্ট-মোড সাপোর্ট করে না\n"
                        "(h264 + aac হতে হয়) — 🎬 নিরাপদ ফুল মোডে\n"
                        "(ঘুরন্ত হলুদ ওয়াটারমার্ক বার্নসহ) কাজ চলছে...")
                    is_fast = False
            if result is None:
                result = await loop.run_in_executor(
                    None, partial(process_video, thumb_path=job.get("thumb_path")),
                    job["path"], out_path)
            o = result["out"]
            logger.info(f"/brand শেষ {job['name']}: মোড {'ফাস্ট' if is_fast else 'ফুল'}, "
                        f"ইন্ট্রো {result['intro_dur']}s, মোট {o['duration'] / 60:.1f} মিনিট "
                        f"({time.time() - t0:.0f}s)")

            up_thumb = os.path.join(workdir, "up_thumb.jpg")
            try:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i",
                                out_path, "-frames:v", "1", "-vf", "scale=320:-2",
                                up_thumb], capture_output=True, check=True)
            except Exception:
                up_thumb = job.get("thumb_path") or ""
            await status.edit("📤 আপলোড হচ্ছে...\n" + _bar(0))
            sent = await job["client"].send_video(
                chat_id, out_path,
                file_name=job["name"], supports_streaming=True,
                duration=int(o["duration"]), width=o["width"], height=o["height"],
                thumb=up_thumb if up_thumb and os.path.exists(up_thumb) else None,
                caption=f"✅ ব্র্যান্ডেড: {job['name']}",
                progress=_make_prog(status, "📤 আপলোড হচ্ছে..."),
            )
            mins = (time.time() - t0) / 60
            if is_fast:
                detail = (f"• মোড: ⚡ ফাস্ট — ইন্ট্রো কার্ড + ভয়েস "
                          f"{result['intro_dur']:.1f} সেকেন্ড "
                          "(মুভি রি-এনকোড নেই, কোয়ালিটি অক্ষত)")
            else:
                detail = (f"• ইন্ট্রো কার্ড + ভয়েস: {result['intro_dur']:.1f} সেকেন্ড\n"
                          f"• হলুদ ওয়াটারমার্ক: প্রতি {BRAND_INTERVAL_MIN:g} মিনিটে "
                          f"{BRAND_SHOW_SEC} সেকেন্ড, ঘুরে ঘুরে ৪ জায়গায়")
            await status.edit(
                f"✅ সম্পন্ন! {job['name']}\n\n"
                f"{detail}\n"
                f"• মোট সময় লেগেছে: {mins:.0f} মিনিট\n\n"
                "📤 উপরের ভিডিওটা এখন যেখানে খুশি ফরওয়ার্ড/ব্যবহার করতে পারো 🎉")
        except Exception as e:
            logger.exception("/brand জব ব্যর্থ")
            try:
                await status.edit(f"❌ ব্র্যান্ডিং ব্যর্থ: {str(e)[:400]}\n\n"
                                  "আবার চেষ্টা করতে /brand দিয়ে ফাইল পাঠাও।")
            except Exception:
                pass
        finally:
            _cleanup(key)
            QUEUE.task_done()


# ───────────────────────── কমান্ড ─────────────────────────

@Client.on_message(filters.private & filters.command("brand") & filters.user(ADMINS))
async def brand_cmd(c, m):
    STATE[m.from_user.id] = {"stage": "file", "exp": time.time() + AWAIT_TTL}
    await m.reply(
        "🎬 **ব্র্যান্ডিং মোড চালু!**\n\n"
        "এখন মুভি/ভিডিও ফাইলটা পাঠাও (সর্বোচ্চ 2 GB)।\n\n"
        "বট যা করবে:\n"
        "1️⃣ নিজে নামাবে — লাইভ প্রোগ্রেস দেখাবে\n"
        "2️⃣ নাম ও থাম্বনেইল বদলানোর বাটন দেবে\n"
        "3️⃣ বার্ন করবে: ইন্ট্রো কার্ড + ভয়েস + ঘুরন্ত হলুদ ওয়াটারমার্ক\n"
        "4️⃣ ব্র্যান্ডেড ফাইল তোমাকে ফেরত দেবে — যেখানে খুশি ফরওয়ার্ড করো\n\n"
        "🔗 স্ট্রিম লিংক বানাতে চাইলে আলাদা কমান্ড: /stream\n"
        "⚡ ওয়াটারমার্ক ছাড়া ফাস্ট ব্র্যান্ড চাইলে: /brandfast\n"
        f"⏱ এই মোড বাতিল হবে {AWAIT_TTL // 60} মিনিট পর। বাতিল করতে /brandcancel")


@Client.on_message(filters.private & filters.command("brandfast") & filters.user(ADMINS))
async def brandfast_cmd(c, m):
    STATE[m.from_user.id] = {"stage": "fast", "exp": time.time() + AWAIT_TTL}
    await m.reply(
        "⚡ **ফাস্ট ব্র্যান্ডিং মোড চালু!**\n\n"
        "এখন মুভি/ভিডিও ফাইলটা পাঠাও (সর্বোচ্চ 2 GB)।\n\n"
        "এই মোডে:\n"
        "• শুরুতে ইন্ট্রো কার্ড + ভয়েস বসবে — ব্র্যান্ড দেখা যাবে\n"
        "• মুভি রি-এনকোড হবে না — কোয়ালিটি ১০০% অক্ষত, সার্ভারের ভার প্রায় শূন্য\n"
        "• যেকোনো সাইজের ফাইল মিনিটখানেকে শেষ\n\n"
        "⚠️ পিক্সেলে ওয়াটারমার্ক বার্ন হবে না — ঘুরন্ত হলুদ ওয়াটারমার্কসহ চাইলে /brand\n"
        "ℹ️ কোডেক সাপোর্ট না করলে বট নিজে থেকেই ফুল মোডে চলে যাবে\n"
        f"⏱ এই মোড বাতিল হবে {AWAIT_TTL // 60} মিনিট পর। বাতিল করতে /brandcancel")


@Client.on_message(filters.private & filters.command("stream") & filters.user(ADMINS))
async def stream_cmd(c, m):
    STATE[m.from_user.id] = {"stage": "stream", "exp": time.time() + AWAIT_TTL}
    await m.reply(
        "🔗 **স্ট্রিম-লিংক মোড চালু!**\n\n"
        "এখন মুভি/ভিডিও ফাইলটা পাঠাও — আগের মতোই স্ট্রিম লিংক + পেজ বানিয়ে দেবে।\n\n"
        "🎬 ব্র্যান্ডেড ফাইল চাইলে আলাদা কমান্ড: /brand\n"
        f"⏱ এই মোড বাতিল হবে {AWAIT_TTL // 60} মিনিট পর। বাতিল করতে /brandcancel")


@Client.on_message(filters.private & filters.command(["brandcancel", "brandstatus"])
                   & filters.user(ADMINS))
async def brand_ctl(c, m):
    if m.command[0] == "brandstatus":
        q = QUEUE.qsize() if QUEUE else 0
        await m.reply(f"📋 কিউতে: {q} টা • চলছে/নামানো হচ্ছে: {max(0, len(JOBS) - q)} টা")
        return
    st = STATE.pop(m.from_user.id, None)
    n = 0
    for key, j in list(JOBS.items()):
        if not j.get("cancelled"):
            j["cancelled"] = True
            _cleanup(key)
            n += 1
    parts = []
    if st:
        parts.append(f"{'ব্র্যান্ড' if st.get('stage') == 'file' else 'স্ট্রিম'}-মোড বাতিল")
    if n:
        parts.append(f"চলতে জব {n} টা বাতিল")
    await m.reply("🛑 " + (" • ".join(parts) if parts else "বাতিল করার কিছু ছিল না।"))


# ───────────────────────── ফাইল গ্রহণ (group=2 → স্ট্রিম-প্লাগিনের আগে) ─────────────────────────

@Client.on_message(filters.private & (filters.video | filters.document | filters.audio), group=2)
async def brand_file(c, m):
    if m.from_user is None or m.from_user.id not in ADMINS:
        return                                  # সাধারণ ইউজার → আগের সিস্টেম অক্ষত
    st = STATE.get(m.from_user.id)
    if not st or st["exp"] < time.time():
        # অ্যাডমিন কমান্ড ছাড়া ফাইল দিলে আর অটো-স্ট্রিম হবে না —
        # আগে /stream বা /brand দিতে হবে (সিস্টেম আলাদা করার নিয়ম)
        await m.reply(HINT_TEXT)
        m.stop_propagation()                    # স্ট্রিম-প্লাগিন (group=4) ফেলে দেবে না
        return
    STATE.pop(m.from_user.id, None)
    if st["stage"] == "stream":
        return          # /stream মোড → কিছু না করে পাস → স্ট্রিম-প্লাগিন লিংক বানাবে
    # ── এখান থেকে ব্র্যান্ড-মোড: /brand (ফুল) বা /brandfast (ফাস্ট) ──
    mode = "fast" if st["stage"] == "fast" else "full"
    if m.audio:
        await m.reply("⚠️ অডিও ব্র্যান্ড করা যায় না — মুভি/ভিডিও ফাইল দাও।")
        m.stop_propagation()
        return
    d = _video_of(m)
    if d is None:
        await m.reply("⚠️ এটা ভিডিও না — মুভি/ভিডিও ফাইল পাঠাও (mp4/mkv/avi...)। "
                      "আবার /brand দিয়ে চেষ্টা করো।")
        m.stop_propagation()
        return
    size = d.file_size or 0
    if size > BOT_LIMIT_GB * GB:
        await m.reply(f"⚠️ ফাইলটা {_size(size)} — টেলিগ্রাম বট-অ্যাকাউন্টের হার্ড "
                      f"লিমিট {BOT_LIMIT_GB:g} GB (ডাউনলোড+আপলোড দুটোতেই, এটা "
                      "টেলিগ্রাম সার্ভারের নিয়ম — প্রিমিয়াম থাকলেও বট পারে না)। "
                      "ছোট ফাইল দিয়ে শুরু করো।")
        return
    if QUEUE and QUEUE.qsize() >= QUEUE_MAX:
        await m.reply("⚠️ কিউ ভরা আছে — একটু পরে আবার /brand দাও।")
        return
    key = f"{m.from_user.id}:{m.id}"
    if key in JOBS:
        await m.reply("⏳ এই ফাইল ইতিমধ্যে চলছে।")
        return
    _ensure_worker()
    # 🐞 ফিক্স (লগ: FileNotFoundError /tmp/mbxbrand/...): ফোল্ডার না থাকলে
    # নিজে বানিয়ে নে — ফ্রেশ-স্টার্টে branding-ওয়ার্কার তখনো চালু না হলেও কাজ করবে
    os.makedirs(BRAND_TMP_DIR, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=BRAND_TMP_DIR, prefix=f"brand_{m.id}_")
    path = os.path.join(workdir, "input.bin")
    st_msg = await m.reply("📥 ডাউনলোড হচ্ছে...\n" + _bar(0))
    await c.download_media(m, file_name=path,
                           progress=_make_prog(st_msg, "📥 ডাউনলোড হচ্ছে..."))
    try:
        meta = probe_media(path)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        await st_msg.edit(f"❌ ফাইলটা ভিডিও না বা ভাঙা: {str(e)[:200]}")
        return
    thumb_path = ""
    thumbs = getattr(d, "thumbs", None) or []
    if thumbs:
        try:
            thumb_path = os.path.join(workdir, "thumb_orig.jpg")
            await c.download_media(thumbs[-1], file_name=thumb_path)
        except Exception:
            thumb_path = ""
    name = getattr(d, "file_name", None) or f"Movie_{m.id}.mp4"
    JOBS[key] = dict(path=path, name=name, size=os.path.getsize(path),
                     duration=meta["duration"], thumb_path=thumb_path,
                     custom_thumb=False, awaiting=None, queued=False, mode=mode,
                     panel_msg=st_msg, cancelled=False, client=c)
    await st_msg.edit(_panel(JOBS[key]), reply_markup=_buttons(str(m.id)))
    m.stop_propagation()    # স্ট্রিম-প্লাগিন (group=4) যেন ডাবল রেসপন্স না দেয়


# ───────────────────────── নাম / থাম্বনেইল ইনপুট ─────────────────────────

def _awaiting(m, what):
    for key, j in JOBS.items():
        if int(key.split(":")[0]) == m.chat.id and j.get("awaiting") == what:
            return key, j
    return None, None


@Client.on_message(filters.private & filters.text & filters.user(ADMINS), group=2)
async def brand_text(c, m):
    txt = (m.text or "").strip()
    if not txt or txt.startswith("/"):
        return                                  # কমান্ড না হলে শুধু তখনই নাম হিসেবে নেবে
    key, job = _awaiting(m, "name")
    if job is None:
        return
    if txt.lower() in ("skip", "বাতিল", "no"):
        job["awaiting"] = None
        await m.reply("↩️ নাম বদলানো বাতিল।")
    else:
        base = os.path.splitext(txt)[0] or txt
        ext = os.path.splitext(txt)[1].lower()
        job["name"] = base + (ext if ext else ".mp4")
        job["awaiting"] = None
        await m.reply(f"✏️ নাম হলো: {job['name']}")
    await job["panel_msg"].edit(_panel(job), reply_markup=_buttons(key.split(":")[-1]))


@Client.on_message(filters.private & filters.photo & filters.user(ADMINS), group=2)
async def brand_photo(c, m):
    key, job = _awaiting(m, "thumb")
    if job is None:
        return
    tp = os.path.join(os.path.dirname(job["path"]), "thumb_custom.jpg")
    await c.download_media(m, file_name=tp)
    job["thumb_path"], job["custom_thumb"], job["awaiting"] = tp, True, None
    await m.reply("🖼 থাম্বনেইল সেট হয়েছে (ইন্ট্রো কার্ডের ব্যাকগ্রাউন্ডও এটাই)।")
    await job["panel_msg"].edit(_panel(job), reply_markup=_buttons(key.split(":")[-1]))


# ───────────────────────── বাটন ─────────────────────────
# group=-1: callback.py-র ক্যাচ-অল হ্যান্ডলার (group=0, ফিল্টার ছাড়া) আগে ধরে
# নিলে আমার হ্যান্ডলার কখনো চলতো না — বাটন চাপলে চুপচাপ স্পিনার ঘুরতো।
# সবার আগে চলে সাথে সাথে answer করে + StopPropagation — বাটন এখন সব কাজ করবে।

@Client.on_callback_query(filters.create(
    lambda _, __, q: bool(q.data) and q.data.startswith(("bren:", "bthu:", "bcan:", "bgo:"))),
    group=-1)
async def brand_cb(c, q):
    try:
        if q.from_user.id not in ADMINS:
            return await q.answer("এটা অ্যাডমিনের বাটন।", show_alert=True)
        if not q.message:
            return await q.answer("মেসেজটা আর নেই — /brand দিয়ে নতুন করে করো",
                                  show_alert=True)
        act, mid = q.data.split(":", 1)
        key = f"{q.message.chat.id}:{mid}"
        job = JOBS.get(key)
        if job is None:
            return await q.answer("এই জব আর নেই — বট রিস্টার্ট হয়ে থাকতে পারে; "
                                  "/brand দিয়ে নতুন করে করো", show_alert=True)
        if act == "bren":
            job["awaiting"] = "name"
            await q.message.edit("✏️ নতুন নাম লিখে পাঠাও (এক্সটেনশনসহ, যেমন: movie.mp4)।\n"
                                 "না বদলাতে চাইলে skip লিখো।")
            await q.answer()
        elif act == "bthu":
            job["awaiting"] = "thumb"
            await q.message.edit("🖼 থাম্বনেইলের ছবি পাঠাও (JPG/PNG)।")
            await q.answer()
        elif act == "bcan":
            job["cancelled"] = True
            _cleanup(key)
            await q.message.edit("❌ বাতিল করা হলো।")
            await q.answer("বাতিল")
        elif act == "bgo":
            if job.get("queued"):
                return await q.answer("ইতিমধ্যে কিউতে আছে", show_alert=True)
            _ensure_worker()
            job["queued"] = True
            _POS["n"] += 1
            await QUEUE.put(key)
            await q.message.edit(f"🚀 কিউতে বসলো! (আগে {_POS['n'] - 1} টা আছে)\n\n{_panel(job)}")
            await q.answer("কিউতে বসলো ✅")
        raise StopPropagation   # ক্যাচ-অল যেন আর ধরে না
    except StopPropagation:
        raise
    except Exception as e:
        # যেকোনো এররে ইউজার স্পিনার না দেখে কারণটা চোখে দেখুক
        logger.exception("/brand বাটন এরর")
        try:
            await q.answer(f"❌ {str(e)[:180]}", show_alert=True)
        except Exception:
            pass
