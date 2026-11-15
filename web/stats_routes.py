# ══════════════════════════════════════════════════════════════════
# 📊 MovieBoxBD অ্যাডমিন প্যানেল + অ্যাপ-ট্র্যাকিং API (aiohttp)
#
# অ্যাপ থেকে আসে:
#   POST /api/track      → ইনস্টল / অ্যাপ-ওপেন / ভিউ / প্লে / ডাউনলোড
#   GET  /api/app/latest → নতুন আপডেট আছে কিনা (versionCode তুলনা অ্যাপেই হয়)
#
# সাকিব ভাইয়ের জন্য:
#   GET  /admin          → পাসওয়ার্ড দিয়ে লগইন → লাইভ ড্যাশবোর্ড
#     • মোট ইউজার, আজ অ্যাক্টিভ, প্রতিদিন কতজন জয়েন
#     • 🎬 ব্লগের সবগুলো আপলোডের টেবিল — নতুনগুলো সবার উপরে, সার্চ অপশন,
#       প্রতি মুভির ভিউ কাউন্ট, ব্লগার-এডিট + প্যানেল-হাইড বাটন (v2 নতুন)
#     • ⬇ কোন মুভি বেশি ডাউনলোড হয়েছে — ওই সিস্টেম অক্ষত
#     • 🚀 আপডেট ব্রডকাস্ট: ভার্সন + APK লিংক দিলেই সব অ্যাপে ব্যানার+নোটিফিকেশন
#   v2 পরিবর্তন: আনইনস্টল কার্ড বাদ (ডিমোটিভেটিং ছিল), ব্লগ-মুভি টেবিল যোগ
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
hidden_db = mydb["mbx_hidden"]      # {_id: postId, ts} — প্যানেল থেকে লুকানো পোস্ট

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
    m = re.search(r"\b(2160|1080|720|480|360)p\b", title, re.IGNORECASE)
    if m:
        return m.group(0).lower()
    if re.search(r"\b4\s?k\b", title, re.IGNORECASE):
        return "4K"
    m = re.search(r"\bEP\s*\d+(\s*-\s*\d+)?\b", title, re.IGNORECASE)
    if m:
        return m.group(0).upper().replace(" ", "")
    if re.search(r"\bHD\b", title, re.IGNORECASE):
        return "HD"
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
                                     headers={"User-Agent": "MovieBoxBD-Panel/2.0"}) as s:
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
        if event == "play":   # 🎬 প্লে-রিপোর্টে আসল মুভির নাম + কোয়ালিটি/EP বসিয়ে দাও
            label = await _play_label(iid, label, now)
        if event != "beat":   # 💓 হার্টবিট ইভেন্ট-লিস্টে জমা হয় না — শুধু 'last' টাইম আপডেট হয়
            await events.insert_one({"id": iid, "event": event, "label": label,
                                     "ver": ver, "ts": now})
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"ok": False}, status=400)

@stats_routes.get("/api/live")
async def api_live(request):
    """🔴 লাইভ কাউন্ট — প্যানেলের কার্ড প্রতি ৩০ সেকেন্ডে নিজে নিজে আপডেট হয় এটা দিয়ে"""
    if request.cookies.get(COOKIE) != TOKEN:
        return web.json_response({"live": 0})
    try:
        n = await installs.count_documents({"last": {"$gte": time.time() - 60}})
        return web.json_response({"live": n})
    except Exception:
        return web.json_response({"live": 0})

@stats_routes.get("/api/app/latest")
async def api_latest(request):
    try:
        doc = await appcfg.find_one({"_id": "update"})
        if not doc:
            return web.json_response({"code": 0})
        return web.json_response({
            "code": int(doc.get("code", 0)),
            "name": str(doc.get("name", "")),
            "url": str(doc.get("url", "")),
            "msg": str(doc.get("msg", "")),
            "ts": float(doc.get("ts", 0)),   # 🆕 ঘোষণার সময় — প্রতিবার নতুন ঘোষণা = নতুন পিং
        })
    except Exception:
        return web.json_response({"code": 0})


# ────────────────────────── অ্যাডমিন UI ──────────────────────────

def _page(title: str, body: str) -> web.Response:
    return web.Response(text=f"""<!DOCTYPE html>
<html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body{{background:#0D0F1C;color:#ECEFF7;font-family:'Noto Sans Bengali',sans-serif;margin:0;padding:20px}}
  h1{{font-size:22px;color:#00E5A0;margin:0 0 4px}} .sub{{color:#8A93B2;font-size:13px;margin-bottom:18px}}
  .cards{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}
  .card{{background:#171A2E;border-radius:12px;padding:14px 18px;min-width:140px;flex:1}}
  .card .n{{font-size:26px;font-weight:700;color:#7C4DFF}}
  .card .t{{font-size:13px;color:#8A93B2;margin-top:2px}}
  h2{{font-size:16px;color:#00E5A0;margin:22px 0 8px}}
  table{{width:100%;border-collapse:collapse;background:#171A2E;border-radius:12px;overflow:hidden}}
  th,td{{padding:8px 12px;text-align:left;font-size:14px;border-bottom:1px solid #232742}}
  th{{color:#8A93B2;font-weight:600;background:#1B1F36}}
  td.num{{color:#7C4DFF;font-weight:700}}
  td .pid{{color:#5A6284;font-size:11px;margin-top:2px}}
  .scroll{{max-height:460px;overflow-y:auto}}
  .bar{{color:#00E5A0;letter-spacing:1px}}
  form{{background:#171A2E;border-radius:12px;padding:16px;max-width:640px}}
  form.inline{{background:none;padding:0;max-width:none;display:inline;margin:0}}
  input,textarea{{width:100%;box-sizing:border-box;background:#0D0F1C;border:1px solid #2A2F4F;
    color:#ECEFF7;border-radius:8px;padding:9px 11px;font-size:14px;margin:4px 0 10px}}
  input.search{{width:auto;max-width:280px;display:inline-block;margin:0;vertical-align:middle}}
  button{{background:#7C4DFF;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}}
  button.gray{{background:#2A2F4F}}
  button.mini{{background:#232742;color:#DCE3FF;border:1px solid #2A2F4F;border-radius:7px;
    padding:5px 10px;font-size:12px;cursor:pointer;margin:2px}}
  button.mini:hover{{background:#2A2F4F}}
  button.warn{{background:#3A1F26;color:#FF9C9C;border-color:#5A2A34}}
  a.mini{{background:#1E2A4A;color:#00E5A0;text-decoration:none;border-radius:7px;
    padding:5px 10px;font-size:12px;display:inline-block;border:1px solid #2A3F55;margin:2px}}
  .chip{{display:inline-block;background:#232742;border-radius:14px;padding:4px 10px;margin:3px;font-size:12px}}
  .hot{{color:#00E5A0}}
  .box{{background:#171A2E;border:1px solid #2A2F4F;border-radius:12px;padding:14px;font-size:14px;color:#8A93B2}}
  a{{color:#00E5A0}} .cur{{background:#232742;border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:14px}}
</style></head><body>{body}</body></html>""", content_type="text/html")


@stats_routes.post("/admin/hidemovie")
async def admin_hidemovie(request):
    """🙈 প্যানেলের তালিকা থেকে পোস্ট লুকাও (ব্লগ থেকে ডিলিট হয় না)"""
    if request.cookies.get(COOKIE) != TOKEN:
        return web.HTTPFound("/admin")
    try:
        form = await request.post()
        pid = str(form.get("pid", "")).strip()[:32]
        if pid:
            await hidden_db.update_one({"_id": pid}, {"$set": {"ts": time.time()}}, upsert=True)
    except Exception:
        pass
    return web.HTTPFound("/admin")


@stats_routes.post("/admin/showmovie")
async def admin_showmovie(request):
    """👁 লুকানো পোস্ট আবার দেখাও"""
    if request.cookies.get(COOKIE) != TOKEN:
        return web.HTTPFound("/admin")
    try:
        form = await request.post()
        pid = str(form.get("pid", "")).strip()[:32]
        if pid:
            await hidden_db.delete_one({"_id": pid})
    except Exception:
        pass
    return web.HTTPFound("/admin")


@stats_routes.get("/admin")
async def admin_home(request):
    if request.cookies.get(COOKIE) != TOKEN:
        return _page("লগইন", """
        <div style="max-width:360px;margin:60px auto;text-align:center">
          <h1>📊 MovieBoxBD অ্যাডমিন</h1>
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
    vnorm = Counter()   # 🎬 স্বাভাবিকীকৃত লেবেল → view+play সংখ্যা (৩০ দিন)
    try:
        async for u in installs.find({"first": {"$gte": week}}, {"first": 1}):
            joins[_day_key(float(u.get("first") or 0))] += 1
        async for u in installs.find({"last": {"$gte": month}}, {"ver": 1}):
            versions[str(u.get("ver") or "?")] += 1
        async for e in events.find({"event": {"$in": ["view", "play"]},
                                    "ts": {"$gte": month}}, {"label": 1}):
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
    hidden_pids = set()
    hidden_docs = []
    try:
        hcur = hidden_db.find({}, {"_id": 1, "ts": 1}).limit(500)
        hidden_docs = [d async for d in hcur]
        hidden_pids = {str(d["_id"]) for d in hidden_docs}
    except Exception:
        pass

    blog_posts = [p for p in _blog_state["posts"] if p["pid"] not in hidden_pids]

    def _views_for(title: str) -> int:
        nt = _norm(title)
        if not nt or len(nt) < 6:
            return 0
        v = vnorm.get(nt, 0)
        if v:
            return v
        pref = nt + " "
        for nl, c in vnorm.items():
            if nl.startswith(pref):
                v += c
        return v

    def _movie_rows() -> str:
        if not blog_posts:
            return ""
        out = []
        for p in blog_posts:
            title = p["title"]
            v = _views_for(title)
            q = _title_quality(title)
            cat = " · ".join(p["labels"][:2]) if p["labels"] else "—"
            pub = html.escape(p["pub"] or "—")
            if _blog_state["blog_id"]:
                edit = f"https://www.blogger.com/blog/post/edit/{_blog_state['blog_id']}/{p['pid']}"
                edit_btn = (f"<a class='mini' href='{html.escape(edit, quote=True)}' "
                            f"target='_blank' rel='noopener'>✏️ এডিট</a>")
            else:
                edit_btn = ""
            t_show = html.escape(title[:90] + ("…" if len(title) > 90 else ""))
            vcls = "num hot" if v >= 100 else "num"
            out.append(
                f"<tr data-v='{v}' data-d='{pub}'>"
                f"<td><b>{t_show}</b>"
                f"<div class='pid'>ID: {html.escape(p['pid'][:12])}… · {pub}</div></td>"
                f"<td>{html.escape(q)}</td>"
                f"<td>{html.escape(cat)}</td>"
                f"<td class='{vcls}'>{v}</td>"
                f"<td>{edit_btn}"
                f"<form class='inline' method='post' action='/admin/hidemovie'>"
                f"<input type='hidden' name='pid' value='{html.escape(p['pid'], quote=True)}'>"
                f"<button class='mini warn' onclick=\"return confirm('প্যানেল থেকে লুকানো হবে — ব্লগ থেকে ডিলিট হবে না, ঠিক আছে?')\">🙈 লুকাও</button>"
                f"</form></td></tr>"
            )
        return "".join(out)

    if not BLOG_URL:
        movies_html = ("<div class='box'>⚙️ ব্লগ-টেবিল চালু করতে Koyeb-এ "
                       "<b style='color:#ECEFF7'>BLOG_URL</b> এনভায়রনমেন্ট ভ্যারিয়েবলে তোমার ব্লগের "
                       "লিঙ্ক দাও (যেমন https://movieboxbd.blogspot.com) — তারপর সার্ভিস রিস্টার্ট। "
                       "বাকি সব এখনই কাজ করছে।</div>")
    elif not blog_posts:
        if _blog_state["loading"] or not _blog_state["ts"]:
            movies_html = ("<div class='box'>⏳ ব্লগ থেকে সব পোস্ট আনা হচ্ছে… "
                           "২০-৩০ সেকেন্ড পর পেজটা একবার রিফ্রেশ করো।"
                           "<script>setTimeout(function(){location.reload()},25000)</script></div>")
        else:
            movies_html = (f"<div class='box'>⚠️ ব্লগ পড়া যায়নি: "
                           f"{html.escape(_blog_state['err'] or 'কোনো পোস্ট পাওয়া যায়নি')} — "
                           f"BLOG_URL ঠিক আছে কিনা দেখো। <a href='/admin?refresh=1'>আবার চেষ্টা</a></div>")
    else:
        stale_note = " · <i>তালিকা ফ্রেশ হচ্ছে, একটু পর রিলোড করলে নতুন পোস্ট দেখা যাবে</i>" \
            if (_blog_state["loading"] or _blog_state["stale"]) else ""
        movies_html = (
            f"<div class='sub'>মোট <b style='color:#ECEFF7'>{len(blog_posts)}</b>টি পোস্ট "
            f"(লুকানো {len(hidden_docs)}টা বাদে) · নতুনগুলো সবার উপরে · "
            f"ভিউ = গত ৩০ দিনের view+play{stale_note} · "
            f"<a href='/admin?refresh=1'>🔄 ফ্রেশ করো</a></div>"
            f"<div style='display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px'>"
            f"<input class='search' id='mvSearch' type='search' placeholder='🔍 Search movie…' "
            f"onkeyup='mvFilter(this.value)' autocomplete='off'>"
            f"<button class='mini' onclick='mvSort(&quot;date&quot;)'>🆕 নতুন আগে</button>"
            f"<button class='mini' onclick='mvSort(&quot;views&quot;)'>🔥 বেশি ভিউ আগে</button>"
            f"</div>"
            f"<div class='scroll'><table id='mvTable'><thead><tr>"
            f"<th>টাইটেল</th><th>কোয়ালিটি</th><th>ক্যাটাগরি</th><th>ভিউ</th><th>অ্যাকশন</th>"
            f"</tr></thead><tbody>{_movie_rows()}</tbody></table></div>"
        )

    hidden_html = ""
    if hidden_docs:
        chips = []
        for d in hidden_docs:
            pid = str(d["_id"])
            chips.append(
                f"<form class='inline' method='post' action='/admin/showmovie'>"
                f"<input type='hidden' name='pid' value='{html.escape(pid, quote=True)}'>"
                f"<span class='chip'>🙈 {html.escape(pid[:10])}… "
                f"<button class='mini' type='submit'>দেখাও</button></span></form>"
            )
        hidden_html = f"<h2>🙈 লুকানো পোস্ট ({len(hidden_docs)})</h2><div>{''.join(chips)}</div>"

    def rows(counter):
        top = counter.most_common()   # 🎬 ক্যাপ নাই — ৩০ দিনের সবগুলো দেখাবে, কিছু বাদ যাবে না
        if not top:
            return "<tr><td colspan=2 style='color:#8A93B2'>এখনো কোনো ডাটা নেই</td></tr>"
        return "".join(
            f"<tr><td>{html.escape(k)}</td><td class='num'>{v}</td></tr>" for k, v in top)

    def table_html(counter, unit):
        # 🎬 ফুল লিস্ট — স্ক্রলসহ, উপরে মোট হিসাব
        if not counter:
            return "<table><tr><td colspan=2 style='color:#8A93B2'>এখনো কোনো ডাটা নেই</td></tr></table>"
        tot = sum(counter.values())
        head = f"<div class='sub'>মোট {len(counter)}টি মুভি · {tot} বার {unit} — যত বেশি দেখা, তত উপরে</div>"
        return head + f"<div class='scroll'><table><tr><th>মুভি</th><th>{unit}</th></tr>{rows(counter)}</table></div>"

    peak = max(joins.values()) if joins else 0
    bars = []
    for i in range(6, -1, -1):
        d = _day_key(now) - i
        n = joins.get(d, 0)
        wnum = int(time.strftime("%w", time.gmtime(d * DAY - TZ)))  # 0=রবি … 6=শনি
        wd = WEEKDAYS[(wnum + 6) % 7]  # লিস্টটা সোমবার থেকে শুরু
        w = max(1, int(n * 22 / peak)) if peak else 1
        bars.append(f"<tr><td style='width:64px'>{wd}</td>"
                    f"<td class='bar'>{'▇' * w}</td><td class='num'>{n}</td></tr>")
    bars_html = "".join(bars)

    vers_html = " · ".join(f"v{html.escape(k)} — <b style='color:#7C4DFF'>{v}</b> জন"
                           for k, v in versions.most_common(8)) or "—"

    cur_html = ("🚀 চালু আছে: <b style='color:#00E5A0'>v{} ({})</b> — {} — <a href='{}'>{}</a>"
                .format(html.escape(str(cur.get("code", ""))), html.escape(str(cur.get("name", ""))),
                        html.escape(str(cur.get("msg", ""))), html.escape(str(cur.get("url", "")), quote=True),
                        "APK লিঙ্ক ✓")
                if cur else "এখনো কোনো আপডেট ঘোষণা করা হয়নি")

    body = f"""
    <h1>📊 MovieBoxBD অ্যাডমিন প্যানেল</h1>
    <div class="sub">লাইভ স্ট্যাট — <a href="/admin/logout" style="color:#8A93B2">লগআউট</a></div>

    <div class="cards">
      <div class="card"><div class="n" id="liveN">{live_now}</div><div class="t">🔴 এখন লাইভ (১ মিনিটে)</div></div>
      <div class="card"><div class="n">{active_today}</div><div class="t">🟢 আজ অ্যাক্টিভ</div></div>
      <div class="card"><div class="n">{new_today}</div><div class="t">✨ আজ নতুন জয়েন</div></div>
      <div class="card"><div class="n">{total}</div><div class="t">👥 মোট ইউজার</div></div>
    </div>
    <div class="sub">🔴 লাইভ সংখ্যা প্রতি ৩০ সেকেন্ডে নিজে নিজে আপডেট হয়।</div>

    <h2>🎬 আপলোড হওয়া মুভি/সিরিজ — ব্লগের সবগুলো</h2>
    {movies_html}
    {hidden_html}

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
    <script>
    function mvFilter(q) {{
      q = q.toLowerCase();
      document.querySelectorAll('#mvTable tbody tr').forEach(function(tr) {{
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) > -1 ? '' : 'none';
      }});
    }}
    function mvSort(key) {{
      var tb = document.querySelector('#mvTable tbody');
      if (!tb) return;
      var rows = [].slice.call(tb.querySelectorAll('tr'));
      rows.sort(function(a, b) {{
        if (key === 'views') return (parseInt(b.getAttribute('data-v')) || 0) - (parseInt(a.getAttribute('data-v')) || 0);
        return (b.getAttribute('data-d') || '').localeCompare(a.getAttribute('data-d') || '');
      }});
      rows.forEach(function(r) {{ tb.appendChild(r); }});
    }}
    setInterval(async () => {{
      try {{
        const r = await fetch('/api/live');
        const j = await r.json();
        const el = document.getElementById('liveN');
        if (el && el.textContent !== String(j.live)) el.textContent = j.live;
      }} catch (e) {{}}
    }}, 30000);
    </script>
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
