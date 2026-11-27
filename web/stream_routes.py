import os
import time
import asyncio
from collections import deque
from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from aiohttp.http_exceptions import BadStatusLine
from pyrogram.errors import FloodWait
from web.bot import multi_clients, work_loads, WebavBot
from web.server.exceptions import FileNotFound, InvalidHash
from web.utils.custom_dl import ByteStreamer
from web.utils.file_properties import get_media_from_message
from web.utils.render_template import render_page
from info import *
from utils import temp, get_readable_time
from web.utils import StartTime, __version__
import aiofiles
import jinja2
from database.users_db import db

routes = web.RouteTableDef()

# ⚡ প্রিমিয়াম লিমিট: এর চেয়ে বড় ফাইল প্রিমিয়াম ক্লায়েন্টে যাবে
PREMIUM_SIZE_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB

# ---------------------------------------------------------------
# ⚡⚡ স্ট্রিম হেলথ সিস্টেম (জিরো-হ্যাং গ্যারান্টি)
# ---------------------------------------------------------------
# সমস্যা যা ধরা পড়েছিল (লাইভ টেস্টে): pyrogram-এর Session.start() ভেতরে
# `while True` লুপে সংযোগ ব্যর্থ হলে চিরকাল আটকে থাকে এবং মাঝে মাঝে
# cancellation-ও মানে না। ফলে স্ট্রিম 0 বাইট নিয়ে অনন্তকাল ঝুলে থাকত এবং
# work_loads কাউন্টার বেড়েই যেত — ২-৩ জন ইউজারের পর সবাই আটকে যেত।
#
# সমাধান: pyrogram-কে cancel করার চেষ্টা না করে প্রতিটি chunk-কে টাইমারের
# সাথে RACE করানো হয়। টাইমার জিতলে স্ট্রিম ছেড়ে দেওয়া হয় (504), player
# নিজে থেকেই আবার চেষ্টা করে, আর দোষী ক্লায়েন্ট কিছুক্ষণের জন্য বাদ পড়ে।
def _env_int(name, default):
    """⚙️ .env / Koyeb dashboard থেকে কোড না ছুঁয়েই সব লিমিট টিউন করার দরজা"""
    try:
        return max(1, int(str(os.getenv(name, str(default))).strip()))
    except (TypeError, ValueError):
        return default

# ⚡ সব লিমিট এখন এনভায়রনমেন্ট ভেরিয়েবল দিয়ে বদলানো যায় (ডিফল্ট = ফ্রি
# ইনস্ট্যান্সের জন্য প্রমাণিত নিরাপদ মান; বড় ইনস্ট্যান্সে এনভায় বাড়িয়ে নেওয়া যাবে)
STREAM_FIRST_CHUNK_TIMEOUT = _env_int("STREAM_FIRST_CHUNK_TIMEOUT", 12)  # প্রথম বাইট এত সেকেন্ডে আসতেই হবে
STREAM_IDLE_TIMEOUT = _env_int("STREAM_IDLE_TIMEOUT", 25)          # মাঝপথে দুই chunk-এর মাঝে সর্বোচ্চ চুপ
MAX_FLOODWAIT = _env_int("MAX_FLOODWAIT", 30)                # এর চেয়ে বড় FloodWait হলে অপেক্ষা নয়, ছেড়ে দাও
CLIENT_COOLDOWN = _env_int("CLIENT_COOLDOWN", 60)             # বারবার আটকানো ক্লায়েন্ট এত সেকেন্ড বাদ থাকবে
FAILS_TO_COOLDOWN = _env_int("FAILS_TO_COOLDOWN", 4)             # টানা এতগুলো ব্যর্থতায় বেঞ্চ হবে
FIRST_CHUNK_RETRIES = _env_int("FIRST_CHUNK_RETRIES", 1)         # প্রথম chunk ফেল করলে অন্য কত ক্লায়েন্টে চুপচাপ রিট্রাই
FAIL_DEBOUNCE_SECS = 2.0                                         # একই ঝাপটার ব্যর্থতা এই সময়ের ভেতরে একবারই গোনা হবে


class UpstreamTimeout(Exception):
    """Telegram থেকে অনেকক্ষণ কোনো data আসেনি — স্ট্রিম বাতিল"""


client_health: dict = {}   # client index -> {"fails": int, "cooldown_until": float}
prewarmed: set = set()     # যাদের media session আগেই তৈরি হয়ে গেছে
prewarm_busy: set = set()
prewarm_tries: dict = {}


def _cooling(index, now=None):
    h = client_health.get(index)
    return bool(h and (now or time.time()) < h["cooldown_until"])


_last_fail_ts: dict = {}   # client index -> শেষ গণনা-করা ব্যর্থতার সময়


def mark_client_failure(index):
    # ⚡ ডিবাউন্স: সিপিইউ চাপা পড়লে এক ক্লায়েন্টের ৫-৬টা স্ট্রিম একই সেকেন্ডে
    # একসাথে মরে — আগে প্রতিটাই আলাদা ব্যর্থতা গোনা হতো, ফলে ২ সেকেন্ডেই
    # সব ক্লায়েন্ট একসাথে বেঞ্চ হয়ে পুরো লোড প্রিমিয়ামের উপর চাপা পড়ত
    # (লাইভ লগের ১৪:২৩–১৪:২৬ ঘটনা)। এখন একই ঝাপটার ব্যর্থতা ১টাই গোনা হবে।
    now = time.time()
    if now - _last_fail_ts.get(index, 0) < FAIL_DEBOUNCE_SECS:
        return
    _last_fail_ts[index] = now
    h = client_health.setdefault(index, {"fails": 0, "cooldown_until": 0.0})
    h["fails"] += 1
    if h["fails"] >= FAILS_TO_COOLDOWN:
        h["cooldown_until"] = now + CLIENT_COOLDOWN
        h["fails"] = 0
        bench_events.append(now)   # ⚡ ওভারলোড গার্ডের জন্য রেকর্ড
        logging.warning(f"Client {index} benched for {CLIENT_COOLDOWN}s after repeated stalls")


def mark_client_success(index):
    # সফল চাঙ্ক ফেইল-কাউন্ট রিসেট করবে, কিন্তু চলমান ১২০ সেকেন্ডের
    # বেঞ্চ মুছবে না — নইলে আটকানো ক্লায়েন্ট ১ সেকেন্ডেই বেঞ্চ থেকে
    # বেরিয়ে আবার চাপ নিয়ে আবার আটকায় (লগের ১৯:৪৩:৫৫ → ১৯:৪৪:০৪ ঘটনা)
    h = client_health.get(index)
    if h and h.get("cooldown_until", 0) > time.time():
        h["fails"] = 0
    else:
        client_health[index] = {"fails": 0, "cooldown_until": 0.0}


# ---------------------------------------------------------------
# ⚡⚡ GATE SYSTEM — এক ক্লায়েন্টে অনিয়ন্ত্রিত সমান্তরাল স্ট্রিম ঢুকলে
# pyrogram-এর একটাই TCP connection ভেঙে পড়ে ('Connection lost' →
# session restart race → 504 ঝড় → player আরও রিট্রাই → মৃত্যু-চক্র)।
# তাই প্রতি ক্লায়েন্টের সামনে গেট — সর্বোচ্চ ৮টি লাইভ স্ট্রিম।
# ---------------------------------------------------------------
client_gates: dict = {}   # client index -> BoundedSemaphore
STREAM_GATE_LIMIT = _env_int("STREAM_GATE_LIMIT", 6)     # সাধারণ বট ক্লায়েন্টে একসাথে সর্বোচ্চ লাইভ স্ট্রিম
PREMIUM_GATE_LIMIT = _env_int("PREMIUM_GATE_LIMIT", 5)    # প্রিমিয়ামে কম — লাইভ টেস্টে ৮টিতেও সংযোগ ভেঙেছিল
STREAM_GATE_WAIT = _env_int("STREAM_GATE_WAIT", 12)      # গেট ফাঁকা হওয়ার অপেক্ষা (সেকেন্ড), এর পরে 504
OVERFLOW_GATE_WAIT = _env_int("OVERFLOW_GATE_WAIT", 1)    # ওভারফ্লোতে প্রতি অন্য ক্লায়েন্টে এত সেকেন্ড অপেক্ষা

# ⚡⚡ সার্বিক ভর্তি-নিয়ন্ত্রণ (সিপিইউ মৃত্যু-চক্রের স্থায়ী সমাধান):
# ফ্রি ইনস্ট্যান্স (০.১ vCPU) একসাথে ~২০-২৫টা স্ট্রিমের MTProto এনক্রিপশন
# টানতে পারে — তার বেশি ঢুকলেই সব ক্লায়েন্ট একসাথে টাইমআউট হয়ে
# মৃত্যু-চক্র শুরু হয় (লাইভ লগ ১৪:২৩–১৪:২৭)। এখন মোট লাইভ স্ট্রিম এই
# সংখ্যায় সীমিত থাকবে; বাড়তি রিকোয়েস্ট ৩-৬ সেকেন্ডের Retry-After পাবে —
# player নিজেই রিট্রাই করবে, ইউজার এক সেকেন্ডের লোডিং ছাড়া কিছু টের পাবে না।
TOTAL_STREAM_CAPACITY = _env_int("TOTAL_STREAM_CAPACITY", 24)  # 0 দিলে সীমা বন্ধ
GLOBAL_GATE_WAIT = _env_int("GLOBAL_GATE_WAIT", 4)             # নতুন স্ট্রিম: ক্যাপাসিটি ফাঁকা হওয়ার সর্বোচ্চ অপেক্ষা (সেকেন্ড)
global_gate = asyncio.BoundedSemaphore(TOTAL_STREAM_CAPACITY) if TOTAL_STREAM_CAPACITY > 0 else None

# ⚡⚡ চালিয়ে-যাওয়া (continuation) রিকোয়েস্টের আলাদা অপেক্ষার বাজেট।
# প্লেয়ারের বাফারে সাধারণত ১০-৩০ সেকেন্ডের ভিডিও জমা থাকে — তাই ভিড়ের
# সময় চলমান ভিডিওর পরের অংশ ৯ সেকেন্ড পর্যন্ত লাইনে দাঁড়িয়ে থাকলেও
# ইউজার একদম কিছু টের পায় না (ভিডিও বাফার থেকে চলতে থাকে)। নতুন
# স্ট্রিম কিন্তু ৪ সেকেন্ডেই সিদ্ধান্ত নেয় — শুরুতে অপেক্ষা বাড়ালে
# "লোডিং আটকে থাকা" অনুভূতি হতো।
CONT_GATE_WAIT = _env_int("CONT_GATE_WAIT", 9)
GLOBAL_MAX_WAITERS = _env_int("GLOBAL_MAX_WAITERS", 48)  # অপেক্ষার লাইনে সর্বোচ্চ এত রিকোয়েস্ট (মেমোরি সুরক্ষা)

# ⚡⚡ ওভারলোড গার্ড (CPU মৃত্যু-চক্র ভালভ) — OVERLOAD_GUARD=0 দিলে বন্ধ।
# আগের মহাবিপর্যয়ের ধরন (লাইভ লগ ১৪:২৩–১৪:২৭): CPU ১০০% → একাধিক
# ক্লায়েন্ট একসাথে বেঞ্চ হয় → পুরো লোড প্রিমিয়ামের উপর চাপা পড়ে →
# প্রিমিয়ামও ভাঙে। এখন অল্প সময়ে একাধিক বেঞ্চ দেখা গেলে সার্ভার নিজেই
# "সতর্ক মোডে" যায়: নতুন স্ট্রিম ভর্তি ক্যাপাসিটির ৭০%-এ সীমিত — চলমান
# স্ট্রিমে কেউ ছোঁ মারে না, তাই দেখছে ইউজাররা কিছুই টের পায় না।
OVERLOAD_GUARD = bool(_env_int("OVERLOAD_GUARD", 1))
OVERLOAD_BENCH_COUNT = _env_int("OVERLOAD_BENCH_COUNT", 2)  # এতগুলো বেঞ্চ...
OVERLOAD_WINDOW = _env_int("OVERLOAD_WINDOW", 60)           # ...এত সেকেন্ডে হলে সতর্ক মোড
OVERLOAD_HOLD = _env_int("OVERLOAD_HOLD", 90)               # শেষ বেঞ্চের পর এত সেকেন্ড সতর্ক মোড থাকে

# ⚡ লগ থ্রটল: ভিড়ের সময় আগে প্রতি সেকেন্ডে ২টা করে WARNING জমত —
# Koyeb লগ দেখা অসম্ভব হয়ে যেত আর লগ-IO-ও CPU খেত। এখন এই মাস্কে
# সর্বোচ্চ ১টি লগ + চাপা পড়া সংখ্যা সাথে করে দেখায়।
SHED_LOG_INTERVAL = _env_int("SHED_LOG_INTERVAL", 5)
STATUS_LOG_INTERVAL = _env_int("STATUS_LOG_INTERVAL", 120)


def premium_index():
    """প্রিমিয়াম ক্লায়েন্টের নম্বর (সবচেয়ে বড় index) — এক ক্লায়েন্ট হলে None"""
    keys = list(work_loads.keys())
    return max(keys) if len(keys) > 1 else None


def _gate(index):
    g = client_gates.get(index)
    if g is None:
        limit = PREMIUM_GATE_LIMIT if index == premium_index() else STREAM_GATE_LIMIT
        g = asyncio.BoundedSemaphore(limit)
        client_gates[index] = g
    return g


async def acquire_gate(index, timeout):
    """গেটে জায়গা পেলে True — না পেলে False (504 দিলে player নিজেই রিট্রাই করবে)"""
    try:
        await asyncio.wait_for(_gate(index).acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


def release_load(index):
    work_loads[index] = max(0, work_loads.get(index, 1) - 1)
    try:
        _gate(index).release()
    except ValueError:
        pass


def _make_load_releaser(index):
    """ওই ক্লায়েন্টের লোড কাউন্টার + গেট ছাড়ানোর কলব্যাক"""
    def _release():
        release_load(index)
    return _release


# ⚡⚡ অ্যাডমিশন স্টেট — ভিড়ের মধ্যে কাকে অপেক্ষা করাতে হবে আর কাকে না,
# তা বুদ্ধিমানভাবে ঠিক করে (ইউজার-অদৃশ্য স্মুথ প্লেব্যাক + সিপিইউ সুরক্ষা)
_global_active = 0        # এই মুহূর্তে চলমান স্ট্রিম সংখ্যা (সার্বিক গেটের ভেতরে)
_global_waiters = 0       # এই মুহূর্তে জায়গার অপেক্ষায় বসে থাকা রিকোয়েস্ট
_overload_until = 0.0     # এই সময়ের আগে পর্যন্ত সতর্ক মোড চালু থাকবে
_last_status_log = 0.0
_last_shed_log = 0.0
_suppressed_sheds = 0
bench_events: deque = deque(maxlen=32)   # বেঞ্চ হওয়ার সময়গুলো (মৃত্যু-চক্র শনাক্তে)
_guard_task = None        # ব্যাকগ্রাউন্ড গার্ড লুপ (প্রথম রিকোয়েস্টেই চালু হয়)


def _overload_active(now=None):
    return (now or time.time()) < _overload_until


def _soft_cap():
    """সতর্ক মোডে নতুন স্ট্রিম ক্যাপাসিটির ৭০%-এ সীমিত (সর্বনিম্ন ৮)।
    চলমান স্ট্রিম কাটা হয় না — শুধু নতুন ভর্তি থামে, ফলে CPU দ্রুত
    নিঃশ্বাস নেয় এবং বেঞ্চ হওয়া ক্লায়েন্টরা শান্তিতে সেরে ওঠে।"""
    return max(int(TOTAL_STREAM_CAPACITY * 0.7), min(8, TOTAL_STREAM_CAPACITY))


async def acquire_global_gate(timeout, continuation=False):
    """সার্বিক ক্যাপাসিটিতে জায়গা মিললে True।
    ⚡ continuation=True (দেখা চালিয়ে যাওয়ার রিকোয়েস্ট) হলে বেশি অপেক্ষা —
    প্লেয়ারের বাফার অপেক্ষাটা চুপিই গিলে নেয়, ইউজার ভাঙে না।
    ⚡ সতর্ক মোডে নতুন স্ট্রিম soft cap ছাড়ালে সাথে সাথে ফেরত যায় —
    কোনো Telegram কল ছাড়াই, মানে CPU খরচ প্রায় শূন্য।"""
    global _global_active, _global_waiters
    if global_gate is None:
        return True
    now = time.time()
    if (not continuation) and _overload_active(now) and _global_active >= _soft_cap():
        return False
    if _global_waiters >= GLOBAL_MAX_WAITERS:
        return False
    _global_waiters += 1
    try:
        try:
            await asyncio.wait_for(global_gate.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        _global_active += 1
        return True
    finally:
        _global_waiters -= 1


def _release_global_gate():
    # দুইবার ডাকাও নিরাপদ রাখতে ValueError চুপচাপ গিলে নেওয়া হয়
    global _global_active
    try:
        if global_gate is not None:
            global_gate.release()
            if _global_active > 0:
                _global_active -= 1
    except ValueError:
        pass


def _shed_log(message):
    """একই ধরনের শেড-ওয়ার্নিং ৫ সেকেন্ডে সর্বোচ্চ ১ বার লগ হয় —
    বাকিগুলো গুনে রাখা হয়, পরের লগে '+N suppressed' দেখায়।"""
    global _last_shed_log, _suppressed_sheds
    now = time.time()
    if now - _last_shed_log >= SHED_LOG_INTERVAL:
        suffix = f" (+{_suppressed_sheds} suppressed)" if _suppressed_sheds else ""
        _suppressed_sheds = 0
        _last_shed_log = now
        logging.warning(f"{message}{suffix}")
    else:
        _suppressed_sheds += 1


async def admission_guard_loop():
    """⚡⚡ CPU মৃত্যু-চক্র ভালভ (ব্যাকগ্রাউন্ড, প্রতি ১৫ সেকেন্ডে একবার —
    খরচ কার্যত শূন্য)। অল্প সময়ে একাধিক ক্লায়েন্ট বেঞ্চ হলে সেটা
    মেল্ডাউনের শুরু — সেখানেই নতুন ভর্তি কমিয়ে দেয়, যাতে আগের মতো
    পুরো লোড প্রিমিয়ামের উপর চাপা না পড়ে। ৯০ সেকেন্ড শান্তি পেলে
    নিজে থেকেই স্বাভাবিক হয়ে যায়।"""
    global _overload_until, _last_status_log
    await asyncio.sleep(15)   # সার্ভার পুরোপুরি চালু হোক
    while True:
        try:
            now = time.time()
            if OVERLOAD_GUARD:
                recent = [t for t in bench_events if now - t <= OVERLOAD_WINDOW]
                if len(recent) >= OVERLOAD_BENCH_COUNT:
                    if not _overload_active(now):
                        logging.warning(
                            f"Overload guard ON — new streams limited to {_soft_cap()}, "
                            f"running streams untouched ({len(recent)} benches in {OVERLOAD_WINDOW}s)"
                        )
                    _overload_until = now + OVERLOAD_HOLD
            if now - _last_status_log >= STATUS_LOG_INTERVAL:
                _last_status_log = now
                logging.info(
                    f"Admission status: {_global_active}/{TOTAL_STREAM_CAPACITY} live, "
                    f"{_global_waiters} waiting, overload-guard "
                    f"{'ON' if _overload_active(now) else 'off'}"
                )
        except Exception:
            pass
        await asyncio.sleep(15)


def _ensure_admission_guard():
    """প্রথম স্ট্রিম রিকোয়েস্টেই গার্ড লুপ চালু (তখনই ইভেন্ট-লুপ নিশ্চিত)"""
    global _guard_task
    if _guard_task is None:
        try:
            _guard_task = asyncio.create_task(admission_guard_loop())
        except RuntimeError:
            pass


def _run_releasers(releasers):
    """ধরা জায়গাগুলো এক জায়গা থেকে ছাড়ানো — যতবারই ডাকা হোক, প্রতিটি
    জায়গা ঠিক একবারই ছাড়া হয় (লিস্ট খালি হয়ে যায়)।"""
    while releasers:
        fn = releasers.pop()
        try:
            fn()
        except Exception:
            pass


def pick_client_index(exclude_premium=True, exclude=frozenset()):
    """সুস্থ বটদের মধ্যে সবচেয়ে কম লোডের ক্লায়েন্ট।
    ⚡ ছোট ফাইল সাধারণত প্রিমিয়ামে যায় না; তবে সব বট বেঞ্চড হলে
    প্রিমিয়াম শেষ ভরসা — তখন ও ধরা পড়বে (ইউজার ফাঁকা থাকবে না)।
    ⚡ exclude — এই রিকোয়েস্টে যেসব ক্লায়েন্ট ইতিমধ্যে ফেল করেছে, ওদের
    আবার বেছে নেওয়া হবে না (ট্রান্সপারেন্ট রিট্রাইয়ের জন্য)।"""
    now = time.time()
    all_keys = list(work_loads.keys())
    pm = premium_index()
    bots = [i for i in all_keys if i != pm] if (exclude_premium and pm not in (None, 0)) else all_keys
    if exclude:
        bots = [i for i in bots if i not in exclude] or bots
    healthy = [i for i in bots if not _cooling(i, now)]
    if healthy:
        pool = healthy
    else:
        # সব বট বেঞ্চড হলে সুস্থ প্রিমিয়ামই শেষ ভরসা; কেউ সুস্থ না থাকলে সবাই
        pool = [i for i in all_keys if not _cooling(i, now)] or all_keys
    return min(pool, key=lambda i: work_loads.get(i, 0))


async def _late_cancel(task, delay=90):
    """আটকে-যাওয়া pyrogram task-টা অনেকক্ষণ পরে best-effort ভাবে বন্ধ করার চেষ্টা।
    সাথে সাথে cancel করি না — কারণ pyrogram মাঝে মাঝে cancellation উপেক্ষা করে।"""
    try:
        await asyncio.sleep(delay)
        if not task.done():
            task.cancel()
    except Exception:
        pass


@routes.get("/", allow_head=True)
async def root_route_handler(_):
    return web.json_response({
        "server_status": "running",
        "uptime": get_readable_time(time.time() - StartTime),
        "telegram_bot": "@" + temp.U_NAME,
        "connected_bots": len(multi_clients),
        "loads": {
            "bot" + str(i + 1): load
            for i, (_, load) in enumerate(
                sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
            )
        },
        "client_health": {
            str(k): ("cooldown" if _cooling(k) else "ok")
            for k in multi_clients.keys()
        },
        "prewarmed": sorted(prewarmed),
        "version": __version__,
    })

# ---------------------------------------------------------------
#  PASSWORD PROTECTED REDIRECT ROUTES
# ---------------------------------------------------------------

@routes.get(r"/p/{token}", allow_head=True)
async def protected_view(request: web.Request):
    token = request.match_info["token"]
    data = await db.get_protected_link(token)

    if not data:
        return web.Response(text="Link not found.", status=404)

    async with aiofiles.open("web/template/password_redirect.html", mode='r') as f:
        template_content = await f.read()
        template = jinja2.Template(template_content)

    return web.Response(
        text=template.render(
            token=token,
            title=data.get("title", "Protected Link"),
            channel_link=data.get("channel_link")
        ),
        content_type="text/html"
    )

@routes.post(r"/p/{token}")
async def protected_verify(request: web.Request):
    token = request.match_info["token"]
    data = await request.post()
    user_pass = data.get("password")

    link_data = await db.get_protected_link(token)

    if not link_data:
        return web.Response(text="Link invalid.", status=404)

    if user_pass == link_data["password"]:
        return web.HTTPFound(link_data["url"])
    else:
        async with aiofiles.open("web/template/password_redirect.html", mode='r') as f:
            template_content = await f.read()
            template = jinja2.Template(template_content)

        return web.Response(
            text=template.render(
                token=token,
                error="❌ Wrong Password!",
                title=link_data.get("title", "Protected Link"),
                channel_link=link_data.get("channel_link")
            ),
            content_type="text/html"
        )

# ⚠️ FIX: {path:.+} — আগে \S+ ছিল। নতুন aiohttp-এ লিংকের নামে স্পেস (%20)
# থাকলে রাউট ম্যাচই করত না → সব স্ট্রিম/ডাউনলোড "404: Not Found" দিত।
# মুভির নামে স্পেস/বাংলা যা-ই থাকুক, এখন সব লিংক কাজ করবে।
@routes.get(r"/watch/{path:.+}", allow_head=True)
async def watch_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        # ⚡ HEAD-এ টেমপ্লেট রেন্ডারের দরকার নেই — সস্তা হেডার-উত্তরই যথেষ্ট
        if request.method == "HEAD":
            return web.Response(status=200, content_type="text/html")
        return web.Response(
            text=await render_page(id, secure_hash), content_type="text/html"
        )
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FileNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        return web.Response(status=400, text="Connection Error")
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


# ---------------------------------------------------------------
# 🖼️ THUMBNAIL ROUTE — মুভির ছবি (পেজ খোলার সাথে সাথেই দেখাবে)
# ---------------------------------------------------------------
# টেলিগ্রাম প্রতিটি ভিডিওর জন্য নিজে থেকেই একটা স্ক্রিনশট (thumbnail) বানায়।
# সেটা সার্ভ করলে ইউজার ভিডিও ডিকোডের অপেক্ষায় কালো স্ক্রিন দেখে না —
# HEVC 10-bit ফাইলেও মুভির ছবি সাথে সাথে আসে।
thumb_cache: dict = {}          # msg_id -> (jpeg_bytes, ts)
THUMB_CACHE_MAX = 800


@routes.get(r"/thumb/{path:.+}", allow_head=True)
async def thumb_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)", path).group(1))
            secure_hash = request.rel_url.query.get("hash")

        # হ্যাশ ছাড়া / ভুল হ্যাশ → ছবি কেউ চুরি করতে পারবে না
        if not secure_hash:
            return web.Response(status=403, text="Forbidden")

        # ⚡ HEAD + ক্যাশ হিট = বাইট না পাঠিয়ে শুধু হেডার (ক্যাশ মিস হলে নিচে
        # স্বাভাবিক পথে গিয়ে একবার নামিয়ে নেবে, পরে সব HEAD ফ্রি)
        if request.method == "HEAD":
            cached = thumb_cache.get(id)
            if cached:
                return web.Response(
                    status=200,
                    content_type="image/jpeg",
                    headers={
                        "Cache-Control": "public, max-age=604800",
                        "Content-Length": str(len(cached[0])),
                    },
                )

        cached = thumb_cache.get(id)
        if cached:
            return web.Response(
                body=cached[0],
                content_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=604800"},
            )

        file = await WebavBot.get_messages(int(BIN_CHANNEL), int(id))
        media = get_media_from_message(file)
        if media is None:
            return web.Response(status=404, text="No media")

        uid = getattr(media, "file_unique_id", "")
        if uid[:6] != secure_hash:
            return web.Response(status=403, text="Forbidden")

        thumbs = getattr(media, "thumbs", None) or []
        if not thumbs:
            return web.Response(status=404, text="No thumbnail")

        # FloodWait হলে ২ বার পর্যন্ত অপেক্ষা করে আবার চেষ্টা
        # ⚠️ শুধু thumbnail অবজেক্টটা ডাউনলোড হবে — পুরো ভিডিও নয় (ভুল হলে 1GB নামতো!)
        bio = None
        for attempt in range(3):
            try:
                bio = await WebavBot.download_media(thumbs[-1], in_memory=True)
                break
            except FloodWait as e:
                if e.value > MAX_FLOODWAIT or attempt == 2:
                    return web.Response(status=429, text="Busy")
                await asyncio.sleep(e.value + 1)
            except Exception:
                return web.Response(status=404, text="No thumbnail")

        if not bio:
            return web.Response(status=404, text="No thumbnail")

        data = bio.getvalue()
        if not data:
            return web.Response(status=404, text="No thumbnail")

        # ছোট্ট মেমোরি ক্যাশ (থাম্বনেইল কখনো বদলায় না)
        if len(thumb_cache) >= THUMB_CACHE_MAX:
            thumb_cache.pop(next(iter(thumb_cache)))
        thumb_cache[id] = (data, time.time())

        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )
    except FileNotFound:
        return web.Response(status=404, text="No thumbnail")
    except Exception as e:
        logging.warning(f"Thumb route error for {request.path}: {e}")
        return web.Response(status=404, text="No thumbnail")


@routes.get(r"/{path:.+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        # ⚡ HEAD = বডি কেউ চায় না — টেলিগ্রাম স্ট্রিম শুরু না করে শুধু হেডার
        if request.method == "HEAD":
            return await media_streamer_head(request, id, secure_hash)
        return await media_streamer(request, id, secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FileNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        return web.Response(status=400, text="Connection Error")
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


class_cache = {}


async def get_props_safe(streamer, message_id):
    """ফাইলের তথ্য নিতে গিয়ে FloodWait এলে অপেক্ষা করে আবার চেষ্টা করে"""
    for _ in range(3):
        try:
            return await streamer.get_file_properties(message_id)
        except FloodWait as e:
            if e.value > MAX_FLOODWAIT:
                raise
            logging.warning(f"FloodWait {e.value}s in get_file_properties — waiting then retrying")
            await asyncio.sleep(e.value + 1)
    return await streamer.get_file_properties(message_id)


def streamer_for(client):
    """প্রতিটি ক্লায়েন্টের নিজের streamer"""
    if client not in class_cache:
        class_cache[client] = ByteStreamer(client)
    return class_cache[client]


async def guarded_stream(src_it, first_chunk, index, releasers=None, idle_timeout=None):
    """⚡ প্রথম chunk পাওয়ার পর প্রতিটি chunk-কে টাইমারের সাথে race করানো হয়।
    Telegram চুপ হলে অনন্ত অপেক্ষা নয় — স্ট্রিম ছেড়ে দেওয়া হয়, ফলে
    connection + load counter সাথে সাথে খালি হয়ে যায় এবং player নিজে
    শেষ buffered পয়েন্ট থেকে আবার চেষ্টা করে।
    গুরুত্বপূর্ণ: এখানে কখনোই pyrogram task-কে সাথে সাথে cancel করার
    ঝুঁকি নেওয়া হয় না — শুধু নিজের দিক থেকে হাল ছাড়া হয়।
    ⚡ releasers: ধরা জায়গাগুলো (ক্লায়েন্ট লোড + সার্বিক গেট) স্ট্রিম শেষে
    এখান থেকেই ছাড়া হয় — মাঝপথে মরা স্ট্রিমেও জায়গা আটকে থাকবে না।"""
    if idle_timeout is None:
        idle_timeout = STREAM_IDLE_TIMEOUT
    if releasers is None:
        releasers = [_make_load_releaser(index)]
    it = src_it
    task = None

    try:
        yield first_chunk
        while True:
            task = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({task}, timeout=idle_timeout)
            if not done:
                mark_client_failure(index)
                asyncio.create_task(_late_cancel(task))
                logging.error(
                    f"Stream idle > {idle_timeout}s on client {index} — abandoned, player will retry"
                )
                raise ConnectionResetError("stream abandoned")
            try:
                chunk = task.result()
            except StopAsyncIteration:
                break
            except FloodWait as e:
                mark_client_failure(index)
                logging.warning(f"FloodWait {e.value}s mid-stream on client {index} — abandoned, player will retry")
                raise ConnectionResetError("stream abandoned")
            except Exception as e:
                # Telegram-পাশের সংযোগ মারা গেল (Connection lost / Broken pipe) —
                # আগে চুপচাপ গিলে ফেলা হতো, তাই ভাঙা ক্লায়েন্ট সত্যিকারের
                # bench হতো না। এখন ব্যর্থতা গণনায় ধরা হবে।
                mark_client_failure(index)
                logging.error(f"Stream died on client {index}: {e} — counted as failure")
                raise ConnectionResetError("stream abandoned")
            mark_client_success(index)
            yield chunk
    finally:
        _run_releasers(releasers)
        if task is not None and not task.done():
            task.cancel()
        try:
            await it.aclose()
        except Exception:
            pass


def _busy_504():
    # ৩-৬ সেকেন্ডে এলোমেলো বিলম্ব — সব প্লেয়ার একসাথে ধাক্কা মারলে
    # আবার ভিড় জমে যায়, তা ভাঙতে জিটার
    return web.Response(
        status=504,
        text="Upstream busy — auto retrying…",
        headers={"Retry-After": str(3 + secrets.randbelow(4))},
    )


# ---------------------------------------------------------------
# ⚡⚡ HEAD SHORT-CIRCUIT — CPU বাঁচানোর সবচেয়ে সস্তা উপায়
# ---------------------------------------------------------------
# প্লেয়ার, আপটাইম-মনিটর, ব্রাউজার প্রিফ্লাইট, স্ক্র্যাপার — অনেকেই ভিডিও
# লিংকে HEAD রিকোয়েস্ট পাঠায়। আগে প্রতিটি HEAD ক্যাচ-অল রুট দিয়ে
# media_streamer-এ ঢুকে যেত: মানে প্রতিটি HEAD = props লুকআপ + gate +
# টেলিগ্রামে GetFile (আসল স্ট্রিম শুরু!) — কেউ বডি পড়বে না জেনেও।
# এখন HEAD-এ শুধু ক্যাশড props দিয়ে হেডার ফেরত — টেলিগ্রামের GetFile-এ
# হাতই দেওয়া হয় না। একটাও বাইট ডাউনলোড ছাড়া উত্তর।
def _base_streamer():
    c = multi_clients.get(0) or next(iter(multi_clients.values()))
    return streamer_for(c)


async def media_streamer_head(request: web.Request, id: int, secure_hash: str):
    streamer = _base_streamer()
    file_id = await get_props_safe(streamer, id)

    if file_id.unique_id[:6] != secure_hash:
        raise InvalidHash

    file_size = file_id.file_size
    mime_type = (file_id.mime_type
                 or mimetypes.guess_type(file_id.file_name or "")[0]
                 or "application/octet-stream")

    headers = {
        "Content-Type": str(mime_type),
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        "CDN-Cache-Control": "no-store",
    }

    range_header = request.headers.get("Range")
    if range_header:
        rm = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", str(range_header).strip())
        if rm and (rm.group(1) or rm.group(2)):
            from_bytes = (file_size - min(int(rm.group(2)), file_size)) if rm.group(1) == "" else int(rm.group(1))
        else:
            from_bytes = 0
        if 0 <= from_bytes < file_size:
            headers["Content-Range"] = f"bytes {from_bytes}-{file_size - 1}/{file_size}"
            headers["Content-Length"] = str(file_size - from_bytes)
            return web.Response(status=206, headers=headers)

    headers["Content-Length"] = str(file_size)
    return web.Response(status=200, headers=headers)


async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    # ⚡ STEP 0: সার্বিক ভর্তি-নিয়ন্ত্রণ (সিপিইউ মৃত্যু-চক্রের স্থায়ী সমাধান)।
    # কোনো Telegram কলের আগেই — একেবারে সস্তা জায়গায় — বাড়তি চাপ সামলানো হয়।
    # ⚡⚡ স্মার্ট লাইন: "bytes=N-" (N>0) মানে ইউজার এই মুহূর্তে ভিডিওটা দেখছে,
    # শুধু পরের অংশ চাইছে — এদের ৯ সেকেন্ড পর্যন্ত অপেক্ষা করানো হয় কারণ
    # প্লেয়ারের বাফার অপেক্ষাটা গিলে নেয় (ইউজার কিছু টের পায় না)। নতুন
    # স্ট্রিম/শুরুর রিকোয়েস্ট ৪ সেকেন্ডেই সিদ্ধান্ত পায়। সতর্ক মোডে শুধু
    # নতুন স্ট্রিম সাথে সাথে ফেরত যায় — চলমান কারও ক্ষতি হয় না।
    _ensure_admission_guard()
    _cont = False
    if isinstance(range_header, str) and range_header:
        _rm0 = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", range_header.strip())
        if _rm0 and _rm0.group(1) and int(_rm0.group(1)) > 0:
            _cont = True
    if not await acquire_global_gate(
        CONT_GATE_WAIT if _cont else GLOBAL_GATE_WAIT, continuation=_cont
    ):
        _shed_log(
            f"Server capacity full ({_global_active}/{TOTAL_STREAM_CAPACITY} live, "
            f"{_global_waiters} waiting)"
            + (" — overload guard" if _overload_active() else "")
            + f" — shed (msg {id})"
        )
        return _busy_504()

    # ⚡ জায়গা-ম্যানেজমেন্ট: load_releasers — প্রতি চেষ্টায় ধরা ক্লায়েন্ট-লোড।
    # সার্বিক গেট একবারই নেওয়া হয় এবং শেষ পর্যন্ত ধরে রাখা হয়: সফল হলে
    # guarded_stream স্ট্রিম শেষে ছাড়বে, ব্যর্থ হলে নিচের এক্সিট পথগুলোতেই।
    # (_release_global_gate দুইবার ডাকা নিরাপদ — ValueError গিলে নেয়।)
    load_releasers = []
    try:
        # ⚡ STEP 1: ডিফল্ট ক্লায়েন্ট দিয়ে আগে ফাইলের তথ্য (সাইজ জানার জন্য)
        base_client = multi_clients.get(0) or next(iter(multi_clients.values()))
        base_streamer = streamer_for(base_client)

        file_id = await get_props_safe(base_streamer, id)
        file_size = file_id.file_size

        # ⚡ হ্যাশ চেক — সব ক্লায়েন্টের props-এ unique_id একই, একবারই যথেষ্ট
        if file_id.unique_id[:6] != secure_hash:
            logging.debug(f"Invalid hash for message with ID {id}")
            raise InvalidHash

        # 🔧 FIX (ডিউরেশন ০০:০০ + সিকে জিরো থেকে প্লে + বড় সিকে ফ্রিজ):
        # ব্রাউজার মুভির ইনডেক্স/ডিউরেশন পড়তে "bytes=-N" (ফাইলের শেষের N বাইট)
        # রিকোয়েস্ট পাঠায়। আগের কোড এতে int('') দিয়ে ক্র্যাশ করত → HTTP 500 →
        # ব্রাউজার ইনডেক্স পায় না → ডিউরেশন ০, সিক দিলে ০ থেকে রিস্টার্ট।
        # (রেঞ্জ এখন লুপের বাইরে একবারই হিসাব হয় — প্রতিটি ক্লায়েন্টে একই ফাইল।)
        if range_header:
            rm = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", str(range_header).strip())
            if rm and (rm.group(1) or rm.group(2)):
                if rm.group(1) == "":
                    # suffix: "bytes=-N" = শেষের N বাইট
                    n = min(int(rm.group(2)), file_size)
                    from_bytes = file_size - n
                    until_bytes = file_size - 1
                else:
                    from_bytes = int(rm.group(1))
                    until_bytes = int(rm.group(2)) if rm.group(2) else file_size - 1
            else:
                from_bytes, until_bytes = 0, file_size - 1
        else:
            from_bytes = request.http_range.start or 0
            until_bytes = (request.http_range.stop or file_size) - 1

        if from_bytes < 0 or from_bytes >= file_size:
            _release_global_gate()
            return web.Response(
                status=416,
                body="416: Range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        chunk_size = 1024 * 1024
        until_bytes = min(until_bytes, file_size - 1)

        offset = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = until_bytes % chunk_size + 1

        req_length = until_bytes - from_bytes + 1
        part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)

        # ⚡⚡ ট্রান্সপারেন্ট রিট্রাই: প্রথম chunk যেই ক্লায়েন্টে ফেল করল, ইউজারকে
        # সাথে সাথে 504 না দিয়ে চুপচাপ অন্য সুস্থ ক্লায়েন্টে আরেকবার চেষ্টা হয়।
        # ইউজার শুধু ১ সেকেন্ডের বাড়তি লোডিং দেখে — ভাঙা ক্লায়েন্ট টেরই পায় না।
        tried = set()
        for attempt in range(1, FIRST_CHUNK_RETRIES + 2):
            # ⚡ STEP 2: স্মার্ট ক্লায়েন্ট নির্বাচন (সুস্থ + কম লোড প্রাধান্য)
            if file_size > PREMIUM_SIZE_LIMIT and len(multi_clients) > 1:
                premium_idx = max(multi_clients.keys())
                if premium_idx != 0 and premium_idx not in tried and not _cooling(premium_idx):
                    index = premium_idx
                    if attempt == 1:
                        logging.info(f"Big file ({file_size / 1024 / 1024 / 1024:.2f} GB) -> Premium Client {index}")
                else:
                    index = pick_client_index(exclude=tried)
            else:
                index = pick_client_index(exclude=tried)

            # ⚡ STEP 2.5: GATE — এক ক্লায়েন্টে বেশি লাইভ স্ট্রিম ঢুকলে session
            # ভেঙে যায়। না পেলে অন্য সুস্থ ক্লায়েন্টে ওভারফ্লো, সব ভরা হলে
            # ছোট্ট 504 — player নিজে থেকেই আবার চেষ্টা করবে।
            if not await acquire_gate(index, STREAM_GATE_WAIT):
                _now = time.time()
                _pm = premium_index()
                _others = [i for i in work_loads.keys() if i != index and not _cooling(i, _now) and i not in tried]
                # ছোট ফাইল: আগে অন্য বটরা, সবাই ফুল হলে শেষে প্রিমিয়াম হেল্প করবে
                # বড় ফাইল: index নিজেই প্রিমিয়াম, তাই তালিকায় বটরা-ই থাকে
                if file_size > PREMIUM_SIZE_LIMIT or _pm is None:
                    alts = sorted(_others, key=lambda i: work_loads.get(i, 0))
                else:
                    _bots = sorted((i for i in _others if i != _pm), key=lambda i: work_loads.get(i, 0))
                    alts = _bots + ([_pm] if _pm in _others else [])
                moved = False
                for alt in alts:
                    if await acquire_gate(alt, OVERFLOW_GATE_WAIT):
                        index = alt
                        moved = True
                        break
                if moved:
                    logging.info(f"Gate full — overflow -> Client {index}")
                else:
                    _shed_log(
                        f"All client gates full ({_global_active}/{TOTAL_STREAM_CAPACITY} live) — shed (msg {id})"
                    )
                    _release_global_gate()
                    return _busy_504()

            # ⚡ STEP 3: যে ক্লায়েন্ট স্ট্রিম দেবে, তার নিজের streamer + নিজের fresh
            # file reference। base ক্লায়েন্ট হলে STEP 1-এর props-ই চলে — দ্বিতীয়
            # লুকআপ (Telegram getMessages) একদম বাদ।
            if index == base_client:
                tg_connect = base_streamer
            else:
                tg_connect = streamer_for(multi_clients[index])
                try:
                    file_id = await get_props_safe(tg_connect, id)
                    file_size = file_id.file_size
                except Exception as e:
                    # এই ক্লায়েন্ট ফাইলের তথ্যই দিতে পারল না — বেঞ্চ গণনা,
                    # তারপর 504 না দিয়ে অন্য ক্লায়েন্টে চুপচাপ রিট্রাই
                    mark_client_failure(index)
                    tried.add(index)
                    logging.warning(f"Client {index} props failed ({e}) — transparent retry on another client")
                    continue

            if MULTI_CLIENT:
                logging.info(f"Client {index} is now serving {request.remote}")

            # ⚡ load কাউন্টার routes নিজে সামলায় — ঝুলে-যাওয়া স্ট্রিমও কাউন্টার
            # আটকে রাখতে পারবে না; সব জায়গা releasers দিয়ে ছাড়ানো হয়
            work_loads[index] = work_loads.get(index, 0) + 1
            load_releasers.append(_make_load_releaser(index))
            raw_body = tg_connect.yield_file(
                file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size, id
            )

            # ⚡⚡ সবচেয়ে জরুরি ধাপ: প্রথম chunk হাতে না পেয়ে headers পাঠাই না।
            # শুরুতেই আটকালে ইউজার অনন্ত লোডিং দেখবে না — এক ক্লায়েন্ট ফেল
            # করলে অন্য ক্লায়েন্টে চুপচাপ রিট্রাই, সব না হলে ছোট্ট 504।
            it = raw_body.__aiter__()
            task = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({task}, timeout=STREAM_FIRST_CHUNK_TIMEOUT)
            if not done:
                _run_releasers(load_releasers)
                mark_client_failure(index)
                asyncio.create_task(_late_cancel(task))
                tried.add(index)
                logging.error(f"First chunk timeout ({STREAM_FIRST_CHUNK_TIMEOUT}s) on client {index} — transparent retry")
                continue
            try:
                first_chunk = task.result()
            except StopAsyncIteration:
                first_chunk = b""
            except FloodWait as e:
                _run_releasers(load_releasers)
                mark_client_failure(index)
                tried.add(index)
                logging.warning(f"FloodWait {e.value}s at stream start on client {index} — transparent retry")
                continue
            except Exception as e:
                _run_releasers(load_releasers)
                mark_client_failure(index)
                tried.add(index)
                logging.error(f"Stream start failed on client {index}: {type(e).__name__}: {e} — transparent retry")
                continue

            if not first_chunk:
                _run_releasers(load_releasers)
                mark_client_failure(index)
                tried.add(index)
                logging.error(f"Empty stream start on client {index} (msg {id}) — transparent retry")
                continue

            mark_client_success(index)
            break
        else:
            # সব চেষ্টা শেষ — এবার সত্যিই 504 (প্লেয়ার নিজে আবার চেষ্টা করবে)
            logging.error(f"All {FIRST_CHUNK_RETRIES + 1} stream attempts failed for msg {id} — 504, player will retry")
            _run_releasers(load_releasers)
            _release_global_gate()
            return _busy_504()

        # ⚡ সফল স্ট্রিম: সার্বিক গেটের মালিকানা guarded_stream-এর কাছে যাচ্ছে —
        # স্ট্রিম শেষ (বা মাঝপথে মরলেও) সেখান থেকেই ছাড়া হবে।
        load_releasers.append(_release_global_gate)

        mime_type = file_id.mime_type
        file_name = file_id.file_name
        disposition = "attachment"

        # mimetypes.guess_type() tuple রিটার্ন করে — শুধু টাইপটা নেওয়া হচ্ছে
        if not mime_type:
            mime_type = mimetypes.guess_type(file_name or "")[0] or "application/octet-stream"
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"

        headers = {
            "Content-Type": str(mime_type),
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
            # CORS: অন্য ডোমেইন থেকে (যেমন মেইন সাইট) ভিডিও চলবে এবং
            # প্রথম সিনের পোস্টারও আঁকা যাবে
            "Access-Control-Allow-Origin": "*",
            # 🔧 CRITICAL FIX (স্ট্রিম ঝুলে-যাওয়া / সিকে জিরো / লোডিং আটকে থাকা):
            # Koyeb-এর সামনের Cloudflare এজ ভিডিও রেসপন্স ক্যাশে রাখার সময় পুরো
            # বডি বাফার করে — তখন লাইভ ইউজার মিনিটের পর মিনিট ০ বাইট পেয়ে ঝুলে
            # থাকে (লাইভ টেস্টে প্রমাণিত: MISS = ১০০ সেকেন্ডে ০ বাইট, HIT = সাথে সাথে)।
            # no-store দিলে এজ ক্যাশ ছাড়াই সরাসরি পাস-থ্রু করে → স্ট্রিম লাইভ চলে।
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "CDN-Cache-Control": "no-store",
            "Cloudflare-CDN-Cache-Control": "no-store",
        }
        # Content-Range শুধু Range request এ পাঠানো ঠিক
        if range_header:
            headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

        body = guarded_stream(it, first_chunk, index, releasers=load_releasers)
        return web.Response(
            status=206 if range_header else 200,
            body=body,
            headers=headers,
        )
    except Exception:
        # যেন কোনো পথেই ধরা জায়গা ফাঁকা না থেকে যায় (দ্বিগুণ ছাড়াও নিরাপদ)
        _run_releasers(load_releasers)
        _release_global_gate()
        raise


# ---------------------------------------------------------------
# ⚡ STARTUP PREWARM: সব ক্লায়েন্টের Telegram media session আগেই তৈরি
# ---------------------------------------------------------------
# bot চালু হওয়ার পর প্রতিটি ক্লায়েন্টের প্রথম স্ট্রিমেই media session
# তৈরি হতো — আর সেখানেই (Session.start-এর অসীম লুপে) ঝুলে যেত।
# এখন ব্যাকগ্রাউন্ডে আগেই সেশন বানিয়ে রাখা হয়, তাই প্রথম ইউজারও
# সাথে সাথে স্ট্রিম পায়।

async def _pick_recent_file_ids(limit=6):
    """Prewarm-এর জন্য BIN channel-এর সাম্প্রতিক কিছু ফাইল message-id বের করে।
    নোট: বট অ্যাকাউন্ট get_chat_history ব্যবহার করতে পারে না (Telegram নিষেধ),
    তাই মূল উৎস হলো ডাটাবেসের ফাইল রেকর্ড (private_stream সেখানে id রাখে)।"""
    ids = []
    # উৎস ১ (মূল): ডাটাবেস — সর্বশেষ আপলোড হওয়া ফাইলগুলো
    try:
        cursor = db.files.find({}, sort=[("timestamp", -1)], limit=limit)
        async for doc in cursor:
            fid = doc.get("file_id")
            if fid:
                try:
                    fid = int(fid)
                except (TypeError, ValueError):
                    continue
                if fid not in ids:
                    ids.append(fid)
    except Exception as e:
        logging.debug(f"Prewarm: db lookup failed: {e}")
    # উৎস ২ (বিকল্প): userbot হলে history পড়া যায় — বট হলে চুপচাপ বাদ
    if not ids:
        try:
            async for m in WebavBot.get_chat_history(BIN_CHANNEL, limit=15):
                if getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None):
                    ids.append(m.id)
                    if len(ids) >= limit:
                        break
        except Exception as e:
            logging.debug(f"Prewarm: history read skipped: {e}")
    return ids


async def _prewarm_one(idx, client, msg_ids):
    try:
        st = streamer_for(client)
        for mid in msg_ids:
            try:
                fid = await st.get_file_properties(mid)
                await st.generate_media_session(client, fid)
                prewarmed.add(idx)
                mark_client_success(idx)
                logging.info(f"Prewarm: client {idx} media session ready")
                return
            except FloodWait as e:
                await asyncio.sleep(min(e.value, MAX_FLOODWAIT) + 1)
            except Exception:
                continue
        prewarm_tries[idx] = prewarm_tries.get(idx, 0) + 1
        logging.warning(f"Prewarm: client {idx} not ready yet (try {prewarm_tries[idx]}/5)")
    finally:
        prewarm_busy.discard(idx)


async def prewarm_media_sessions():
    await asyncio.sleep(15)  # ক্লায়েন্টগুলো পুরোপুরি চালু হোক
    for _round in range(20):
        try:
            if prewarmed.issuperset(multi_clients.keys()):
                logging.info(f"Prewarm complete: all {len(multi_clients)} clients ready")
                return
            msg_ids = await _pick_recent_file_ids()
            if not msg_ids:
                await asyncio.sleep(120)
                continue
            for idx, client in list(multi_clients.items()):
                if idx in prewarmed or idx in prewarm_busy:
                    continue
                if prewarm_tries.get(idx, 0) >= 5:
                    continue
                prewarm_busy.add(idx)
                asyncio.create_task(_prewarm_one(idx, client, msg_ids))
        except Exception as e:
            logging.warning(f"Prewarm round failed: {e}")
        await asyncio.sleep(60)
