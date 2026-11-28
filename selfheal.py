# ============================================================
# 🛡️ SELF-HEAL SYSTEM — MovieBoxBDStreamBot (অটো-সেলফ-হিলিং)
# ============================================================
# এই ফাইলটা সার্ভারকে সম্পূর্ণ স্বয়ংক্রিয় (fully automatic) করে দেয়:
#
# ১) 🧠 MEMORY WATCHDOG + ESCALATION (প্রধান প্রাধান্য):
#    - প্রতি ৫ সেকেন্ডে RAM চেক করে (Koyeb/Render/Docker কনটেইনার লিমিট সহ)
#    - RAM টার্গেট (SELFHEAL_MEM_LIMIT, ডিফল্ট ৯০%) ছুঁলে = ১টি "ক্রসিং":
#      ১ম-৫ম ক্রসিং → পূর্ণ মেমোরি ক্লিন + রিসেট (Android Clear Cache-এর মতো) —
#        RAM নিচে নেমে আসে, মেমোরি টার্গেট আবার শূন্য থেকে শুরু হয়।
#        প্রতি প্রচেষ্টার খবর লগ চ্যানেলে যায় (RAM কত % হয়েছে, ক্লিন হচ্ছে)।
#      ৬ষ্ঠ ক্রসিং → ক্লিনেও ধরেনি — তখনই নিজে নিজে REDEPLOY:
#        Koyeb API (POST /v1/services/{id}/redeploy) দিয়ে ড্যাশবোর্ডের
#        Redeploy বাটন চাপার মতোই নতুন deployment ট্রিগার হয়। Koyeb নতুন
#        instance বিল্ড করে healthy হলে অটোমেটিক swap করে — নতুন instance-এ
#        মেমোরি ০ (জিরো) থেকে শুরু, ফাইল/ডাটা/থাম্বনেইল সব অক্ষত থাকে।
#    - RAM টানা ১৫ মিনিট স্বাভাবিক থাকলে ক্লিন-গণনা আবার ০ থেকে শুরু হয়।
#    - টোকেন না থাকলে/API ফেল হলে → fresh restart fallback (exec):
#      প্রসেস নিজেকে বদলে নেয়, মেমোরি জিরো হয় — সার্ভার কখনো মরে না।
#    - ⚠️ CPU কখনোই ট্রিগার নয় — CPU 100% হলেও কিছু হবে না।
#      ট্রিগারের একমাত্র কারণ = মেমোরি।
#
# ২) 🧹 AUTO CACHE CLEANER (প্রতি ৩০ মিনিটে):
#    - অ্যান্ড্রয়েডের "Clear Cache"-এর মতোই কাজ করে:
#      অপ্রয়োজনীয় জাঙ্ক/টেম্প ফাইল মুছে যায়, বাকি সব অক্ষত থাকে।
#    - শুধুমাত্র নির্দিষ্ট জাঙ্ক প্যাটার্ন (*.tmp, *.part, __pycache__ ইত্যাদি)
#      মোছা হয় — হোয়াইটলিস্ট ছাড়া কিছুই মোছা হয় না।
#    - কখনোই স্পর্শ হয় না: ফাইল ডাটাবেস, থাম্বনেইল, .session, .env,
#      কনফিগ, সোর্স কোড — কিছুই ডিলিট হবে না, থাম্বনেইল দেখা যাবে।
#    - ক্লিন হওয়ার সময় বট এক মুহূর্তের জন্যও বন্ধ হয় না।
#    - 🤫 এই রুটিন ক্লিন সম্পূর্ণ সাইলেন্ট — লগ চ্যানেলে কিছু পাঠায় না
#      (শুধু কনসোল লগে দেখা যায়)। লগ চ্যানেলে যায় শুধু RAM-টার্গেট
#      ক্লিন আর redeploy-এর মেসেজ — চ্যানেল পরিষ্কার থাকে।
#
# ৩) ♻️ RAM টিডি-আপ: পুরনো in-memory থাম্বনেইল ক্যাশ + Python garbage
#    প্রতি সাইকেলে পরিষ্কার হয় → দুই রিলোডের মাঝে RAM কম থাকে।
#
# ৪) 🔁 BOOT-LOOP PROTECTION: টানা রিলোড হলে ব্যাক-অফ কুলডাউন —
#    সার্ভার ২৪/৭ চালু থাকে, কখনো আটকে যায় না।
#
# সব সেটিং info.py / environment ভ্যারিয়েবলে বদলানো যায়।
# ============================================================

import os
import sys
import time
import fnmatch
import gc
import shutil
import logging
import threading
import traceback

logger = logging.getLogger("selfheal")

_START_TIME = time.time()
_started = False

# ------------------------------------------------------------
# কনফিগ (info.py থেকে আসে — না থাকলে এই ডিফল্ট)
# ------------------------------------------------------------
try:
    from info import (
        SELFHEAL_ENABLED,
        SELFHEAL_MEM_LIMIT,
        SELFHEAL_CACHE_INTERVAL,
        SELFHEAL_CHECK_INTERVAL,
        KOYEB_API_TOKEN,
        KOYEB_SERVICE_NAME,
    )
except Exception:
    SELFHEAL_ENABLED = True
    SELFHEAL_MEM_LIMIT = 95
    SELFHEAL_CACHE_INTERVAL = 1800
    SELFHEAL_CHECK_INTERVAL = 5
    KOYEB_API_TOKEN = ""
    KOYEB_SERVICE_NAME = "FlixBoxsStreamBot"

# ঐচ্ছিক কনফিগ — আলাদা try-ব্লকে, যাতে info.py-এর পুরনো কপিতেও না ভাঙে
try:
    from info import KOYEB_SERVICE_ID, BIN_CHANNEL, URL
except Exception:
    KOYEB_SERVICE_ID = ""
    BIN_CHANNEL = 0
    URL = ""

try:
    from info import (
        SELFHEAL_LOG_CLEAN_ENABLED,
        SELFHEAL_LOG_CLEAN_INTERVAL,
        SELFHEAL_LOG_CLEAN_KEEP_PINNED,
    )
except Exception:
    SELFHEAL_LOG_CLEAN_ENABLED = True
    SELFHEAL_LOG_CLEAN_INTERVAL = 300   # ৩০০ সেকেন্ড = ৫ মিনিট
    SELFHEAL_LOG_CLEAN_KEEP_PINNED = True

try:
    from info import (
        SELFHEAL_MAX_CLEANS,
        SELFHEAL_REARM_SECONDS,
        SELFHEAL_CLEAN_RESET_SECONDS,
    )
except Exception:
    SELFHEAL_MAX_CLEANS = 5             # সর্বোচ্চ ৫ বার মেমোরি ক্লিন
    SELFHEAL_REARM_SECONDS = 120        # RAM উপরে থাকলে প্রতি ২ মিনিটে পরের প্রচেষ্টা
    SELFHEAL_CLEAN_RESET_SECONDS = 900  # RAM টানা ১৫ মিনিট স্বাভাবিক থাকলে গণনা রিসেট

BOOT_GRACE = 90          # প্রসেস চালু হওয়ার পর প্রথম ৯০ সেকেন্ড রিলোড বন্ধ (বুট স্থিতি)
RELOAD_CONFIRM = 2       # টানা কত চেকে টার্গেট+ দেখলে কনফার্ম (false spike এড়াতে)
RELOAD_LOG_FILE = "/tmp/selfheal_reloads.log"
RELOAD_COOLDOWN_WINDOW = 1800   # ৩০ মিনিটে ৩+ রিলোড হলে কুলডাউন
RELOAD_COOLDOWN_MAX = 3
RELOAD_COOLDOWN_SLEEP = 600     # কুলডাউনে ১০ মিনিট অপেক্ষা (সার্ভার চালুই থাকে)

MIN_JUNK_AGE = 900       # ১৫ মিনিটের পুরনো জাঙ্ক ফাইলই মুছবে (চলমান ফাইল রক্ষা)

# ------------------------------------------------------------
# 📊 মেমোরি রিডার — কনটেইনার-অ্যাওয়ার (Koyeb/Render/Docker/VPS সবখানে কাজ করে)
# ------------------------------------------------------------
def _read_file_int(path):
    try:
        with open(path, "r") as f:
            v = f.read().strip()
        return int(v) if v and v != "max" else None
    except Exception:
        return None


def _cgroup_memory():
    """Docker/Koyeb কনটেইনারের আসল মেমোরি লিমিট পড়ে (v2 আগে, তারপর v1)।"""
    # cgroup v2
    usage = _read_file_int("/sys/fs/cgroup/memory.current")
    limit = _read_file_int("/sys/fs/cgroup/memory.max")
    if usage is not None and limit and limit > 0:
        return usage, limit
    # cgroup v1
    usage = _read_file_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    limit = _read_file_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if usage is not None and limit and 0 < limit < (1 << 60):
        return usage, limit
    return None


def memory_snapshot():
    """
    রিটার্ন করে: (percent, used_mb, total_mb)
    - কনটেইনার লিমিট থাকলে সেটার হিসাব (Koyeb ড্যাশবোর্ডে যা দেখা যায় সেটাই)
    - না থাকলে পুরো সিস্টেম RAM
    """
    cg = _cgroup_memory()
    if cg:
        used, limit = cg
        pct = (used / limit) * 100.0
        return min(pct, 100.0), used / (1024 * 1024), limit / (1024 * 1024)
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.percent, vm.used / (1024 * 1024), vm.total / (1024 * 1024)
    except Exception:
        return 0.0, 0.0, 0.0


# ------------------------------------------------------------
# 🧹 জাঙ্ক-ফাইল ক্লিনার — হোয়াইটলিস্ট-অনলি (Android "Clear Cache"-এর মতো)
# ------------------------------------------------------------
# নিয়ম: শুধু নিচের প্যাটার্নের ফাইলই মোছা হবে। বাকি সবকিছু
# (ফাইল ডাটাবেস, থাম্বনেইল, সেশন, কনফিগ, কোড) সম্পূর্ণ অক্ষত থাকবে।
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

JUNK_FILE_PATTERNS = [
    "*.tmp", "*.part", "*.partial", "*.downloading", "*.download",
    "*.pyc", "*.pyo", "*.swp", "*.bak_tmp",
    ".DS_Store", "Thumbs.db",
]

JUNK_DIR_NAMES = {"__pycache__", ".pytest_cache"}

# এই ফাইলগুলো প্লাগিন দিয়ে তৈরি হয় আর কাজ শেষে রয়ে যায় — জাঙ্ক
SPECIAL_JUNK_FILES = {"User_File_Stats.txt"}

# কখনোই স্পর্শ করা হবে না (সেফটি নেট — হোয়াইটলিস্টের পাশাপাশি ডাবল-লক)
NEVER_TOUCH_EXT = (
    ".session", ".session-journal", ".env", ".db", ".sqlite",
    ".yaml", ".yml", ".json", ".conf", ".ini", ".md", ".txt",
    ".py", ".sh", ".lock", ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mkv", ".avi", ".webm", ".mp3", ".m4a", ".pdf", ".zip", ".apk",
)
NEVER_TOUCH_NAMES = {
    "Dockerfile", "Procfile", "LICENSE", "README.md", "requirements.txt",
    "runtime.txt", "app.json", "koyeb.yaml", "render.yaml", ".env",
    "logging.conf", "selfheal.py", "bot.py", "info.py", "utils.py",
}
NEVER_TOUCH_DIRS = {
    ".git", "node_modules", ".cache",
    ".heroku",   # Koyeb-এর Python runtime + site-packages (লাইব্রেরি কখনো স্পর্শ নয়)
    ".venv", "venv",  # VPS-এর ভার্চুয়াল এনভায়রনমেন্ট (কখনো স্পর্শ নয়)
}


def clean_junk_files():
    """জাঙ্ক ফাইল মুছে ফ্রি হওয়া বাইট রিটার্ন করে। শুধু প্রজেক্ট ফোল্ডারের ভেতরে কাজ করে।"""
    freed = 0
    removed = 0

    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # .git ইত্যাদি ফোল্ডারে ঢুকবেই না
        dirnames[:] = [d for d in dirnames if d not in NEVER_TOUCH_DIRS]

        # পুরো __pycache__ ফোল্ডার মুছে দাও
        for d in list(dirnames):
            if d in JUNK_DIR_NAMES:
                full = os.path.join(dirpath, d)
                try:
                    size = sum(
                        os.path.getsize(os.path.join(r, f))
                        for r, _, fs in os.walk(full)
                        for f in fs
                        if not os.path.islink(os.path.join(r, f))
                    )
                    shutil.rmtree(full, ignore_errors=True)
                    freed += size
                    removed += 1
                    dirnames.remove(d)
                except Exception:
                    pass

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.islink(fpath):
                    continue
                base = fname.lower()
                _, ext = os.path.splitext(fname)
                ext = ext.lower()

                # স্পেশাল জাঙ্ক আগে চেক (এগুলো .txt হলেও জাঙ্ক — প্লাগিনের বাকি থাকা ফাইল)
                is_special = fname in SPECIAL_JUNK_FILES
                if is_special:
                    # এক ঘণ্টার পুরনো হলেই মুছবে
                    if time.time() - os.path.getmtime(fpath) < 3600:
                        continue
                else:
                    # ডাবল-লক: সোর্স কোড/ডাটা/কনফিগ কখনো না
                    if ext in NEVER_TOUCH_EXT:
                        continue
                    if fname in NEVER_TOUCH_NAMES:
                        continue

                is_junk = any(fnmatch.fnmatch(fname, pat) for pat in JUNK_FILE_PATTERNS)

                if not is_junk and not is_special:
                    continue

                # এই মুহূর্তে লেখা হচ্ছে এমন ফাইল স্পর্শ করবে না (১৫ মিনিট পুরনো হতে হবে)
                if time.time() - os.path.getmtime(fpath) < MIN_JUNK_AGE:
                    continue

                size = os.path.getsize(fpath)
                os.remove(fpath)
                freed += size
                removed += 1
            except Exception:
                continue

    return freed, removed


# ------------------------------------------------------------
# 🧽 RAM টিডি-আপ — পুরনো in-memory থাম্বনেইল ক্যাশ + garbage collect
# ------------------------------------------------------------
def purge_ram_caches():
    """
    web/stream_routes.py-এর thumb_cache-এ পুরনো এন্ট্রি মুছে দেয়।
    ⚠️ থাম্বনেইল কখনো হারাবে না — পরে কেউ চাইলে টেলিগ্রাম থেকে আবার
    এক ঝলকে নেমে আসে (ফিচার অক্ষত, শুধু র‍্যাম খালি হয়)।
    """
    purged = 0
    try:
        from web import stream_routes
        if hasattr(stream_routes, "purge_stale_thumbs"):
            purged = stream_routes.purge_stale_thumbs(
                max_age=SELFHEAL_CACHE_INTERVAL
            )
    except Exception:
        pass
    gc.collect()
    return purged


def run_clean_cycle():
    """একবার পূর্ণ ক্লিন সাইকেল — বট চালু থাকা অবস্থায়ই হয়।"""
    freed, files = clean_junk_files()
    purged = purge_ram_caches()
    pct, used_mb, total_mb = memory_snapshot()
    return freed, files, purged, pct, used_mb, total_mb


# ------------------------------------------------------------
# 🔁 রিলোড কাউন্টার (বুট-লুপ প্রোটেকশন)
# ------------------------------------------------------------
def _record_reload():
    try:
        now = time.time()
        stamps = _recent_reloads()
        stamps.append(now)
        stamps = [s for s in stamps if now - s < RELOAD_COOLDOWN_WINDOW]
        with open(RELOAD_LOG_FILE, "w") as f:
            f.write("\n".join(str(s) for s in stamps))
        return len(stamps)
    except Exception:
        return 0


def _recent_reloads():
    try:
        with open(RELOAD_LOG_FILE, "r") as f:
            now = time.time()
            return [float(x) for x in f.read().split() if x and now - float(x) < RELOAD_COOLDOWN_WINDOW]
    except Exception:
        return []


# ------------------------------------------------------------
# 📢 টেলিগ্রাম নোটিফায়ার (best-effort — ফেল করলেও রিলোড চলবেই)
# ------------------------------------------------------------
def make_telegram_notifier(client, chat_id, loop):
    """bot.py থেকে কল হয় — LOG_CHANNEL-এ সেলফ-হিল মেসেজ পাঠানোর ফাংশন বানায়।"""
    def notify(text):
        try:
            import asyncio

            async def _send():
                await client.send_message(chat_id=chat_id, text=text)

            fut = asyncio.run_coroutine_threadsafe(_send(), loop)
            fut.result(timeout=8)
        except Exception:
            pass
    return notify


_notify = None  # গ্লোবাল নোটিফায়ার


def _safe_notify(text):
    if _notify:
        try:
            _notify(text)
        except Exception:
            pass


# ------------------------------------------------------------
# 🚀 KOYEB AUTO-REDEPLOY — ড্যাশবোর্ডের Redeploy বাটনের অটোমেটিক সংস্করণ
# ------------------------------------------------------------
# RAM থ্রেশহোল্ড ছুঁলে Koyeb API (POST /v1/services/{id}/redeploy —
# Koyeb-এর অফিসিয়াল API) দিয়ে নিজে নিজে redeploy ট্রিগার হয়।
#
# সার্ভিস খোঁজার ৪ স্তর — যেকোনো একটাতে মিললেই চলবে:
#   ১) KOYEB_SERVICE_ID (সরাসরি id)
#   ২) নাম হুবহু মিল (KOYEB_SERVICE_NAME)
#   ৩) নাম প্রায়-মিল — কেস/বানান একটু ভুল থাকলেও ধরা পড়বে
#   ৪) বটের নিজের ডোমেইন (URL env) দিয়ে চিহ্নিত — সবচেয়ে নির্ভরযোগ্য,
#      নাম সম্পূর্ণ ভুল থাকলেও ডোমেইন দিয়ে সঠিক সার্ভিস বের করা হয়
# শেষ ভরসা: একাউন্টে একমাত্র সার্ভিস থাকলে সেটাই।
# ফেল হলে টেলিগ্রামে সাথে সাথে আসল HTTP error দেখানো হয় (401/403/404 ইত্যাদি)
# — কেন ফেল হলো সেটা অনুমান করতে হবে না।
KOYEB_API_BASE = "https://app.koyeb.com/v1"


def _koyeb_token():
    # কপি-পেস্টে স্পেস/কোটেশন ঢুকে গেলেও টোকেন বাঁচে
    return (KOYEB_API_TOKEN or "").strip().strip('"').strip("'")


def _norm_name(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _koyeb_find_service(sess):
    """রিটার্ন করে (service_dict, কীভাবে_পাওয়া_গেল) — না পেলে (None, কারণ)।"""
    # ১) সরাসরি service id দেওয়া থাকলে
    if KOYEB_SERVICE_ID:
        try:
            r = sess.get(f"{KOYEB_API_BASE}/services/{KOYEB_SERVICE_ID}", timeout=25)
            if r.status_code == 200 and (r.json() or {}).get("service"):
                return ((r.json())["service"], "KOYEB_SERVICE_ID")
        except Exception:
            pass
    # ২) সব সার্ভিস নামাও (পেজিনেশন সহ — বড় একাউন্টেও সব আসবে)
    services, offset = [], 0
    while True:
        r = sess.get(f"{KOYEB_API_BASE}/services",
                     params={"limit": 100, "offset": offset}, timeout=25)
        if r.status_code != 200:
            return (None, f"services list HTTP {r.status_code}: {r.text[:150]}")
        batch = (r.json() or {}).get("services") or []
        services.extend(batch)
        if len(batch) < 100 or offset > 400:
            break
        offset += 100
    if not services:
        return (None, "এই টোকেনের একাউন্টে কোনো সার্ভিস পাওয়া যায়নি")
    want = KOYEB_SERVICE_NAME or ""
    # ৩) হুবহু নাম → কেস-ইনসেনসিটিভ → বানান-নিরপেক্ষ (normalized) মিল
    for s in services:
        if want and (s.get("name") or "") == want:
            return (s, f"নাম হুবহু মিলেছে ('{s.get('name')}')")
    if want:
        for s in services:
            if (s.get("name") or "").lower() == want.lower():
                return (s, f"নাম কেস-ভিন্নতা সহ মিলেছে ('{s.get('name')}')")
    nw = _norm_name(want)
    if nw:
        for s in services:
            if _norm_name(s.get("name")) == nw:
                return (s, f"নাম প্রায়-মিল ('{s.get('name')}')")
    # ৪) বটের নিজের ডোমেইন দিয়ে চিহ্নিত
    host = ""
    try:
        from urllib.parse import urlparse
        host = urlparse(URL or "").netloc.split(":")[0].lower()
    except Exception:
        pass
    if host:
        try:
            r = sess.get(f"{KOYEB_API_BASE}/domains", params={"limit": 100}, timeout=25)
            if r.status_code == 200:
                for d in (r.json() or {}).get("domains") or []:
                    if (d.get("name") or "").lower().split(":")[0] == host:
                        app_id = d.get("app_id")
                        if app_id:
                            r2 = sess.get(f"{KOYEB_API_BASE}/services",
                                          params={"app_id": app_id, "limit": 100}, timeout=25)
                            svcs = (r2.json() or {}).get("services") or []
                            if svcs:
                                return (svcs[0], f"ডোমেইন '{host}' থেকে চিহ্নিত")
        except Exception:
            pass
    # ৫) একাউন্টে একটাই সার্ভিস থাকলে সেটাই
    if len(services) == 1:
        return (services[0], f"একমাত্র সার্ভিস ('{services[0].get('name')}')")
    names = [s.get("name") for s in services]
    return (None, f"'{want}' খুঁজে পাওয়া যায়নি। একাউন্টের সার্ভিসগুলো: {names}")


def koyeb_redeploy():
    """Koyeb API দিয়ে সার্ভিস redeploy ট্রিগার করে (ড্যাশবোর্ড বাটনের সমান)।
    রিটার্ন: (সফল?, বিস্তারিত-বার্তা) — বার্তাটা টেলিগ্রামে দেখানো হয়।"""
    token = _koyeb_token()
    if not token:
        return (False, "KOYEB_API_TOKEN সেট নেই")
    try:
        import requests
        sess = requests.Session()
        sess.headers.update({"Authorization": f"Bearer {token}"})

        svc, how = _koyeb_find_service(sess)
        if svc is None:
            return (False, how)
        sid = svc.get("id")
        logger.info(f"🚀 [REDEPLOY] সার্ভিস '{svc.get('name')}' ({how}) নিজে নিজে redeploy হচ্ছে...")

        # ট্রানজিয়েন্ট ফেল (নেটওয়ার্ক/5xx) হলে ১ বার আবার চেষ্টা
        last = "unknown error"
        for attempt in (1, 2):
            try:
                r = sess.post(f"{KOYEB_API_BASE}/services/{sid}/redeploy", json={}, timeout=30)
            except Exception as e:
                last = f"network error: {e}"
                time.sleep(3)
                continue
            if 200 <= r.status_code < 300:
                return (True, f"সার্ভিস '{svc.get('name')}' ({how}) — HTTP {r.status_code}")
            last = f"HTTP {r.status_code}: {(r.text or '')[:200]}"
            if r.status_code < 500:
                break  # 401/403/404 — আবার চেষ্টা করেও লাভ নেই
            time.sleep(3)
        hint = ""
        if last.startswith("HTTP 401"):
            hint = " → টোকেন ভুল বা মেয়াদ শেষ — Koyeb → Account Settings → API থেকে নতুন টোকেন নিয়ে KOYEB_API_TOKEN env আপডেট করো"
        elif last.startswith("HTTP 403"):
            hint = " → এই টোকেনে সার্ভিসে পারমিশন নেই — আবার নতুন টোকেন বানাও (একই অ্যাকাউন্টে)"
        elif last.startswith("HTTP 404"):
            hint = " → KOYEB_SERVICE_ID ভুল — ভুল হলে env থেকে মুছে দাও, অটো-ডিটেকশন নিজে খুঁজে নেবে"
        return (False, f"{last}{hint}")
    except Exception:
        return (False, f"exception: {traceback.format_exc(limit=2)[:200]}")


# ------------------------------------------------------------
# 🧠 ESCALATION STATE — RAM টার্গেট ছুঁলে: ৫ বার ক্লিন → ছয়বারে REDEPLOY
# ------------------------------------------------------------
# নিয়ম:
#   RAM টার্গেট (যেমন ৯০%) ছুঁলে = ১টি "ক্রসিং"।
#   ১ম-৫ম ক্রসিং → পূর্ণ মেমোরি ক্লিন + রিসেট → লগ চ্যানেলে রিপোর্ট।
#   ৬ষ্ঠ ক্রসিং → ক্লিনে ধরেনি — তখনই নিজে নিজে Koyeb REDEPLOY।
#   RAM টানা SELFHEAL_CLEAN_RESET_SECONDS স্বাভাবিক থাকলে গণনা রিসেট।
#   RAM টানা উপরে থাকলে প্রতি SELFHEAL_REARM_SECONDS পর পর পরের প্রচেষ্টা —
#   ফলে টানা চাপে ~১০ মিনিটে ৫ ক্লিন শেষে redeploy হয়ে যায়।

_esc = {
    "consecutive": 0,       # টানা কত চেকে RAM টার্গেটের উপরে
    "clean_count": 0,       # এই চক্রে কতবার ক্লিন হয়েছে (০-৫)
    "last_action_ts": 0.0,  # শেষ ক্লিন/রিডেপ্লয় কবে হয়েছে
    "below_ts": None,       # RAM কবে থেকে টানা স্বাভাবিক
}


def _escalation_decide(pct, now, st=None):
    """সিদ্ধান্ত-ফাংশন (টেস্টযোগ্য)। রিটার্ন: 'none' | 'clean' | 'redeploy'।"""
    if st is None:
        st = _esc
    if pct >= SELFHEAL_MEM_LIMIT:
        st["below_ts"] = None
        st["consecutive"] += 1
        if st["consecutive"] < RELOAD_CONFIRM:
            return "none"   # false-spike এড়াতে ২ চেক (১০ সেকেন্ড) কনফার্ম
        if now - st["last_action_ts"] < SELFHEAL_REARM_SECONDS:
            return "none"   # শেষ প্রচেষ্টার ফল দেখার সময় দাও
        if st["clean_count"] < SELFHEAL_MAX_CLEANS:
            return "clean"
        return "redeploy"
    # RAM স্বাভাবিক — গণনা-রিসেট ট্র্যাকিং
    st["consecutive"] = 0
    if st["clean_count"] > 0:
        if st["below_ts"] is None:
            st["below_ts"] = now
        elif now - st["below_ts"] >= SELFHEAL_CLEAN_RESET_SECONDS:
            st["clean_count"] = 0
            st["below_ts"] = None
            logger.info(
                f"🛡️ [SELF-HEAL] RAM টানা {SELFHEAL_CLEAN_RESET_SECONDS // 60} মিনিট স্বাভাবিক — "
                f"ক্লিন-গণনা আবার ০/{SELFHEAL_MAX_CLEANS} থেকে শুরু"
            )
    return "none"


def _trigger_clean(pct_before, used_mb, total_mb, attempt):
    """১টি মেমোরি-ক্লিন প্রচেষ্টা: পূর্ণ ক্লিন সাইকেল + লগ চ্যানেলে রিপোর্ট।"""
    freed, files, purged, pct_after, u2, t2 = run_clean_cycle()
    mb_freed = freed / (1024 * 1024)
    logger.warning(
        f"🛡️ [SELF-HEAL] মেমোরি ক্লিন+রিসেট প্রচেষ্টা {attempt}/{SELFHEAL_MAX_CLEANS}: "
        f"RAM {pct_before:.1f}% → {pct_after:.1f}% | জাঙ্ক {files}টি ({mb_freed:.1f} MB) | "
        f"থাম্ব-ক্যাশ {purged}টি | বট চালুই আছে"
    )
    _safe_notify(
        f"🧠 <b>Memory Clean + Reset</b> — প্রচেষ্টা {attempt}/{SELFHEAL_MAX_CLEANS}\n\n"
        f"⚠️ RAM <b>{pct_before:.1f}%</b> হয়ে গেছে ({used_mb:.0f}MB/{total_mb:.0f}MB) "
        f"— টার্গেট {SELFHEAL_MEM_LIMIT}%\n"
        f"🧹 মেমোরি ক্লিন হচ্ছে: জাঙ্ক ফাইল <b>{files}টি</b> ({mb_freed:.1f} MB খালি), "
        f"পুরনো থাম্ব-ক্যাশ <b>{purged}টি</b>\n"
        f"✅ ক্লিন শেষে RAM এখন: <b>{pct_after:.1f}%</b> — বট চালুই আছে, কিছুই বন্ধ হয়নি।\n\n"
        f"📊 {SELFHEAL_MAX_CLEANS} বার ক্লিনের পরও RAM ফুল থাকলে ৬ষ্ঠ বার সার্ভার "
        f"নিজে নিজে <b>Redeploy</b> নেবে (Koyeb — ড্যাশবোর্ড বাটনের মতো)।"
    )
    return pct_after


# ------------------------------------------------------------
# 🔄 মেমোরি-ট্রিগার: ৫ বার ক্লিনেও RAM ফুল → পূর্ণ AUTO-REDEPLOY
# ------------------------------------------------------------
def _trigger_redeploy(reason):
    pct, used_mb, total_mb = memory_snapshot()
    _record_reload()

    logger.warning(
        f"🛡️ [SELF-HEAL] RAM {pct:.1f}% ({used_mb:.0f}MB/{total_mb:.0f}MB) >= "
        f"{SELFHEAL_MEM_LIMIT}% — কারণ: {reason} | AUTO-REDEPLOY শুরু..."
    )

    if KOYEB_API_TOKEN:
        # বিল্ড চলার সময় পুরনো instance-এ RAM হেডরুম বাড়াতে এখনই ক্যাশ খালি
        try:
            purge_ram_caches()
        except Exception:
            pass

        ok, detail = koyeb_redeploy()
        if ok:
            logger.warning(
                "🚀 [SELF-HEAL] AUTO-REDEPLOY ট্রিগার হয়েছে ✅ — Koyeb নতুন instance "
                "বিল্ড করে healthy হলে অটোমেটিক swap করবে (মেমোরি জিরো থেকে শুরু)"
            )
            _safe_notify(
                f"🚀 <b>AUTO-REDEPLOY Started</b>\n\n"
                f"🧠 RAM: <b>{pct:.1f}%</b> ({used_mb:.0f}MB / {total_mb:.0f}MB)\n\n"
                f"♻️ {SELFHEAL_MAX_CLEANS} বার মেমোরি ক্লিনের পরও RAM ফুল — সার্ভার <b>নিজে নিজে Redeploy</b> নিয়েছে "
                f"(ড্যাশবোর্ডের Redeploy বাটন চাপার মতোই, হাত দিতে হবে না)।\n"
                f"✅ {detail}\n"
                f"🏗️ Koyeb নতুন instance বিল্ড করে অটোমেটিক swap করবে — চলমান স্ট্রিমে আঘাত লাগবে না।\n"
                f"✅ ফাইল/থাম্বনেইল/ডাটা সব অক্ষত থাকবে।"
            )
            # ডিপ্লয় ইন-ফ্লাইট আছে — এর মধ্যে আবার ট্রিগার এড়াতে চুপ করে অপেক্ষা
            time.sleep(RELOAD_COOLDOWN_SLEEP)
            return

        logger.error(f"🚀 [SELF-HEAL] Koyeb API redeploy ফেল — fallback: fresh restart নিচ্ছি | {detail}")
        _safe_notify(
            f"⚠️ <b>Auto-Redeploy API Fail → Fresh Restart</b>\n"
            f"🧠 RAM: {pct:.1f}%\n\n"
            f"❌ <b>Koyeb API বলছে:</b> <code>{detail}</code>\n\n"
            f"উপরের কারণ দেখে env ঠিক করো। তবুও সার্ভার বাঁচাতে এখন fresh restart নিচ্ছি — "
            f"মেমোরি জিরো থেকে শুরু হবে, সব ডাটা অক্ষত।"
        )
    else:
        logger.warning(
            "🛡️ [SELF-HEAL] KOYEB_API_TOKEN নেই — পূর্ণ Redeploy সম্ভব না, "
            "fallback: fresh restart নিচ্ছি (মেমোরি জিরো হবে)"
        )
        _safe_notify(
            f"🛡️ <b>Auto Fresh Restart</b>\n"
            f"🧠 RAM: <b>{pct:.1f}%</b> ({used_mb:.0f}MB/{total_mb:.0f}MB) — {SELFHEAL_MEM_LIMIT}% ছুঁয়েছে।\n\n"
            f"ℹ️ পূর্ণ <b>Redeploy</b> চাইলে Koyeb-এ <code>KOYEB_API_TOKEN</code> env যোগ করো "
            f"(SELFHEAL_GUIDE_BANGLA.md দেখো)। এখন fresh restart নিচ্ছি — "
            f"মেমোরি জিরো থেকে শুরু হবে।"
        )

    # Fallback: প্রসেস নিজেকে fresh করে নেয় — /restart কমান্ডের প্রমাণিত পদ্ধতি
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
    except Exception:
        pass
    time.sleep(1.5)
    os.execl(sys.executable, sys.executable, *sys.argv)


# ------------------------------------------------------------
# 🧠 WATCHDOG THREAD — প্রতি ৫ সেকেন্ডে RAM চেক (CPU চেক করেই না!)
# ------------------------------------------------------------
def _watchdog_loop():
    grace_logged = False
    hb_last = 0.0
    logger.info(
        f"🛡️ [SELF-HEAL] Memory Watchdog চালু — RAM টার্গেট: {SELFHEAL_MEM_LIMIT}% "
        f"| চক্র: {SELFHEAL_MAX_CLEANS} বার মেমোরি ক্লিন → ৬ষ্ঠ ক্রসিংয়ে REDEPLOY "
        f"(CPU কখনো ট্রিগার নয়) | চেক প্রতি {SELFHEAL_CHECK_INTERVAL} সেকেন্ডে"
    )
    while True:
        try:
            time.sleep(SELFHEAL_CHECK_INTERVAL)
            pct, used_mb, total_mb = memory_snapshot()
            now = time.time()
            uptime = now - _START_TIME

            if uptime < BOOT_GRACE:
                if not grace_logged:
                    logger.info("🛡️ [SELF-HEAL] বুট গ্রেস পিরিয়ড (৯০ সেকেন্ড) — এর মধ্যে redeploy হবে না")
                    grace_logged = True
                continue

            # হার্টবিট: প্রতি ৫ মিনিটে লাইভ RAM রিডিং লগে — ড্যাশবোর্ডের সাথে মিল যাচাই
            if now - hb_last >= 300:
                hb_last = now
                logger.info(
                    f"🛡️ [SELF-HEAL] RAM হার্টবিট: {pct:.1f}% ({used_mb:.0f}MB/{total_mb:.0f}MB) "
                    f"| টার্গেট {SELFHEAL_MEM_LIMIT}% | ক্লিন গণনা {_esc['clean_count']}/{SELFHEAL_MAX_CLEANS}"
                )

            action = _escalation_decide(pct, now)

            if action == "clean":
                _esc["clean_count"] += 1
                _esc["last_action_ts"] = now
                _esc["consecutive"] = 0
                _trigger_clean(pct, used_mb, total_mb, _esc["clean_count"])
            elif action == "redeploy":
                logger.warning(
                    f"🛡️ [SELF-HEAL] RAM {pct:.1f}% ({used_mb:.0f}MB/{total_mb:.0f}MB) — "
                    f"{_esc['clean_count']}/{SELFHEAL_MAX_CLEANS} ক্লিন শেষ, এবার ৬ষ্ঠ ক্রসিং — REDEPLOY শুরু..."
                )
                # বুট-লুপ প্রোটেকশন: ৩০ মিনিটে ৩+ বার ট্রিগার হলে কুলডাউন
                recent = _recent_reloads()
                if len(recent) >= RELOAD_COOLDOWN_MAX:
                    logger.warning(
                        f"🛡️ [SELF-HEAL] {RELOAD_COOLDOWN_WINDOW // 60} মিনিটে {len(recent)} বার ট্রিগার — "
                        f"সার্ভার বাঁচাতে {RELOAD_COOLDOWN_SLEEP // 60} মিনিট কুলডাউন (বট চালুই থাকছে)"
                    )
                    _safe_notify(
                        f"⚠️ <b>Self-Heal Cooldown</b>\nমেমোরি বারবার {SELFHEAL_MEM_LIMIT}%+ হচ্ছে "
                        f"({len(recent)} বার)। সার্ভার চালু রাখতে {RELOAD_COOLDOWN_SLEEP // 60} মিনিট পর পর redeploy/restart হবে।"
                    )
                    time.sleep(RELOAD_COOLDOWN_SLEEP)
                    # কুলডাউন শেষে RAM স্বাভাবিক এসে গেলে ট্রিগারের দরকারই নেই
                    pct2, _, _ = memory_snapshot()
                    if pct2 < SELFHEAL_MEM_LIMIT:
                        logger.info(f"🛡️ [SELF-HEAL] কুলডাউন শেষে RAM স্বাভাবিক: {pct2:.1f}% — redeploy বাতিল")
                        _esc["consecutive"] = 0
                        continue
                _esc["clean_count"] = 0
                _esc["last_action_ts"] = now
                _esc["consecutive"] = 0
                _trigger_redeploy(
                    f"{SELFHEAL_MAX_CLEANS} বার মেমোরি ক্লিনের পরও RAM {pct:.1f}% >= {SELFHEAL_MEM_LIMIT}%"
                )
                # redeploy সফল হলে এখানে ফিরে আসে (old instance তখনও চালু);
                # fallback exec হলে ফেরে না। দুই ক্ষেত্রেই নতুন চক্র শুরু।
        except Exception:
            logger.error(f"🛡️ [SELF-HEAL] watchdog error: {traceback.format_exc()}")
            time.sleep(SELFHEAL_CHECK_INTERVAL)


# ------------------------------------------------------------
# 🧹 CLEANER THREAD — সাইলেন্ট রুটিন ক্লিন (ডিফল্ট প্রতি ১৫ মিনিটে, বট বন্ধ হয় না)
# ------------------------------------------------------------
def _cleaner_loop():
    time.sleep(60)  # বুটের এক মিনিট পর প্রথম ক্লিন
    while True:
        try:
            freed, files, purged, pct, used_mb, total_mb = run_clean_cycle()
            mb_freed = freed / (1024 * 1024)
            logger.info(
                f"🧹 [AUTO-CLEAN] জাঙ্ক ফাইল: {files}টি ({mb_freed:.1f} MB ফ্রি) | "
                f"পুরনো RAM থাম্ব-ক্যাশ: {purged}টি | RAM: {pct:.1f}% | বট চালুই আছে ✅"
            )
            # 🤫 রুটিন ক্লিন সাইলেন্ট — লগ চ্যানেলে কিছু যায় না (শুধু কনসোল লগ)।
            # লগ চ্যানেলে যায় শুধু RAM-টার্গেট ক্লিন আর redeploy-এর মেসেজ।
        except Exception:
            logger.error(f"🧹 [AUTO-CLEAN] error: {traceback.format_exc()}")
        time.sleep(max(SELFHEAL_CACHE_INTERVAL, 300))


# ------------------------------------------------------------
# 🧹 LOG-CHANNEL AUTO-CLEAN — প্রতি ৫ মিনিটে লগ চ্যানেল পরিষ্কার থাকবে
# ------------------------------------------------------------
# কী করে: লগ চ্যানেলে আসা স্ট্যাটাস/নোটিফিকেশন মেসেজ (RAM রিপোর্ট,
# redeploy নোটিশ, রিস্টার্ট নোটিশ, ইউজার-জয়েন, অটো-ক্লিন রিপোর্ট ইত্যাদি)
# প্রতি ৫ মিনিটে নিজে নিজে মুছে দেয় — চ্যানেল সবসময় পরিষ্কার দেখায়।
#
# ⚠️ সেফটি নিয়ম (সবচেয়ে গুরুত্বপূর্ণ):
#   স্ট্রিম লিঙ্কগুলো BIN_CHANNEL-এর ফাইল-মেসেজের উপর দাঁড়িয়ে আছে
#   (web/stream_routes.py: get_messages(BIN_CHANNEL, id)) — ওই মেসেজ
#   মুছলে আগে দেওয়া লিঙ্ক মরে যায়! তাই:
#   - BIN_CHANNEL আর LOG_CHANNEL **একই চ্যানেল হলে** → শুধু টেক্সট/
#     স্ট্যাটাস মেসেজ মুছবে, ভিডিও/ফাইল মেসেজ স্পর্শ করবে না (লিঙ্ক বাঁচবে)
#   - BIN_CHANNEL **আলাদা** হলে → লগ চ্যানেল ১০০% খালি করবে (ফাইলসহ)
# পিন করা মেসেজ ডিফল্টে বাঁচে (SELFHEAL_LOG_CLEAN_KEEP_PINNED=False
# দিলে সেটাও মুছবে)।

_lc_client = None
_lc_loop = None
_lc_chat = 0
_lc_flags = {"intro": False, "perm": False}


def _is_media_message(m):
    """ফাইল/ভিডিও মেসেজ কি না — এগুলো স্ট্রিম লিঙ্কের উৎস, স্পর্শ-বহির্ভূত।"""
    return any(getattr(m, a, None) for a in (
        "video", "document", "audio", "photo", "animation",
        "voice", "video_note", "sticker",
    ))


def _purge_log_channel():
    """লগ চ্যানেলের মেসেজ মুছে দেয়। রিটার্ন: (মুছেছে, স্ক্যান করেছে)।"""
    import asyncio

    if not _lc_client or not _lc_loop or not _lc_chat:
        return (0, 0)

    try:
        same_channel = int(BIN_CHANNEL or 0) == int(_lc_chat)
    except Exception:
        same_channel = False
    # BIN_CHANNEL অজানা হলেও সেফটির খাতিরে মিডিয়া রক্ষা করো
    protect_media = same_channel or not (BIN_CHANNEL or 0)

    async def _wipe():
        from pyrogram.errors import FloodWait

        deleted, scanned = 0, 0
        keep = set()
        if SELFHEAL_LOG_CLEAN_KEEP_PINNED:
            try:
                chat = await _lc_client.get_chat(_lc_chat)
                pm = getattr(chat, "pinned_message", None)
                if pm is not None:
                    keep.add(pm.id)
            except Exception:
                pass

        ids = []
        async for m in _lc_client.get_chat_history(_lc_chat, limit=800):
            scanned += 1
            if m.id in keep:
                continue
            if protect_media and _is_media_message(m):
                continue
            ids.append(m.id)
            if len(ids) >= 100:
                try:
                    await _lc_client.delete_messages(_lc_chat, ids)
                    deleted += len(ids)
                except FloodWait as e:
                    await asyncio.sleep(min(e.value, 60) + 1)
                    try:
                        await _lc_client.delete_messages(_lc_chat, ids)
                        deleted += len(ids)
                    except Exception:
                        break
                ids = []
        if ids:
            try:
                await _lc_client.delete_messages(_lc_chat, ids)
                deleted += len(ids)
            except Exception:
                pass
        return deleted, scanned

    fut = asyncio.run_coroutine_threadsafe(_wipe(), _lc_loop)
    return fut.result(timeout=900)


def _log_cleaner_loop():
    time.sleep(90)  # বুট গ্রেস শেষ হলে প্রথম পরিষ্কার শুরু
    while True:
        try:
            try:
                same = int(BIN_CHANNEL or 0) == int(_lc_chat)
            except Exception:
                same = False
            protect = same or not (BIN_CHANNEL or 0)
            deleted, scanned = _purge_log_channel()
            logger.info(
                f"🧹 [LOG-CLEAN] লগ চ্যানেল পরিষ্কার: {deleted} মেসেজ মুছেছে "
                f"(স্ক্যান {scanned}) | প্রতি {max(SELFHEAL_LOG_CLEAN_INTERVAL // 60, 1)} মিনিটে | "
                f"{'ফাইল রক্ষা মোড (BIN=LOG)' if protect else 'সম্পূর্ণ পরিষ্কার'} | বট চালুই আছে ✅"
            )
            if not _lc_flags["intro"]:
                _lc_flags["intro"] = True
                if protect:
                    _safe_notify(
                        "🧹 <b>Log Auto-Clean চালু</b>\n\n"
                        f"এখন থেকে প্রতি <b>{max(SELFHEAL_LOG_CLEAN_INTERVAL // 60, 1)} মিনিটে</b> লগ চ্যানেলের "
                        "স্ট্যাটাস/টেক্সট মেসেজ নিজে নিজে মুছে যাবে।\n\n"
                        "⚠️ <b>কিন্তু তোমার BIN_CHANNEL আর LOG_CHANNEL একই চ্যানেল!</b>\n"
                        "পুরনো স্ট্রিম লিঙ্ক বাঁচাতে <b>ভিডিও/ফাইল মেসেজ এখনো মুছছি না</b> — "
                        "ওগুলোই লিঙ্কের উৎস, মুছলে সব লিঙ্ক মরে যাবে।\n\n"
                        "✅ <b>ফাইলও অটো-ডিলিট করাতে চাইলে (২ মিনিটের কাজ):</b>\n"
                        "১. Telegram-এ একটা <b>নতুন প্রাইভেট চ্যানেল</b> বানাও (যেমন: Movie Box Files)\n"
                        "২. বটকে ওই চ্যানেলে <b>Admin</b> বানাও (সব পারমিশন)\n"
                        "৩. চ্যানেলের ID নাও — চ্যানেলে একটা মেসেজ ফরওয়ার্ড করো @userinfobot-এ, "
                        "বা @RawDataBot চ্যানেলে ঢুকিয়ে ID দেখো (-100... দিয়ে শুরু)\n"
                        "৪. Koyeb → Settings → Environment Variables → <code>BIN_CHANNEL</code> = "
                        "ওই নতুন ID বসাও → রিডিপ্লয় হবে\n"
                        "৫. তারপর থেকে লগ চ্যানেল <b>১০০% খালি</b> থাকবে — ফাইল যাবে নতুন ফাইল-চ্যানেলে, "
                        "লিঙ্ক কখনো ভাঙবে না।"
                    )
                else:
                    # ফুল মোডে কোনো ইন্ট্রো মেসেজ নেই — লগ চ্যানেল গোঙিয়ে রাখাই না
                    logger.info(
                        f"🧹 [LOG-CLEAN] ফুল মোড (BIN আলাদা) — প্রতি "
                        f"{max(SELFHEAL_LOG_CLEAN_INTERVAL // 60, 1)} মিনিটে লগ চ্যানেল সম্পূর্ণ পরিষ্কার হবে"
                    )
        except Exception:
            logger.error(f"🧹 [LOG-CLEAN] error: {traceback.format_exc()}")
            if not _lc_flags["perm"]:
                _lc_flags["perm"] = True
                _safe_notify(
                    "⚠️ <b>Log Auto-Clean কাজ করছে না</b>\n\n"
                    "বটের লগ চ্যানেলে মেসেজ ডিলিট করার পারমিশন নেই।\n"
                    "সমাধান: চ্যানেলে বটকে <b>Admin</b> বানাও → <b>Delete Messages</b> "
                    "পারমিশন চালু করো।"
                )
        time.sleep(max(SELFHEAL_LOG_CLEAN_INTERVAL, 60))


# ------------------------------------------------------------
# 🚀 START — bot.py থেকে ডাকা হয়
# ------------------------------------------------------------
def start(notify=None, client=None, loop=None, log_chat=0):
    """সেলফ-হিল সিস্টেম চালু করে (ব্যাকগ্রাউন্ড থ্রেড)।"""
    global _started, _notify, _lc_client, _lc_loop, _lc_chat
    if not SELFHEAL_ENABLED:
        logger.info("🛡️ [SELF-HEAL] বন্ধ আছে (SELFHEAL_ENABLED=False)")
        return
    if _started:
        return
    _started = True
    _notify = notify
    _lc_client = client
    _lc_loop = loop
    try:
        _lc_chat = int(log_chat or 0)
    except Exception:
        _lc_chat = 0

    pct, used_mb, total_mb = memory_snapshot()
    _src = ("env SELFHEAL_MEM_LIMIT" if os.environ.get("SELFHEAL_MEM_LIMIT")
            else "info.py ডিফল্ট")
    logger.info(
        f"🛡️ [SELF-HEAL] সিস্টেম চালু ✅ | RAM: {pct:.1f}% ({used_mb:.0f}MB/{total_mb:.0f}MB) | "
        f"RAM টার্গেট: {SELFHEAL_MEM_LIMIT}% (উৎস: {_src}) | "
        f"সাইলেন্ট অটো-ক্লিন: প্রতি {SELFHEAL_CACHE_INTERVAL // 60} মিনিটে"
    )
    logger.info(
        f"🧠 [ESCALATION] RAM {SELFHEAL_MEM_LIMIT}% ছুঁলে: সর্বোচ্চ {SELFHEAL_MAX_CLEANS} বার "
        f"মেমোরি ক্লিন+রিসেট (প্রতি {SELFHEAL_REARM_SECONDS}s পর পর প্রচেষ্টা) → "
        f"তারপরও ফুল হলে নিজে নিজে REDEPLOY"
    )
    logger.info(
        f"🚀 [REDEPLOY] Koyeb API মোড: {'টোকেন আছে ✅' if _koyeb_token() else 'টোকেন নেই (fallback restart চলবে)'}"
    )

    threading.Thread(target=_watchdog_loop, name="selfheal-watchdog", daemon=True).start()
    threading.Thread(target=_cleaner_loop, name="selfheal-cleaner", daemon=True).start()
    if _lc_client and _lc_loop and _lc_chat:
        if SELFHEAL_LOG_CLEAN_ENABLED:
            threading.Thread(target=_log_cleaner_loop, name="selfheal-logclean", daemon=True).start()
            logger.info(
                f"🧹 [SELF-HEAL] লগ চ্যানেল অটো-ক্লিন চালু — প্রতি {max(SELFHEAL_LOG_CLEAN_INTERVAL // 60, 1)} মিনিটে "
                f"(BIN=LOG হলে ফাইল-মেসেজ স্বয়ংক্রিয়ভাবে রক্ষা পাবে)"
            )
    else:
        logger.info("🧹 [SELF-HEAL] লগ অটো-ক্লিন বন্ধ — bot.py থেকে client/loop/log_chat পাওয়া যায়নি")
