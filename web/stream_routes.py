import time
import asyncio  # ✅ FIX 1: এটা ছিল না — এর কারণে watchdog/timeout কাজ করতো না
from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from aiohttp.http_exceptions import BadStatusLine
from pyrogram.errors import FloodWait  # ✅ FIX 2: স্ট্রিমের ভিতরের FloodWait ধরার জন্য
from web.bot import multi_clients, work_loads
from web.server.exceptions import FileNotFound, InvalidHash
from web.utils.custom_dl import ByteStreamer
from web.utils.render_template import render_page
from info import *
from utils import temp, get_readable_time
from web.utils import StartTime, __version__
import aiofiles
import jinja2
from database.users_db import db  # Apni DB import

routes = web.RouteTableDef()

# ⚡ প্রিমিয়াম লিমিট: এর চেয়ে বড় ফাইল প্রিমিয়াম ক্লায়েন্টে যাবে
PREMIUM_SIZE_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB

# ✅ FIX 1 (সবচেয়ে জরুরি): Telegram যদি এত সেকেন্ড কোনো data না পাঠায়,
# স্ট্রিম বাতিল হবে — তাহলে connection খালি হয়ে যাবে এবং
# player (তোমার FIXED প্লেয়ার) নিজে থেকেই আবার চেষ্টা করবে।
# আগে এটা ছিল না — আটকে যাওয়া স্ট্রিম চিরকাল connection দখলে রাখত,
# তাই ২-৩ জনের পর সবাই 0 বাইট পেত (আমার টেস্টে যেটা ধরা পড়েছিল)।
TELEGRAM_IDLE_TIMEOUT = 25  # seconds
MAX_FLOODWAIT = 45          # এর চেয়ে বড় FloodWait হলে অপেক্ষা না করে বাতিল


class UpstreamTimeout(Exception):
    """Telegram থেকে অনেকক্ষণ কোনো data আসেনি — স্ট্রিম বাতিল"""


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
                error="âŒ Wrong Password!",
                title=link_data.get("title", "Protected Link"),
                channel_link=link_data.get("channel_link")
            ),
            content_type="text/html"
        )

@routes.get(r"/watch/{path:\S+}", allow_head=True)
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


@routes.get(r"/{path:\S+}", allow_head=True)
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
    """✅ FIX 2: ফাইলের তথ্য নিতে গিয়ে FloodWait এলে অপেক্ষা করে আবার চেষ্টা করে (আগে 500 error হতো)"""
    for _ in range(3):
        try:
            return await streamer.get_file_properties(message_id)
        except FloodWait as e:
            if e.value > MAX_FLOODWAIT:
                raise
            logging.warning(f"FloodWait {e.value}s in get_file_properties — waiting then retrying")
            await asyncio.sleep(e.value + 1)
    return await streamer.get_file_properties(message_id)


async def guarded_stream(src, idle_timeout=TELEGRAM_IDLE_TIMEOUT):
    """✅ FIX 1 (আসল সমাধান): watchdog দিয়ে স্ট্রিম মোড়ানো।
    Telegram চুপ হয়ে গেলে (কোনো chunk না এলে) স্ট্রিম বাতিল করে
    connection খালি করে দেয় — ফলে পরের ইউজাররা আর আটকায় না।
    ছোটখাটো FloodWait হলে অপেক্ষা করে স্ট্রিম বাঁচিয়ে রাখে।"""
    it = src.__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except (asyncio.TimeoutError, TimeoutError):
                logging.error(
                    f"TG stream idle > {idle_timeout}s — aborting so the player can retry"
                )
                raise UpstreamTimeout()
            except FloodWait as e:
                if e.value > MAX_FLOODWAIT:
                    logging.warning(f"FloodWait {e.value}s — too long, aborting stream")
                    raise UpstreamTimeout()
                logging.warning(f"FloodWait {e.value}s during stream — waiting")
                await asyncio.sleep(e.value)
                continue
            yield chunk
    finally:
        try:
            await it.aclose()
        except Exception:
            pass


def streamer_for(client):
    """✅ FIX 3: প্রতিটি ক্লায়েন্টের নিজের streamer — আগে ভুল ক্লায়েন্টেরটা ব্যবহার হতো"""
    if client not in class_cache:
        class_cache[client] = ByteStreamer(client)
    return class_cache[client]


async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    # ⚡ STEP 1: ডিফল্ট ক্লায়েন্ট দিয়ে আগে ফাইলের তথ্য (সাইজ জানার জন্য)
    base_client = multi_clients.get(0) or next(iter(multi_clients.values()))
    base_streamer = streamer_for(base_client)

    file_id = await get_props_safe(base_streamer, id)
    file_size = file_id.file_size

    # ⚡ STEP 2: স্মার্ট ক্লায়েন্ট নির্বাচন
    if file_size > PREMIUM_SIZE_LIMIT and len(multi_clients) > 1:
        premium_idx = max(multi_clients.keys())
        if premium_idx != 0:
            index = premium_idx
            logging.info(f"Big file ({file_size / 1024 / 1024 / 1024:.2f} GB) -> Premium Client {index}")

            faster_client = multi_clients[index]

            # ⚡⚡ আসল ফিক্স: কাগজটাও প্রিমিয়াম শ্রমিকের কাছ থেকেই নাও!
            # তাহলে সে নিজের নামে FRESH file reference পাবে
            tg_connect = streamer_for(faster_client)
            file_id = await get_props_safe(tg_connect, id)  # fresh reference!
            file_size = file_id.file_size
        else:
            index = min(work_loads, key=work_loads.get)
            faster_client = multi_clients[index]
            # ✅ FIX 3: আগে এখানে ভুল করে base_streamer ব্যবহার হতো
            tg_connect = streamer_for(faster_client)
            file_id = await get_props_safe(tg_connect, id)
            file_size = file_id.file_size
    else:
        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]
        # ✅ FIX 3: যে ক্লায়েন্ট স্ট্রিম দেবে, তার নিজের streamer + নিজের file reference
        tg_connect = streamer_for(faster_client)
        file_id = await get_props_safe(tg_connect, id)
        file_size = file_id.file_size

    if MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    # ⚡ হ্যাশ চেক
    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
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

    raw_body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size, id
    )
    # ✅ FIX 1: watchdog দিয়ে মুড়ানো — আটকে গেলে বাতিল হবে, চিরকাল ঝুলে থাকবে না
    body = guarded_stream(raw_body)

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    # ✅ FIX 4: mimetypes.guess_type() আসলে tuple রিটার্ন করে — আগে
    # Content-Type ভুল হয়ে "('video/x-matroska', None)" যেত, তাই ভিডিও
    # প্লে না হয়ে ডাউনলোড হতো। এখন ঠিকভাবে শুধু টাইপটা নেওয়া হচ্ছে।
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
    }
    # ✅ Content-Range শুধু Range request এ পাঠানো ঠিক
    if range_header:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers=headers,
    )
