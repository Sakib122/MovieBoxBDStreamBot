# ══════════════════════════════════════════════════════════════════
# 📊 MovieBoxBD অ্যাডমিন প্যানেল v4 + অ্যাপ-ট্র্যাকিং API (aiohttp)
#
# অ্যাপ থেকে আসে:
#   POST /api/track      → ইনস্টল / অ্যাপ-ওপেন / ভিউ / প্লে / ডাউনলোড
#   GET  /api/app/latest → নতুন আপডেট আছে কিনা (versionCode তুলনা অ্যাপেই হয়)
#
# সাকিব ভাইয়ের জন্য:
#   GET  /admin          → পাসওয়ার্ড দিয়ে লগইন → লাইভ ড্যাশবোর্ড
#     • মোট ইউজার, আজ অ্যাক্টিভ, প্রতিদিন কতজন জয়েন
#     • 🎬 ব্লগের সবগুলো আপলোডের টেবিল — নতুনগুলো সবার উপরে, সার্চ অপশন
#     • 🆕 v3: এডিট/লুকাও বাটন বাদ — পোস্ট এডিট-ডিলিট এখন শুধু ব্লগারেই
#     • 🆕 v4: ভিউ = মুভির পেজ/ডিটেইল খুললেই অটোমেটিক কাউন্ট (view ইভেন্ট) —
#       আগের ভিউ-সিস্টেমই থাকবে; প্লে-ভিত্তিক হিসাব আর এপিসোড/কোয়ালিটি ভাগ বাদ
#     • ⬇ কোন মুভি বেশি ডাউনলোড হয়েছে — ওই সিস্টেম অক্ষত
#     • 🚀 আপডেট ব্রডকাস্ট: ভার্সন + APK লিঙ্ক দিলেই সব অ্যাপে ব্যানার+নোটিফিকেশন
#   ইতিহাস: v3 = বাটন বাদ + প্লে-ভিত্তিক ভিউ + নতুন ডিজাইন
#            v4 = প্লে-সিস্টেম বাদ — ভিউ আবার পেজ-ভিউ অটো-কাউন্ট
#
# পাসওয়ার্ড: Koyeb-এ ADMIN_PASSWORD এনভায়রনমেন্ট ভ্যারিয়েবল দাও
#   (না দিলে ডিফল্ট "moviebox@2026" — দ্রুত বদলাবে!)
# ব্লগ: Koyeb-এ BLOG_URL ভ্যারিয়েবলে তোমার ব্লগের লিঙ্ক দাও
#   (যেমন https://movieboxbd.blogspot.com) — টেবিল ওখান থেকেই লোড হয়
# ══════════════════════════════════════════════════════════════════

import os
import re
import time
import asyncio
import hashlib
import html
from collections import Counter

import aiohttp
from aiohttp import web

from database.users_db import mydb

stats_routes = web.RouteTableDef()

installs = mydb["mbx_installs"]     # {_id: installId, first, last, model, ver}
events = mydb["mbx_events"]         # {id, event, label, ver, ts}
appcfg = mydb["mbx_app_config"]     # {_id: "update", code, name, url, msg}

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "moviebox@2026")
COOKIE = "mbx_admin"
TOKEN = hashlib.sha256(("mbx-admin:" + ADMIN_PASSWORD).encode()).hexdigest()[:40]

DAY = 86400.0
TZ = 6 * 3600  # বাংলাদেশ UTC+6 — দিনের হিসাব এখান থেকে
WEEKDAYS = ["সোম", "মঙ্গল", "বুধ", "বৃহঃ", "শুক্র", "শনি", "রবি"]

# ──────────────────── 🎬 ব্লগ-ফিড (মুভি টেবিলের উৎস) ────────────────────
BLOG_URL = os.environ.get("BLOG_URL", "").strip().rstrip("/")
BLOG_TTL = 900        # ১৫ মিনিট ক্যাশ — CPU বাঁচাতে বারবার Blogger-কে টানা হয় না
BLOG_MAX_POSTS = 3000  # সেফটি ক্যাপ
BLOG_PAGE = 100

_blog_state = {"ts": 0.0, "posts": [], "blog_id": "", "total": 0,
               "err": "", "loading": False, "stale": False}


def _t(v) -> str:
    """Blogger JSON feed-এর $t-মোড়ানো ভ্যালু সোজা স্ট্রিং করে"""
    if isinstance(v, dict):
        return str(v.get("$t", "") or "")
    return str(v or "")


_NORM_RE = re.compile(r"[^0-9a-z\u0980-\u09FF]+")


def _norm(s: str) -> str:
    """টাইটেল-ম্যাচিংয়ের জন্য স্বাভাবিকীকরণ: ছোট হাতের + শুধু অক্ষর/সংখ্যা/বাংলা"""
    return _NORM_RE.sub(" ", (s or "").lower()).strip()


def _title_quality(title: str) -> str:
    """টাইটেল থেকে কোয়ালিটি — v3: খালি 'HD' আর দেখানো হয় না।
    টাইটেলে সোজা 720p/1080p/480p/4K থাকলে সেটা, সিরিজ হলে EP রেঞ্জ, নাহলে খালি"""
    m = re.search(r"\b(2160|1080|720|480|360)p\b", title, re.IGNORECASE)
    if m:
        return m.group(0).lower()
    if re.search(r"\b4\s?k\b", title, re.IGNORECASE):
        return "4K"
    m = re.search(r"\bEP\s*\d+(\s*-\s*\d+)?\b", title, re.IGNORECASE)
    if m:
        return m.group(0).upper().replace(" ", "")
    return "—"


def _parse_blog_json(data) -> tuple:
    """Blogger feed JSON → (posts list, blog_id, total) — অফলাইন-টেস্টেবল"""
    feed = data.get("feed", {}) if isinstance(data, dict) else {}
    blog_id = ""
    m = re.search(r"blog-(\d+)", _t(feed.get("id")))
    if m:
        blog_id = m.group(1)
    try:
        total = int(float(_t(feed.get("openSearch$totalResults")) or 0))
    except Exception:
        total = 0
    entries = feed.get("entry", []) or []
    if isinstance(entries, dict):
        entries = [entries]
    posts = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        m = re.search(r"post-(\d+)", _t(e.get("id")))
        pid = m.group(1) if m else ""
        title = _t(e.get("title")).strip()
        pub = _t(e.get("published"))[:10]
        cats = e.get("category") or []
        labels = []
        if isinstance(cats, dict):
            cats = [cats]
        for c in cats:
            if isinstance(c, dict) and c.get("term"):
                labels.append(str(c["term"]))
        if pid and title:
            posts.append({"pid": pid, "title": title, "pub": pub, "labels": labels})
    return posts, blog_id, total


async def _fetch_blog_all():
    """Blogger summary-feed থেকে সব পোস্ট টেনে ক্যাশে ভরে — প্রতি ১৫ মিনিটে"""
    timeout = aiohttp.ClientTimeout(total=90)
    posts, bid, total = [], "", 0
    async with aiohttp.ClientSession(timeout=timeout,
                                     headers={"User-Agent": "MovieBoxBD-Panel/3.0"}) as s:
        for start in range(1, BLOG_MAX_POSTS + 1, BLOG_PAGE):
            url = f"{BLOG_URL}/feeds/posts/summary?alt=json&max-results={BLOG_PAGE}&start-index={start}"
            async with s.get(url) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
            chunk, bid2, total2 = _parse_blog_json(data)
            if bid2:
                bid = bid2
            if total2:
                total = total2
            posts.extend(chunk)
            if len(chunk) < BLOG_PAGE:
                break
    _blog_state.update(ts=time.time(), posts=posts, blog_id=bid, total=total,
                       err="", stale=False)


def _ensure_blog_fresh(force: bool = False):
    """ক্যাশ টাটকা থাকলে কিছু না; বাসি/খালি হলে ব্যাকগ্রাউন্ডে ফ্রেশ করে —
    প্যানেল লোড কখনোই আটকায় না (0.1 vCPU-তে জরুরি)"""
    if not BLOG_URL:
        return
    fresh = _blog_state["posts"] and (time.time() - _blog_state["ts"]) < BLOG_TTL
    if fresh and not force:
        return
    if _blog_state["loading"]:
        return
    _blog_state["loading"] = True

    async def _job():
        try:
            await _fetch_blog_all()
        except Exception as e:
            if _blog_state["posts"]:
                _blog_state.update(err=str(e)[:120], stale=True)
            else:
                _blog_state.update(err=str(e)[:120], ts=time.time())
        finally:
            _blog_state["loading"] = False

    try:
        asyncio.get_running_loop().create_task(_job())
    except Exception:
        _blog_state["loading"] = False


# ──────────────────────── ট্র্যাকিং হেল্পার ────────────────────────
def _day_key(ts: float) -> int:
    return int((ts + TZ) // DAY)


def _today_start(ts: float) -> float:
    return _day_key(ts) * DAY - TZ


# 🎬 হেল্পার ১ — লেবেল থেকে শুধু কোয়ালিটি বের করে
def _quality(label: str) -> str:
    """'720p WEB-DL 1002MB' → '720p' — কোয়ালিটি রাখো, সাইজ/বাড়তি বাদ দাও"""
    m = re.search(r"\b\d{3,4}\s?p\b", label, re.IGNORECASE)
    if m:
        return m.group(0).replace(" ", "").lower()
    m = re.search(r"\b[24]k\b", label, re.IGNORECASE)
    if m:
        return m.group(0).upper()
    return label.strip()[:24]


# 🎬 হেল্পার ২ — লেবেলটা কি লিংকের নামের মতো? ('720p WEB-DL', 'EP 128', '450MB')
def _looks_link(label: str) -> bool:
    if re.search(r"\b\d{3,4}\s?p\b", label, re.IGNORECASE):
        return True
    if re.match(r"^ep\s*\d+", label, re.IGNORECASE):
        return True
    if re.search(r"\b\d+(\.\d+)?\s?(mb|gb|kb)\b", label, re.IGNORECASE):
        return True
    return False


# 🎬 হেল্পার ২.৫ — লেবেলে কি আসল নাম আছে? ('মুভির নাম (720p)' → হ্যাঁ,
# '720p WEB-DL · 1.20 GB' → না)
def _has_name(label: str) -> bool:
    s = (label or "").strip()
    if not s:
        return False
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)                             # শেষের (…) বাদ
    s = re.sub(r"^(ep\s*\d+)\b[·:\- ]*", "", s, flags=re.IGNORECASE)   # EP 128 …
    s = re.sub(r"\b\d{3,4}\s?p\b.*$", "", s, flags=re.IGNORECASE).strip()   # 720p …
    s = re.sub(r"\b\d+(\.\d+)?\s?(mb|gb|kb)\b.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^(web[- ]?dl|hdrip|webrip|bluray|hdtv|hdr)\b[·:\- ]*", "",
               s, flags=re.IGNORECASE).strip()
    return len(s) >= 3


# 🎬 হেল্পার ৩ — প্লে-লেবেলের সাথে আসল মুভি/সিরিজের নাম জুড়ে দেয়
async def _play_label(iid: str, label: str, now: float) -> str:
    """প্লে-এর ঠিক আগে একই ফোন যে মুভির ডিটেইল খুলেছিল (view ইভেন্ট) —
    সেই আসল নামটাই জুড়ে দাও → 'মুভির নাম (720p)' / 'সিরিজের নাম (EP 128)'"""
    try:
        doc = await installs.find_one({"_id": iid}, {"last_view": 1, "last_view_ts": 1})
        name = str(doc.get("last_view") or "").strip() if doc else ""
        ts = float(doc.get("last_view_ts") or 0) if doc else 0.0
        if name and 0 < now - ts <= 1800:   # ৩০ মিনিটের ভিতরের view হলেই গ্রহণযোগ্য
            q = _quality(label)
            return f"{name} ({q})"[:120] if q else name[:120]
    except Exception:
        pass
    return label


# ──────────────── 🆕 v4 — ভিউ-ম্যাচিং (পেজ-ভিউ অটো-কাউন্ট) ────────────────
def _view_count(nt: str, idx: dict) -> int:
    """একটা পোস্ট-টাইটেলের মোট ভিউ — অ্যাপে মুভির পেজ খুললেই 'view' ইভেন্ট
    গোনা হয় (প্লে-সিস্টেম বাদ)। idx = প্রথম শব্দ → [(স্বাভাবিক লেবেল, সংখ্যা)]
    — ০.১ vCPU-তে দ্রুত রাখার ইনডেক্স"""
    if not nt or len(nt) < 6:
        return 0
    cands = idx.get(nt.split(" ", 1)[0], [])
    if not cands:
        return 0
    n = 0
    pref = nt + " "
    for k, c in cands:
        if k == nt or k.startswith(pref):
            n += c          # হুবহু ম্যাচ + টাইটেলের পরে বাড়তি অংশ থাকলেও গণ্য
    return n


# ────────────────────────── অ্যাপ API ──────────────────────────

@stats_routes.post("/api/track")
async def api_track(request):
    try:
        d = await request.json()
        iid = str(d.get("id", "")).strip()[:64]
        event = str(d.get("event", "")).strip()[:24]
        if not iid or not event:
            return web.json_response({"ok": False}, status=400)
        label = str(d.get("label", "")).strip()[:120]
        model = str(d.get("model", "")).strip()[:48]
        ver = str(d.get("ver", "")).strip()[:16]
        now = time.time()
        extra = {}
        if event == "view" and label and not _looks_link(label):
            # 🎬 শেষ কোন মুভি/সিরিজের ডিটেইল খুলল — আসল নাম হলেই মনে রাখো
            extra = {"last_view": label[:120], "last_view_ts": now}
        await installs.update_one(
            {"_id": iid},
            {"$set": {"last": now, "model": model, "ver": ver, **extra},
             "$setOnInsert": {"first": now}},
            upsert=True,
        )
        if event == "watch_stop":   # 🎬 প্লে বন্ধ (v4.1 অ্যাপ) — "▶ দেখছে" স্টেট এখনই নামাও
            await installs.update_one({"_id": iid},
                                      {"$unset": {"last_play": "", "last_play_ts": ""}})
            return web.json_response({"ok": True})
        if event == "watch":   # 👀 প্লেয়ার-হার্টবিট (৪৫ সে) — গোনা হয় না, শুধু
            # "এই ফোন এখনো এই মুভিটা দেখছে" স্টেটটা সতেজ রাখে
            # 🎬 (প্যানেল-ফিক্স) হার্টবিটের কাঁচা লেবেল ('720p WEB-DL · 1.20 GB')
            #   আসল মুভির নাম মুছে দেয় — নাম-বিহীন লেবেল হলে আগের নামটাই রাখো,
            #   না থাকলে play/view-ইতিহাস থেকে রিজলভ করো
            if _has_name(label):
                wlabel = label
            else:
                prev = await installs.find_one({"_id": iid},
                                               {"last_play": 1, "last_play_ts": 1})
                keep = str((prev or {}).get("last_play") or "").strip()
                if _has_name(keep) and now - float((prev or {}).get("last_play_ts") or 0) <= 7200:
                    wlabel = keep   # ২ ঘণ্টার ভিতরের নাম-যুক্ত লেবেল — সেটাই থাক
                else:
                    wlabel = await _play_label(iid, label, now)   # view-ইতিহাস থেকে রিজলভ
            await installs.update_one({"_id": iid},
                                      {"$set": {"last_play": wlabel[:120], "last_play_ts": now}})
            return web.json_response({"ok": True})
        if event == "play":   # 🎬 প্লে-রিপোর্টে আসল মুভির নাম + কোয়ালিটি/EP বসিয়ে দাও
            label = await _play_label(iid, label, now)
            # 👀 (প্যানেল-ফিক্স) এই ইউজার এখন কী দেখছে — ইনস্টল-ডকেও মনে রাখো,
            # তাহলেই প্যানেলের "👀 কে কী দেখছে (লাইভ)" লিস্ট কাজ করবে
            await installs.update_one({"_id": iid},
                                      {"$set": {"last_play": label[:120], "last_play_ts": now}})
        if event not in ("beat", "watch", "watch_stop"):   # 💓 beat/watch/watch_stop জমা হয় না — শুধু 'last'/'last_play' সতেজ হয়
            # (আগে watch প্রতি ৪৫ সেকেন্ডে events-এ জমাত — Mongo-ব্লোটের আসল কারণ)
            await events.insert_one({"id": iid, "event": event, "label": label,
                                     "ver": ver, "ts": now})
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"ok": False}, status=400)

@stats_routes.get("/api/live")
async def api_live(request):
    """🔴 লাইভ — কাউন্ট + 👀 কে কী দেখছে (প্যানেল প্রতি ১ মিনিটে নিজে নিজে আপডেট হয় এটা দিয়ে; রিফ্রেশে সাথে সাথে)"""
    if request.cookies.get(COOKIE) != TOKEN:
        return web.json_response({"live": 0})
    try:
        now = time.time()
        n = await installs.count_documents({"last": {"$gte": now - 60}})
        # 👀 লাইভ ইউজারদের হালকা প্রোফাইল — ফোনের মডেল + এই সেশনের শেষ প্লে/ভিউ।
        # 🔒 ইউজার-আইডি কখনোই প্যানেলে যায় না — শুধু মডেল, ভার্সন আর মুভির নাম।
        cur = installs.find({"last": {"$gte": now - 60}},
                            {"model": 1, "ver": 1, "last_play": 1, "last_play_ts": 1,
                             "last_view": 1, "last_view_ts": 1, "last": 1}
                            ).sort("last", -1).limit(30)
        users = []
        for u in await cur.to_list(30):
            users.append({
                "model": str(u.get("model") or ""),
                "ver": str(u.get("ver") or ""),
                "play": str(u.get("last_play") or ""),
                "play_ts": float(u.get("last_play_ts") or 0),
                "view": str(u.get("last_view") or ""),
                "view_ts": float(u.get("last_view_ts") or 0),
                "last": float(u.get("last") or 0),
            })
        # 🎬 স্ট্রিম-সার্ভারের আসল প্রমাণ — শেষ ২ মিনিটে যা সত্যিই স্ট্রিম হয়েছে
        try:
            from .stream_routes import recent_streams
            streams = [{"title": t, "ts": ts}
                       for t, ts in sorted(recent_streams(120), key=lambda x: -x[1])][:12]
        except Exception:
            streams = []
        return web.json_response({"live": n, "users": users, "streams": streams})
    except Exception:
        return web.json_response({"live": 0})

@stats_routes.get("/api/app/latest")
async def api_latest(request):
    try:
        doc = await appcfg.find_one({"_id": "update"})
        if not doc:
            return web.json_response({"code": 0})
        # 🛡 (v4.3) ভার্সন-সচেতন আপডেট-লিঙ্ক:
        #   নতুন অ্যাপ (৪.৩+) ?code= পাঠায় → সরাসরি APK (একই সাইনিং → ওভর-ইনস্টল ✓)
        #   পুরনো অ্যাপ (২.x–৪.২) প্যারাম পাঠায় না → গাইড-পেজ লিঙ্ক — ওরা APK-তে
        #   ট্যাপ করলে ভয়ংকর "App not installed"-এর বদলে বাংলা গাইড খুলবে
        #   (ভিন্ন-সাইনিং APK Android কখনোই ওভর-ইনস্টল করতে দেয় না — OS-লেভেল নিয়ম)
        url = str(doc.get("url", ""))
        if request.query.get("code") is None:
            try:
                base = request.url.origin()
            except Exception:
                base = ""
            if base:
                url = base + "/update-guide"
        return web.json_response({
            "code": int(doc.get("code", 0)),
            "name": str(doc.get("name", "")),
            "url": url,
            "msg": str(doc.get("msg", "")),
            "ts": float(doc.get("ts", 0)),   # 🆕 ঘোষণার সময় — প্রতিবার নতুন ঘোষণা = নতুন পিং
        })
    except Exception:
        return web.json_response({"code": 0})


# ────────────────────────── 🆕 আপডেট গাইড-পেজ (পুরনো-ভার্সন ইউজারদের জন্য) ──────────────────────────

_GUIDE_CSS = """
*{box-sizing:border-box}
body{background:#070A14;color:#E9EEFF;font-family:'Noto Sans Bengali','SolaimanLipi',sans-serif;margin:0;padding:28px 16px 50px;
background-image:radial-gradient(800px 460px at 85% -5%,rgba(124,108,255,.16),transparent 60%),radial-gradient(640px 400px at -5% 20%,rgba(0,229,160,.10),transparent 60%)}
.wrap{max-width:470px;margin:0 auto}
.badge{display:inline-block;background:linear-gradient(90deg,#00E5A0,#7C6CFF);color:#04121B;font-weight:700;font-size:13px;padding:4px 12px;border-radius:999px;margin-bottom:10px}
h1{font-size:22px;line-height:1.35;margin:0 0 6px}
.sub{color:#8B94B8;font-size:14px;margin:0 0 18px}
.why{background:linear-gradient(160deg,#151B33,#10142A);border:1px solid #2A3458;border-radius:14px;padding:14px 16px;font-size:14px;line-height:1.7;color:#C9D2F5;margin-bottom:18px}
.why b{color:#FFD666}
.step{background:linear-gradient(160deg,#151B33,#10142A);border:1px solid #232B4A;border-radius:14px;padding:16px;margin-bottom:14px}
.step h2{font-size:16px;margin:0 0 6px;color:#E9EEFF}
.step p{font-size:14px;color:#8B94B8;line-height:1.7;margin:0 0 12px}
.btn{display:block;text-align:center;text-decoration:none;font-weight:700;font-size:16px;padding:13px 16px;border-radius:12px}
.btn-del{background:#3A1622;color:#FF7A93;border:1px solid #6E2B3C}
.btn-apk{background:linear-gradient(90deg,#00E5A0,#7C6CFF);color:#04121B;font-size:17px}
.hint{font-size:12.5px;color:#69719A;line-height:1.65;margin:10px 0 0}
.foot{font-size:12.5px;color:#5A6288;text-align:center;margin-top:22px;line-height:1.7}
"""

_GUIDE_HTML = """<!DOCTYPE html>
<html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MovieBoxBD আপডেট গাইড</title>
<style>__GUIDE_CSS__</style></head>
<body><div class="wrap">
<span class="badge">🚀 অফিসিয়াল আপডেট</span>
<h1>MovieBoxBD __VER__ এসেছে! 🎉</h1>
<p class="sub">নতুন ফিচার, ফাস্ট স্ট্রিমিং আর নতুন সিকিউরিটি সিস্টেম।</p>

<div class="why">🔑 <b>একটা ছোট্ট বিষয়:</b> নতুন সিকিউরিটি সাইনিং সিস্টেমের জন্য এই <b>একবারই</b> পুরনোটা রিমুভ করে ইনস্টল করতে হবে। এরপর থেকে সব আপডেট অ্যাপের ভেতর থেকেই <b>এক ট্যাপে</b> হয়ে যাবে — আর কখনো এই ঝামেলা নেই! 💚</div>

<div class="step">
<h2>১️⃣ পুরনো MovieBoxBD রিমুভ করো</h2>
<p>নিচের বাটনে ট্যাপ করলেই ফোন রিমুভ-কনফার্মেশন দেখাবে — OK চাপো।</p>
<a class="btn btn-del" href="intent:#Intent;action=android.intent.action.DELETE;package=com.movieboxbd.app;S.browser_fallback_url=movieboxbd%3A%2F%2Fguide;end">🗑️ এক ট্যাপে রিমুভ</a>
<p class="hint">বাটন কাজ না করলে: ফোনের <b>সেটিংস → অ্যাপস → MovieBoxBD → আনইনস্টল</b></p>
</div>

<div class="step">
<h2>২️⃣ নতুন APK ডাউনলোড করো</h2>
<p>ডাউনলোড শেষে নোটিফিকেশন বা ডাউনলোড-লিস্ট থেকে ফাইলটায় ট্যাপ করো।</p>
<a class="btn btn-apk" href="__APK_URL__">⬇️ __VER__ ডাউনলোড করো</a>
</div>

<div class="step">
<h2>৩️⃣ ইনস্টল করে ওপেন করো ✅</h2>
<p>ইনস্টলার খুললে <b>Install</b> চাপো। "অজানা উৎস/Unknown sources" চাইলে <b>Allow</b> করো (একবারই)। ইনস্টল শেষে <b>Open</b> — ব্যস, নতুন ভার্সন রেডি! 🎬</p>
</div>

<p class="foot">এটি MovieBoxBD-এর অফিসিয়াল সার্ভারের পেজ।<br>শুধুমাত্র অ্যাপের আপডেট-নোটিফিকেশন/পপআপ থেকে আসা লিঙ্কেই বিশ্বাস করো।</p>
</div></body></html>"""


@stats_routes.get("/update-guide", allow_head=True)
async def update_guide(request):
    """🆕 পুরনো-ভার্সন ইউজাররা "আপডেট" ট্যাপ করলে ব্রাউজারে এই বাংলা গাইড খুলবে।
    APK-লিঙ্ক সবসময় প্যানেলের বর্তমান ঘোষণা থেকে নেয় — আলাদা কিছু করতে হবে না।"""
    try:
        doc = await appcfg.find_one({"_id": "update"})
    except Exception:
        doc = None
    apk = str(doc.get("url", "")).strip() if doc else ""
    ver = str(doc.get("name", "")).strip() if doc else ""
    if not apk:
        body = ('<div class="wrap"><span class="badge">MovieBoxBD</span>'
                '<h1>এই মুহূর্তে কোনো নতুন আপডেট নেই 🙂</h1>'
                '<p class="sub">নতুন ভার্সন এলে অ্যাপে নোটিফিকেশন পাবে। চালিয়ে যাও, মজে থেকো! 🎬</p></div>')
        html = ('<!DOCTYPE html><html lang="bn"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>MovieBoxBD</title><style>' + _GUIDE_CSS + '</style></head><body>' + body + '</body></html>')
    else:
        html = _GUIDE_HTML.replace("__GUIDE_CSS__", _GUIDE_CSS).replace("__APK_URL__", apk).replace("__VER__", ver or "নতুন ভার্সন")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


# ────────────────────────── অ্যাডমিন UI ──────────────────────────

_CSS = """
*{box-sizing:border-box}
body{background:#070A14;color:#E9EEFF;font-family:'Noto Sans Bengali','SolaimanLipi',sans-serif;margin:0;padding:22px 18px 46px;
background-image:radial-gradient(900px 500px at 85% -5%,rgba(124,108,255,.15),transparent 60%),radial-gradient(700px 420px at -5% 22%,rgba(0,229,160,.09),transparent 60%)}
h1{font-size:23px;margin:0 0 4px;font-weight:700;background:linear-gradient(90deg,#00E5A0,#7C6CFF);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#8B94B8;font-size:13px;margin-bottom:16px}
.sub b{color:#E9EEFF}
h2{font-size:16px;color:#E9EEFF;margin:28px 0 10px;padding-bottom:8px;position:relative}
h2:after{content:'';position:absolute;left:0;bottom:0;width:64px;height:3px;border-radius:2px;background:linear-gradient(90deg,#00E5A0,#7C6CFF)}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:6px}
.card{background:linear-gradient(160deg,#151B33,#10142A);border:1px solid #232B4A;border-radius:14px;padding:16px 18px;min-width:150px;flex:1;position:relative;overflow:hidden;animation:fadeUp .5s ease both;transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease}
.card:nth-child(2){animation-delay:.07s}.card:nth-child(3){animation-delay:.14s}.card:nth-child(4){animation-delay:.21s}
.card:hover{transform:translateY(-3px);box-shadow:0 12px 26px rgba(124,108,255,.18);border-color:#31406F}
.card .n{font-size:28px;font-weight:700;color:#8F7BFF}
.card .t{font-size:13px;color:#8B94B8;margin-top:3px}
.card.live .n{color:#FF5D7A}
.card.live:after{content:'';position:absolute;top:16px;right:16px;width:9px;height:9px;border-radius:50%;background:#FF3B5C;animation:pulse 1.6s ease infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,59,92,.5)}70%{box-shadow:0 0 0 9px rgba(255,59,92,0)}100%{box-shadow:0 0 0 0 rgba(255,59,92,0)}}
table{width:100%;border-collapse:collapse;background:#11162A;border-radius:12px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;font-size:14px;border-bottom:1px solid #1D2440}
th{color:#8B94B8;font-weight:600;background:#161C36;font-size:13px}
td.num{color:#8F7BFF;font-weight:700;font-size:15px;white-space:nowrap}
td.num.hot{color:#00E5A0}
td.num.mid{color:#FFC24B}
.scroll{max-height:500px;overflow:auto;border-radius:12px}
.bar{color:#00E5A0;letter-spacing:1px}
form{background:#11162A;border:1px solid #232B4A;border-radius:12px;padding:16px;max-width:640px}
form.inline{background:none;padding:0;max-width:none;display:inline;margin:0;border:none}
input,textarea{width:100%;background:#0B0F1F;border:1px solid #2A2F4F;color:#E9EEFF;border-radius:9px;padding:10px 12px;font-size:14px;margin:4px 0 12px;transition:border-color .2s,box-shadow .2s}
input:focus,textarea:focus{outline:none;border-color:#7C6CFF;box-shadow:0 0 0 3px rgba(124,108,255,.18)}
input.search{width:auto;max-width:300px;display:inline-block;margin:0;vertical-align:middle}
button{background:linear-gradient(135deg,#7C6CFF,#5A3FE0);color:#fff;border:0;border-radius:9px;padding:10px 18px;font-size:14px;cursor:pointer;font-family:inherit;transition:transform .2s ease,box-shadow .2s ease}
button:hover{transform:translateY(-1px);box-shadow:0 8px 18px rgba(124,108,255,.35)}
button.gray{background:#2A2F4F}
button.mini{background:#1B2340;color:#DCE3FF;border:1px solid #2C3763;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;margin:2px}
button.mini:hover{background:#26305A;transform:none;box-shadow:none}
.qchip{display:inline-block;background:#1B2340;border:1px solid #2C3763;border-radius:8px;padding:3px 9px;font-size:12px;color:#B8C4FF;margin:1px 4px 1px 0;white-space:nowrap}
.dim{color:#5A6284}
td.ttl{position:relative}
td.ttl b{font-size:14px}
.pid{color:#5A6284;font-size:11px;margin-top:2px}
tr.mrow{transition:background .2s ease}
tr.mrow:hover{background:rgba(124,108,255,.08)}
@keyframes grow{from{width:0}}
.wbar{height:12px;background:#1A2140;border-radius:6px;overflow:hidden;min-width:90px}
.wbar i{display:block;height:100%;background:linear-gradient(90deg,#00E5A0,#7C6CFF);animation:grow 1s ease both}
td.dwd{width:64px;color:#8B94B8}
.dbar{height:10px;background:#1A2140;border-radius:6px;overflow:hidden;min-width:60px}
.dbar i{display:block;height:100%;background:linear-gradient(90deg,#FFC24B,#FF5D7A);animation:grow .9s ease both}
.box{background:#11162A;border:1px solid #232B4A;border-radius:12px;padding:14px;font-size:14px;color:#8B94B8}
a{color:#00E5A0}
.cur{background:#11162A;border:1px solid #232B4A;border-radius:12px;padding:14px;margin-bottom:10px;font-size:14px;color:#8B94B8}
.loginbox{max-width:380px;margin:70px auto 0;text-align:center;background:#11162A;border:1px solid #232B4A;border-radius:16px;padding:32px 26px;animation:fadeUp .5s ease both;box-shadow:0 22px 55px rgba(0,0,0,.45)}
@media(max-width:640px){body{padding:14px 10px 40px}}
"""


def _page(title: str, body: str) -> web.Response:
    return web.Response(text=f"""<!DOCTYPE html>
<html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700&display=swap" rel="stylesheet">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head><body>{body}</body></html>""", content_type="text/html")


ADMIN_JS = """
(function(){
  function cu(el){
    var n = parseInt(el.getAttribute('data-n')||'0',10)||0;
    if(n<=0){ el.textContent='0'; return; }
    var t0=null, dur=900;
    function step(ts){
      if(t0===null) t0=ts;
      var p=Math.min(1,(ts-t0)/dur);
      el.textContent=Math.round(n*(1-Math.pow(1-p,3)));
      if(p<1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  document.querySelectorAll('.card .n[data-n]').forEach(cu);
})();
function mvFilter(q){
  q=(q||'').toLowerCase();
  document.querySelectorAll('#mvTable tbody.mgrp').forEach(function(g){
    var t=g.querySelector('td.ttl');
    g.style.display=(t && t.textContent.toLowerCase().indexOf(q)>-1)?'':'none';
  });
}
function mvSort(key){
  var tbl=document.getElementById('mvTable'); if(!tbl) return;
  var gs=[].slice.call(tbl.querySelectorAll('tbody.mgrp'));
  gs.sort(function(a,b){
    if(key==='views') return (parseInt(b.getAttribute('data-v'),10)||0)-(parseInt(a.getAttribute('data-v'),10)||0);
    return (b.getAttribute('data-d')||'').localeCompare(a.getAttribute('data-d')||'');
  });
  gs.forEach(function(g){ tbl.appendChild(g); });
}
function escLive(s){ return String(s||'').replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
async function liveTick() {
  try {
    const r = await fetch('/api/live');
    const j = await r.json();
    const el = document.getElementById('liveN');
    if (el && el.textContent !== String(j.live)) el.textContent = j.live;
    /* 👀 কে কী দেখছে (লাইভ) — কাউন্টের সাথে লিস্টও প্রতি ১ মিনিটে রিফ্রেশ */
    const box = document.getElementById('liveUsersBox');
    const lu = document.getElementById('liveUsers');
    if (!box || !lu) return;
    const us = j.users || [];
    if (!us.length) { box.style.display = 'none'; lu.innerHTML = ''; return; }
    const nowSec = Date.now() / 1000;
    box.style.display = '';
    let html = us.map(function(u){
      const secs = Math.max(0, Math.round(nowSec - (u.last || 0)));
      const ago = secs < 60 ? secs + ' সেকেন্ড আগে' : Math.round(secs / 60) + ' মিনিট আগে';
      let what;
      if (u.play && nowSec - (u.play_ts || 0) <= 1800)
        what = '<b style="color:#39D98A">▶ ' + escLive(u.play) + '</b>';
      else if (u.play && nowSec - (u.play_ts || 0) <= 10800)
        what = '<span style="color:#6B76A8">▶ ' + escLive(u.play) + ' (আগে প্লে)</span>';
      else if (u.view && nowSec - (u.view_ts || 0) <= 1800)
        what = '<span style="color:#8FA3FF">🔍 ' + escLive(u.view) + ' (দেখছে)</span>';
      else
        what = '<span style="color:#8B94B8">🏠 ব্রাউজ করছে</span>';
      const who = u.model ? escLive(u.model) : 'অজানা ফোন';
      const vv = u.ver ? ' <span style="color:#8B94B8">v' + escLive(u.ver) + '</span>' : '';
      return '<div style="padding:8px 0;border-top:1px solid #232845;font-size:14px">'
        + '<b>📱 ' + who + '</b>' + vv + ' — ' + what
        + '<span style="color:#8B94B8;float:right">' + ago + '</span></div>';
    }).join('');
    /* 🎬 স্ট্রিম-সার্ভারের সত্যিকারের প্লে-প্রমাণ — অ্যাপ-ভার্সন যা-ই হোক */
    const sts = j.streams || [];
    if (sts.length) {
      html += '<div style="margin-top:8px;font-weight:700;color:#39D98A">🎬 এখন স্ট্রিম হচ্ছে (সত্যিকারের প্লে)</div>';
      html += sts.map(function(st){
        const s2 = Math.max(0, Math.round(nowSec - (st.ts || 0)));
        const ag2 = s2 < 60 ? s2 + ' সেকেন্ড আগে পুল' : Math.round(s2 / 60) + ' মিনিট আগে পুল';
        return '<div style="padding:7px 0;border-top:1px solid #232845;font-size:14px">'
          + '<b style="color:#39D98A">▶ ' + escLive(st.title) + '</b>'
          + '<span style="color:#8B94B8;float:right">' + ag2 + '</span></div>';
      }).join('');
    }
    lu.innerHTML = html;
  } catch (e) {}
}
/* 🚀 পেজ খুললেই/রিফ্রেশ দিলেই সাথে সাথে প্রথম ফেচ — হার্টবিট থেকে টাটকা রেজাল্ট (১-৫ সেকেন্ডে) */
liveTick();
/* 👀 তারপর প্রতি ১ মিনিটে নিজে নিজে — লাইভ-উইন্ডো '১ মিনিট'-এর সাথে ম্যাচিং, তাতে কাউন্ট মিলবে;
   মাঝপথে নতুন কেউ জয়েন করলেও পরের টিকে চলে আসবে */
setInterval(liveTick, 60000);
"""


# ──────────────── 🆕 v4 — মুভি টেবিল (বাটন-মুক্ত, পেজ-ভিউ অটো-কাউন্ট) ────────────────
def _movies_html(vnorm) -> str:
    """ব্লগের সব পোস্টের টেবিল — এডিট/লুকাও নেই,
    ভিউ = মুভির পেজ খুললেই অটোমেটিক গোনা হয় (আগের সিস্টেম)"""
    if not BLOG_URL:
        return ("<div class='box'>⚙️ ব্লগ-টেবিল চালু করতে Koyeb-এ "
                "<b style='color:#E9EEFF'>BLOG_URL</b> এনভায়রনমেন্ট ভ্যারিয়েবলে তোমার ব্লগের "
                "লিঙ্ক দাও (যেমন https://movieboxbd.blogspot.com) — তারপর সার্ভিস রিস্টার্ট। "
                "বাকি সব এখনই কাজ করছে।</div>")

    if not _blog_state["posts"]:
        if _blog_state["loading"] or not _blog_state["ts"]:
            return ("<div class='box'>⏳ ব্লগ থেকে সব পোস্ট আনা হচ্ছে… "
                    "২০-৩০ সেকেন্ড পর পেজটা একবার রিফ্রেশ করো।"
                    "<script>setTimeout(function(){location.reload()},25000)</script></div>")
        return (f"<div class='box'>⚠️ ব্লগ পড়া যায়নি: "
                f"{html.escape(_blog_state['err'] or 'কোনো পোস্ট পাওয়া যায়নি')} — "
                f"BLOG_URL ঠিক আছে কিনা দেখো। <a href='/admin?refresh=1'>আবার চেষ্টা</a></div>")

    # 🚀 ইনডেক্স: প্রথম শব্দ → লেবেল-তালিকা (৩০০০ পোস্টেও দ্রুত থাকে)
    idx = {}
    for k, c in vnorm.items():
        idx.setdefault(k.split(" ", 1)[0], []).append((k, c))

    groups = []
    for p in _blog_state["posts"]:
        title = p["title"]
        pub = html.escape(p["pub"] or "—")
        nt = _norm(title)
        tot = _view_count(nt, idx)
        fb = _title_quality(title)
        qcell = (f"<span class='qchip'>{html.escape(fb)}</span>"
                 if fb != "—" else "<span class='dim'>—</span>")
        cat = " · ".join(p["labels"][:2]) if p["labels"] else "—"
        cat = html.escape(cat)
        t_show = html.escape(title[:90] + ("…" if len(title) > 90 else ""))

        vcls = "num hot" if tot >= 100 else ("num mid" if tot >= 20 else "num")
        groups.append(
            f"<tbody class='mgrp' data-v='{tot}' data-d='{pub}'>"
            f"<tr class='mrow'>"
            f"<td class='ttl'><b>{t_show}</b>"
            f"<div class='pid'>ID: {html.escape(p['pid'][:12])}… · {pub}</div></td>"
            f"<td>{qcell}</td>"
            f"<td>{cat}</td>"
            f"<td class='{vcls}'>{tot}</td>"
            f"</tr></tbody>"
        )

    stale_note = " · <i>তালিকা ফ্রেশ হচ্ছে, একটু পর রিলোড করলে নতুন পোস্ট দেখা যাবে</i>" \
        if (_blog_state["loading"] or _blog_state["stale"]) else ""
    head = (
        f"<div class='sub'>মোট <b>{len(_blog_state['posts'])}</b>টি পোস্ট · নতুনগুলো সবার উপরে · "
        f"ভিউ = মুভির <b style='color:#00E5A0'>পেজ/ডিটেইল খুললেই অটোমেটিক</b> গোনা হয় (গত ৩০ দিন)"
        f"{stale_note} · "
        f"<a href='/admin?refresh=1'>🔄 ফ্রেশ করো</a></div>"
        f"<div style='display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px'>"
        f"<input class='search' id='mvSearch' type='search' placeholder='🔍 Search movie…' "
        f"onkeyup='mvFilter(this.value)' autocomplete='off'>"
        f"<button class='mini' onclick='mvSort(&quot;date&quot;)'>🆕 নতুন আগে</button>"
        f"<button class='mini' onclick='mvSort(&quot;views&quot;)'>🔥 বেশি ভিউ আগে</button>"
        f"</div>"
        f"<div class='scroll'><table id='mvTable'><thead><tr>"
        f"<th>টাইটেল</th><th>কোয়ালিটি</th><th>ক্যাটাগরি</th><th>ভিউ (৩০ দিন)</th>"
        f"</tr></thead>{''.join(groups)}</table></div>"
    )
    return head


@stats_routes.get("/admin")
async def admin_home(request):
    if request.cookies.get(COOKIE) != TOKEN:
        return _page("লগইন", """
        <div class="loginbox">
          <h1>MovieBoxBD অ্যাডমিন</h1>
          <div class="sub">প্যানেলে ঢুকতে পাসওয়ার্ড দাও</div>
          <form method="post" action="/admin/login">
            <input type="password" name="pw" placeholder="অ্যাডমিন পাসওয়ার্ড" required>
            <button type="submit">ঢুকো →</button>
          </form>
        </div>""")

    now = time.time()
    midnight = _today_start(now)
    week = midnight - 6 * DAY
    month = now - 30 * DAY

    try:
        total = await installs.count_documents({})
        active_today = await installs.count_documents({"last": {"$gte": midnight}})
        new_today = await installs.count_documents({"first": {"$gte": midnight}})
        live_now = await installs.count_documents({"last": {"$gte": now - 60}})   # 🔴 ১ মিনিটে অ্যাক্টিভ
    except Exception:
        total = active_today = new_today = 0
        live_now = 0

    joins = Counter()
    dls = Counter()
    versions = Counter()
    vnorm = Counter()   # 🎬 v4: স্বাভাবিক লেবেল → ভিউ সংখ্যা (৩০ দিন)
    try:
        async for u in installs.find({"first": {"$gte": week}}, {"first": 1}):
            joins[_day_key(float(u.get("first") or 0))] += 1
        async for u in installs.find({"last": {"$gte": month}}, {"ver": 1}):
            versions[str(u.get("ver") or "?")] += 1
        # 🆕 v4: প্লে-সিস্টেম বাদ — আবার 'view' ইভেন্ট (পেজ খুললেই অটো গোনা হয়)
        async for e in events.find({"event": "view", "ts": {"$gte": month}}, {"label": 1}):
            lab = (e.get("label") or "").strip()
            if lab:
                vnorm[_norm(lab)] += 1
        async for e in events.find({"event": "download", "ts": {"$gte": month}}, {"label": 1}):
            if e.get("label"):
                dls[e["label"]] += 1
    except Exception:
        pass

    cur = {}
    try:
        cur = await appcfg.find_one({"_id": "update"}) or {}
    except Exception:
        pass

    # ── 🎬 ব্লগের সব আপলোড ──────────────────────────────────────────
    _ensure_blog_fresh(force=request.query.get("refresh") == "1")
    movies_html = _movies_html(vnorm)

    def rows(counter):
        top = counter.most_common()   # 🎬 ক্যাপ নাই — ৩০ দিনের সবগুলো দেখাবে
        if not top:
            return "<tr><td colspan=3 style='color:#8B94B8'>এখনো কোনো ডাটা নেই</td></tr>"
        mx = max(v for _, v in top)
        out = []
        for k, v in top:
            pct = max(3, int(v * 100 / mx)) if mx else 3
            out.append(f"<tr><td>{html.escape(k)}</td>"
                       f"<td style='width:42%'><div class='dbar'><i style='width:{pct}%'></i></div></td>"
                       f"<td class='num'>{v}</td></tr>")
        return "".join(out)

    def table_html(counter, unit):
        # 🎬 ফুল লিস্ট — স্ক্রলসহ, উপরে মোট হিসাব, পাশে বার
        if not counter:
            return "<table><tr><td colspan=3 style='color:#8B94B8'>এখনো কোনো ডাটা নেই</td></tr></table>"
        tot = sum(counter.values())
        head = f"<div class='sub'>মোট {len(counter)}টি মুভি · {tot} বার {unit} — যত বেশি, তত উপরে</div>"
        return head + (f"<div class='scroll'><table>"
                       f"<tr><th>মুভি</th><th style='width:42%'></th><th>{unit}</th></tr>"
                       f"{rows(counter)}</table></div>")

    peak = max(joins.values()) if joins else 0
    bars = []
    for i in range(6, -1, -1):
        d = _day_key(now) - i
        n = joins.get(d, 0)
        wnum = int(time.strftime("%w", time.gmtime(d * DAY - TZ)))  # 0=রবি … 6=শনি
        wd = WEEKDAYS[(wnum + 6) % 7]  # লিস্টটা সোমবার থেকে শুরু
        pct = max(3, int(n * 100 / peak)) if (peak and n) else 0
        bars.append(f"<tr><td class='dwd'>{wd}</td>"
                    f"<td><div class='wbar'><i style='width:{pct}%'></i></div></td>"
                    f"<td class='num'>{n}</td></tr>")
    bars_html = "".join(bars)

    vers_html = " · ".join(f"v{html.escape(k)} — <b style='color:#8F7BFF'>{v}</b> জন"
                           for k, v in versions.most_common(8)) or "—"

    cur_html = ("🚀 চালু আছে: <b style='color:#00E5A0'>v{} ({})</b> — {} — <a href='{}'>{}</a>"
                .format(html.escape(str(cur.get("code", ""))), html.escape(str(cur.get("name", ""))),
                        html.escape(str(cur.get("msg", ""))), html.escape(str(cur.get("url", "")), quote=True),
                        "APK লিঙ্ক ✓")
                if cur else "এখনো কোনো আপডেট ঘোষণা করা হয়নি")

    body = f"""
    <h1>MovieBoxBD অ্যাডমিন প্যানেল</h1>
    <div class="sub">লাইভ স্ট্যাট — <a href="/admin/logout" style="color:#8B94B8">লগআউট</a></div>

    <div class="cards">
      <div class="card live"><div class="n" id="liveN" data-n="{live_now}">{live_now}</div><div class="t">🔴 এখন লাইভ (১ মিনিটে)</div></div>
      <div class="card"><div class="n" data-n="{active_today}">{active_today}</div><div class="t">🟢 আজ অ্যাক্টিভ</div></div>
      <div class="card"><div class="n" data-n="{new_today}">{new_today}</div><div class="t">✨ আজ নতুন জয়েন</div></div>
      <div class="card"><div class="n" data-n="{total}">{total}</div><div class="t">👥 মোট ইউজার</div></div>
    </div>
    <div class="box" id="liveUsersBox" style="display:none;margin-top:10px">
      <div style="font-weight:700;margin-bottom:4px">👀 কে কী দেখছে — লাইভ (১ মিনিটে)</div>
      <div id="liveUsers"></div>
    </div>
    <div class="sub">🔴 লাইভ সংখ্যা আর "👀 কে কী দেখছে" লিস্ট প্রতি ১ মিনিটে নিজে নিজে আপডেট হয় · পেজ রিফ্রেশ দিলেই সাথে সাথে হার্টবিট থেকে টাটকা রেজাল্ট।</div>

    <h2>🎬 আপলোড হওয়া মুভি/সিরিজ — ব্লগের সবগুলো</h2>
    {movies_html}

    <h2>📅 শেষ ৭ দিনে জয়েন (প্রতিদিন)</h2>
    <table>{bars_html}</table>

    <h2>⬇ ডাউনলোড হয়েছে যেসব মুভি (৩০ দিন — সবগুলো)</h2>
    {table_html(dls, "ডাউনলোড")}

    <h2>📱 ভার্সন বিতরণ</h2>
    <div class="cur">{vers_html}</div>

    <h2>🚀 অ্যাপ আপডেট ব্রডকাস্ট</h2>
    <div class="cur">{cur_html}</div>
    <form method="post" action="/admin/setupdate">
      <div class="sub" style="margin-bottom:6px">নতুন APK Release-এ উঠলে এখানে ভার্সন দাও —
      সব ইউজারের অ্যাপে উপরে সবুজ ব্যানার + নোটিফিকেশন যাবে (অ্যাপ খুললে / ১৫ মিনিট পরপর)।</div>
      <label>নতুন versionCode (build.gradle-এর versionCode — যেমন 7)</label>
      <input name="code" type="number" min="1" required>
      <label>ভার্সন নাম (যেমন 2.6)</label>
      <input name="name" placeholder="2.6" required>
      <label>APK ডাউনলোড লিঙ্ক (GitHub Release-এর MovieBoxBD.apk লিঙ্ক)</label>
      <input name="url" type="url" placeholder="https://github.com/Sakib122/MovieBoxBD-App/releases/…" required>
      <label>ছোট মেসেজ (ইউজার কী পাবে — যেমন: সাউন্ড সমস্যা ফিক্স)</label>
      <input name="msg" placeholder="নতুন ফিচার ও ফিক্স">
      <button type="submit">🚀 আপডেট ঘোষণা করো</button>
      <button type="submit" formaction="/admin/clearupdate" class="gray">⏹ আপডেট বন্ধ করো</button>
    </form>
    <script>{ADMIN_JS}</script>
    """
    return _page("অ্যাডমিন প্যানেল", body)


@stats_routes.post("/admin/login")
async def admin_login(request):
    form = await request.post()
    if str(form.get("pw", "")) == ADMIN_PASSWORD:
        resp = web.HTTPFound("/admin")
        resp.set_cookie(COOKIE, TOKEN, max_age=86400 * 7, httponly=True, path="/")
        return resp
    return web.HTTPFound("/admin")


@stats_routes.get("/admin/logout")
async def admin_logout(request):
    resp = web.HTTPFound("/admin")
    resp.del_cookie(COOKIE, path="/")
    return resp


@stats_routes.post("/admin/setupdate")
async def admin_setupdate(request):
    if request.cookies.get(COOKIE) != TOKEN:
        return web.HTTPFound("/admin")
    try:
        form = await request.post()
        code = int(str(form.get("code", "0") or 0))
        if code > 0:
            await appcfg.update_one(
                {"_id": "update"},
                {"$set": {
                    "code": code,
                    "name": str(form.get("name", ""))[:24],
                    "url": str(form.get("url", ""))[:500],
                    "msg": str(form.get("msg", ""))[:200],
                    "ts": time.time(),   # 🆕 প্রতিবার ঘোষণা = নতুন পিং — সব পুরনো ফোন আবার পাবে
                }},
                upsert=True,
            )
    except Exception:
        pass
    return web.HTTPFound("/admin")


@stats_routes.post("/admin/clearupdate")
async def admin_clearupdate(request):
    if request.cookies.get(COOKIE) != TOKEN:
        return web.HTTPFound("/admin")
    try:
        await appcfg.delete_one({"_id": "update"})
    except Exception:
        pass
    return web.HTTPFound("/admin")
