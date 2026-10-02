import time
from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from aiohttp.http_exceptions import BadStatusLine
from web.bot import multi_clients, work_loads
from web.server.exceptions import FileNotFound, InvalidHash
from web.utils.custom_dl import ByteStreamer
from web.utils.render_template import render_page
from info import *
from utils import temp, get_readable_time
from web.utils import StartTime, __version__
import aiofiles
import jinja2
from database.users_db import db # Apni DB import

routes = web.RouteTableDef()

# ⚡ প্রিমিয়াম লিমিট: এর চেয়ে বড় ফাইল প্রিমিয়াম ক্লায়েন্টে যাবে
PREMIUM_SIZE_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB

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


async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    # ⚡ STEP 1: ডিফল্ট ক্লায়েন্ট দিয়ে আগে ফাইলের তথ্য (সাইজ জানার জন্য)
    base_client = multi_clients.get(0) or next(iter(multi_clients.values()))
    if base_client in class_cache:
        base_streamer = class_cache[base_client]
    else:
        base_streamer = ByteStreamer(base_client)
        class_cache[base_client] = base_streamer

    file_id = await base_streamer.get_file_properties(id)
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
            if faster_client in class_cache:
                tg_connect = class_cache[faster_client]
            else:
                tg_connect = ByteStreamer(faster_client)
                class_cache[faster_client] = tg_connect

            file_id = await tg_connect.get_file_properties(id)  # fresh reference!
            file_size = file_id.file_size
        else:
            index = min(work_loads, key=work_loads.get)
            faster_client = multi_clients[index]
            tg_connect = base_streamer
    else:
        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]

        if faster_client in class_cache:
            tg_connect = class_cache[faster_client]
        else:
            tg_connect = ByteStreamer(faster_client)
            class_cache[faster_client] = tg_connect

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
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
)
