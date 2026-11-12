import time
import asyncio
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
STREAM_FIRST_CHUNK_TIMEOUT = 20   # প্রথম বাইট এত সেকেন্ডে আসতেই হবে
STREAM_IDLE_TIMEOUT = 25          # মাঝপথে দুই chunk-এর মাঝে সর্বোচ্চ চুপ
MAX_FLOODWAIT = 30                # এর চেয়ে বড় FloodWait হলে অপেক্ষা নয়, ছেড়ে দাও
CLIENT_COOLDOWN = 120             # বারবার আটকানো ক্লায়েন্ট এত সেকেন্ড বাদ থাকবে
FAILS_TO_COOLDOWN = 2             # টানা এতগুলো ব্যর্থতায় বেঞ্চ হবে


class UpstreamTimeout(Exception):
    """Telegram থেকে অনেকক্ষণ কোনো data আসেনি — স্ট্রিম বাতিল"""


client_health: dict = {}   # client index -> {"fails": int, "cooldown_until": float}
prewarmed: set = set()     # যাদের media session আগেই তৈরি হয়ে গেছে
prewarm_busy: set = set()
prewarm_tries: dict = {}


def _cooling(index, now=None):
    h = client_health.get(index)
    return bool(h and (now or time.time()) < h["cooldown_until"])


def mark_client_failure(index):
    h = client_health.setdefault(index, {"fails": 0, "cooldown_until": 0.0})
    h["fails"] += 1
    if h["fails"] >= FAILS_TO_COOLDOWN:
        h["cooldown_until"] = time.time() + CLIENT_COOLDOWN
        h["fails"] = 0
        logging.warning(f"Client {index} benched for {CLIENT_COOLDOWN}s after repeated stalls")


def mark_client_success(index):
    client_health[index] = {"fails": 0, "cooldown_until": 0.0}


# ---------------------------------------------------------------
# ⚡⚡ GATE SYSTEM — এক ক্লায়েন্টে অনিয়ন্ত্রিত সমান্তরাল স্ট্রিম ঢুকলে
# pyrogram-এর একটাই TCP connection ভেঙে পড়ে ('Connection lost' →
# session restart race → 504 ঝড় → player আরও রিট্রাই → মৃত্যু-চক্র)।
# তাই প্রতি ক্লায়েন্টের সামনে গেট — সর্বোচ্চ ৮টি লাইভ স্ট্রিম।
# ---------------------------------------------------------------
client_gates: dict = {}   # client index -> BoundedSemaphore
STREAM_GATE_LIMIT = 8     # প্রতি ক্লায়েন্টে একসাথে সর্বোচ্চ কতগুলো লাইভ স্ট্রিম
STREAM_GATE_WAIT = 12     # গেট ফাঁকা হওয়ার অপেক্ষা (সেকেন্ড), এর পরে 504
OVERFLOW_GATE_WAIT = 1    # ওভারফ্লোতে প্রতি অন্য ক্লায়েন্টে এত সেকেন্ড অপেক্ষা


def _gate(index):
    g = client_gates.get(index)
    if g is None:
        g = asyncio.BoundedSemaphore(STREAM_GATE_LIMIT)
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


def pick_client_index():
    """লোড সবচেয়ে কম এমন সুস্থ ক্লায়েন্ট — বেঞ্চড (cooling) ক্লায়েন্ট এড়িয়ে"""
    now = time.time()
    healthy = [i for i in work_loads.keys() if not _cooling(i, now)]
    pool = healthy if healthy else list(work_loads.keys())
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


async def guarded_stream(src_it, first_chunk, index, idle_timeout=None):
    """⚡ প্রথম chunk পাওয়ার পর প্রতিটি chunk-কে টাইমারের সাথে race করানো হয়।
    Telegram চুপ হলে অনন্ত অপেক্ষা নয় — স্ট্রিম ছেড়ে দেওয়া হয়, ফলে
    connection + load counter সাথে সাথে খালি হয়ে যায় এবং player নিজে
    শেষ buffered পয়েন্ট থেকে আবার চেষ্টা করে।
    গুরুত্বপূর্ণ: এখানে কখনোই pyrogram task-কে সাথে সাথে cancel করার
    ঝুঁকি নেওয়া হয় না — শুধু নিজের দিক থেকে হাল ছাড়া হয়।"""
    if idle_timeout is None:
        idle_timeout = STREAM_IDLE_TIMEOUT
    it = src_it
    released = False
    task = None

    def release():
        nonlocal released
        if not released:
            released = True
            release_load(index)

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
                raise UpstreamTimeout()
            try:
                chunk = task.result()
            except StopAsyncIteration:
                break
            except FloodWait as e:
                mark_client_failure(index)
                logging.warning(f"FloodWait {e.value}s mid-stream on client {index} — abandoned, player will retry")
                raise UpstreamTimeout()
            mark_client_success(index)
            yield chunk
    finally:
        release()
        if task is not None and not task.done():
            task.cancel()
        try:
            await it.aclose()
        except Exception:
            pass


def _busy_504():
    return web.Response(
        status=504,
        text="Upstream busy — auto retrying…",
        headers={"Retry-After": "3"},
    )


async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    # ⚡ STEP 1: ডিফল্ট ক্লায়েন্ট দিয়ে আগে ফাইলের তথ্য (সাইজ জানার জন্য)
    base_client = multi_clients.get(0) or next(iter(multi_clients.values()))
    base_streamer = streamer_for(base_client)

    file_id = await get_props_safe(base_streamer, id)
    file_size = file_id.file_size

    # ⚡ STEP 2: স্মার্ট ক্লায়েন্ট নির্বাচন (সুস্থ + কম লোড প্রাধান্য)
    if file_size > PREMIUM_SIZE_LIMIT and len(multi_clients) > 1:
        premium_idx = max(multi_clients.keys())
        if premium_idx != 0 and not _cooling(premium_idx):
            index = premium_idx
            logging.info(f"Big file ({file_size / 1024 / 1024 / 1024:.2f} GB) -> Premium Client {index}")
        else:
            index = pick_client_index()
    else:
        index = pick_client_index()

    # ⚡ STEP 3: যে ক্লায়েন্ট স্ট্রিম দেবে, তার নিজের streamer + নিজের fresh file reference
    tg_connect = streamer_for(multi_clients[index])
    try:
        file_id = await get_props_safe(tg_connect, id)
        file_size = file_id.file_size
    except Exception as e:
        # এই ক্লায়েন্ট ফাইলের তথ্যই দিতে পারল না — বেঞ্চ করে অন্য ক্লায়েন্ট দিয়ে একবার চেষ্টা
        mark_client_failure(index)
        logging.warning(f"Client {index} props failed ({e}) — trying another client")
        index = pick_client_index()
        tg_connect = streamer_for(multi_clients[index])
        file_id = await get_props_safe(tg_connect, id)
        file_size = file_id.file_size

    if MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    # ⚡ হ্যাশ চেক
    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    # 🔧 FIX (ডিউরেশন ০০:০০ + সিকে জিরো থেকে প্লে + বড় সিকে ফ্রিজ):
    # ব্রাউজার মুভির ইনডেক্স/ডিউরেশন পড়তে "bytes=-N" (ফাইলের শেষের N বাইট)
    # রিকোয়েস্ট পাঠায়। আগের কোড এতে int('') দিয়ে ক্র্যাশ করত → HTTP 500 →
    # ব্রাউজার ইনডেক্স পায় না → ডিউরেশন ০, সিক দিলে ০ থেকে রিস্টার্ট।
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

    # ⚡ STEP 2.5: GATE — নির্বাচিত ক্লায়েন্টে একসাথে ৮টির বেশি লাইভ স্ট্রিম
    # ঢুকলে session ভেঙে যায়। তাই আগে গেটে জায়গা নিতে হবে; না পেলে
    # অন্য সুস্থ ক্লায়েন্টগুলোতে ওভারফ্লো, সব ভরা হলে ছোট্ট 504 —
    # player নিজে থেকেই আবার চেষ্টা করবে। গেট হারানো = মৃত্যু-চক্র।
    if not await acquire_gate(index, STREAM_GATE_WAIT):
        _now = time.time()
        alts = sorted(
            (i for i in work_loads.keys() if i != index and not _cooling(i, _now)),
            key=lambda i: work_loads.get(i, 0),
        )
        moved = False
        for alt in alts:
            if await acquire_gate(alt, OVERFLOW_GATE_WAIT):
                index = alt
                moved = True
                break
        if moved:
            tg_connect = streamer_for(multi_clients[index])
            file_id = await get_props_safe(tg_connect, id)
            file_size = file_id.file_size
            logging.info(f"Gate full — overflow -> Client {index}")
        else:
            logging.warning(f"All gates full — shedding load (msg {id})")
            return _busy_504()

    # ⚡ load কাউন্টার এখন routes নিজে সামলায় (yield_file থেকে সরানো হয়েছে) —
    # ঝুলে-যাওয়া স্ট্রিমও কাউন্টার আটকে রাখতে পারবে না
    work_loads[index] = work_loads.get(index, 0) + 1
    raw_body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size, id
    )

    # ⚡⚡ সবচেয়ে জরুরি ধাপ: প্রথম chunk হাতে না পেয়ে headers পাঠাই না।
    # তাই স্ট্রিম শুরুতেই আটকালে ইউজার অনন্ত লোডিং দেখবে না — ৩ সেকেন্ডের
    # 504 পাবে, player নিজে থেকেই আবার চেষ্টা করবে (সাধারণত অন্য ক্লায়েন্টে)।
    it = raw_body.__aiter__()
    task = asyncio.ensure_future(it.__anext__())
    done, _ = await asyncio.wait({task}, timeout=STREAM_FIRST_CHUNK_TIMEOUT)
    if not done:
        release_load(index)
        mark_client_failure(index)
        asyncio.create_task(_late_cancel(task))
        logging.error(f"First chunk timeout ({STREAM_FIRST_CHUNK_TIMEOUT}s) on client {index} — 504, player will retry")
        return _busy_504()
    try:
        first_chunk = task.result()
    except StopAsyncIteration:
        first_chunk = b""
    except FloodWait as e:
        release_load(index)
        mark_client_failure(index)
        logging.warning(f"FloodWait {e.value}s at stream start on client {index} — 504, player will retry")
        return _busy_504()
    except Exception:
        release_load(index)
        mark_client_failure(index)
        logging.exception(f"Stream start failed on client {index}")
        return _busy_504()

    if not first_chunk:
        release_load(index)
        mark_client_failure(index)
        logging.error(f"Empty stream start on client {index} (msg {id}) — 504, player will retry")
        return _busy_504()
    mark_client_success(index)

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

    body = guarded_stream(it, first_chunk, index)
    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers=headers,
    )


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
