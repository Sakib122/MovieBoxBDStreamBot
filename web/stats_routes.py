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
#     • কোন মুভি কতবার দেখা/প্লে/ডাউনলোড হয়েছে
#     • কোন ভার্সন কতজন ব্যবহার করছে
#     • 🚀 আপডেট ব্রডকাস্ট: ভার্সন + APK লিংক দিলেই সব অ্যাপে ব্যানার+নোটিফিকেশন
#
# পাসওয়ার্ড: Koyeb-এ ADMIN_PASSWORD এনভায়রনমেন্ট ভ্যারিয়েবল দাও
#   (না দিলে ডিফল্ট "moviebox@2026" — দ্রুত বদলাবে!)
# ══════════════════════════════════════════════════════════════════

import os
import re
import time
import hashlib
import html
from collections import Counter

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
  .scroll{{max-height:420px;overflow-y:auto}}
  .bar{{color:#00E5A0;letter-spacing:1px}}
  form{{background:#171A2E;border-radius:12px;padding:16px;max-width:640px}}
  input,textarea{{width:100%;box-sizing:border-box;background:#0D0F1C;border:1px solid #2A2F4F;
    color:#ECEFF7;border-radius:8px;padding:9px 11px;font-size:14px;margin:4px 0 10px}}
  button{{background:#7C4DFF;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}}
  button.gray{{background:#2A2F4F}} a{{color:#00E5A0}} .cur{{background:#232742;border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:14px}}
</style></head><body>{body}</body></html>""", content_type="text/html")


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
        gone = await installs.count_documents({"last": {"$lt": now - 2 * DAY}})   # 🗑 ২ দিন নিরব = আনইনস্টল (আনুমানিক)
    except Exception:
        total = active_today = new_today = 0
        live_now = gone = 0

    joins = Counter()
    views, plays, dls = Counter(), Counter(), Counter()
    versions = Counter()
    try:
        async for u in installs.find({"first": {"$gte": week}}, {"first": 1}):
            joins[_day_key(float(u.get("first") or 0))] += 1
        async for u in installs.find({"last": {"$gte": month}}, {"ver": 1}):
            versions[str(u.get("ver") or "?")] += 1
        async for e in events.find({"event": "view", "ts": {"$gte": month}}, {"label": 1}):
            if e.get("label"):
                views[e["label"]] += 1
        async for e in events.find({"event": "play", "ts": {"$gte": month}}, {"label": 1}):
            if e.get("label"):
                plays[e["label"]] += 1
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
        total = sum(counter.values())
        head = f"<div class='sub'>মোট {len(counter)}টি মুভি · {total} বার {unit} — যত বেশি দেখা, তত উপরে</div>"
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
                        "APK লিংক ✓")
                if cur else "এখনো কোনো আপডেট ঘোষণা করা হয়নি")

    body = f"""
    <h1>📊 MovieBoxBD অ্যাডমিন প্যানেল</h1>
    <div class="sub">লাইভ স্ট্যাট — <a href="/admin/logout" style="color:#8A93B2">লগআউট</a></div>

    <div class="cards">
      <div class="card"><div class="n" id="liveN">{live_now}</div><div class="t">🔴 এখন লাইভ (১ মিনিটে)</div></div>
      <div class="card"><div class="n">{active_today}</div><div class="t">🟢 আজ অ্যাক্টিভ</div></div>
      <div class="card"><div class="n">{new_today}</div><div class="t">✨ আজ নতুন জয়েন</div></div>
      <div class="card"><div class="n">{total}</div><div class="t">👥 মোট ইউজার</div></div>
      <div class="card"><div class="n">{gone}</div><div class="t">🗑 আনইনস্টল (আনুমানিক)</div></div>
    </div>
    <div class="sub">🗑 আনইনস্টল = গত ২ দিনে সার্ভারে কোনো খবর পাওয়া যায়নি এমন ফোন (আনুমানিক)।
    🔴 লাইভ সংখ্যা প্রতি ৩০ সেকেন্ডে নিজে নিজে আপডেট হয়।</div>

    <h2>📅 শেষ ৭ দিনে জয়েন (প্রতিদিন)</h2>
    <table>{bars_html}</table>

    <h2>👀 দেখা হয়েছে যেসব মুভি (৩০ দিন — সবগুলো)</h2>
    {table_html(views, "ভিউ")}

    <h2>▶ প্লে হয়েছে যেসব মুভি (৩০ দিন — সবগুলো)</h2>
    {table_html(plays, "প্লে")}

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
      <label>APK ডাউনলোড লিংক (GitHub Release-এর MovieBoxBD.apk লিংক)</label>
      <input name="url" type="url" placeholder="https://github.com/Sakib122/MovieBoxBD-App/releases/…" required>
      <label>ছোট মেসেজ (ইউজার কী পাবে — যেমন: সাউন্ড সমস্যা ফিক্স)</label>
      <input name="msg" placeholder="নতুন ফিচার ও ফিক্স">
      <button type="submit">🚀 আপডেট ঘোষণা করো</button>
      <button type="submit" formaction="/admin/clearupdate" class="gray">⏹ আপডেট বন্ধ করো</button>
    </form>
        <script>
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
