import os
import re
import html
import json
import jinja2
import logging
import aiohttp
import urllib.parse
import aiofiles

from info import *
from utils import temp
from web.bot import WebavBot
from utils import humanbytes
from web.utils.file_properties import get_file_ids, get_media_from_message
from web.server.exceptions import InvalidHash


def _clean_caption_name(caption: str) -> str:
    """ইউজারের দেওয়া নাম (ক্যাপশনের প্রথম লাইন) পরিষ্কার করে"""
    name = (caption or "").strip().split("\n")[0].strip()
    # Telegram markdown চিহ্নগুলো সরাও, কিন্তু ( ) - রাখো — মুভির নামে লাগে
    name = re.sub(r"[*_`~\[\]<>]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _url_safe_name(display_name: str, real_name: str, fallback_id: int) -> str:
    """লিংকে যাওয়ার নাম: ইউজারের নাম + আসল এক্সটেনশন (খারাপ ক্যারেক্টার বাদ)"""
    ext = os.path.splitext(real_name or "")[1].lower()
    if not re.match(r"^\.(mkv|mp4|avi|webm|mov|m4v|ts|flv|wmv|mp3|m4a|flac|ogg|opus|wav)$", ext):
        ext = ".mkv"
    base = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", display_name or "").strip()
    base = re.sub(r"\.(mkv|mp4|avi|webm|mov|m4v|ts|flv|wmv|mp3|m4a|flac|ogg|opus|wav)$", "", base, flags=re.I)
    base = re.sub(r"\s+", " ", base)[:180].strip()
    if not base:
        base = f"File_{fallback_id}"
    return urllib.parse.quote(f"{base}{ext}", safe="()")


async def render_page(id, secure_hash, src=None):
    file = await WebavBot.get_messages(int(BIN_CHANNEL), int(id))
    file_data = await get_file_ids(WebavBot, int(BIN_CHANNEL), int(id))

    if file_data.unique_id[:6] != secure_hash:
        logging.debug(f"link hash: {secure_hash} - {file_data.unique_id[:6]}")
        logging.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash

    media = get_media_from_message(file)

    # --- নাম নির্ধারণ ---
    # ⚡ ইউজার মুভি স্ক্যান করার সময় যে নাম লিখে (ক্যাপশন), সেটাই মূল নাম হবে।
    # ক্যাপশন না থাকলে আসল ফাইলের টেকনিক্যাল নাম (পুরনো নিয়ম)।
    raw_file_name = file_data.file_name if file_data.file_name else f"File_{id}"
    display_name = _clean_caption_name(getattr(file, "caption", None) or "")
    if not display_name:
        display_name = raw_file_name.replace("_", " ").replace(".", " ").strip()
        display_name = re.sub(r"\.(mkv|mp4|avi|webm|mov|m4v)$", "", display_name, flags=re.I)
        display_name = re.sub(r"\s+", " ", display_name).strip() or f"File_{id}"

    # ভিডিওর আসল সোর্স URL: ইউজারের নাম + আসল এক্সটেনশন (প্লেয়ারে কোনো সমস্যা নেই,
    # id + hash দিয়েই ফাইল চেনা হয়, নামটা শুধু দেখার জন্য)
    url_name = _url_safe_name(display_name, raw_file_name, id)
    src = urllib.parse.urljoin(
        URL,
        f"{id}/{url_name}?hash={secure_hash}",
    )

    # --- 🖼️ মুভির ছবি (টেলিগ্রাম নিজেই প্রতিটি ভিডিওর স্ক্রিনশট বানায়) ---
    # এটা ভিডিও ডিকোড ছাড়াই সাথে সাথে দেখানো যায় — তাই HEVC 10-bit ফাইলেও
    # পোস্টার আর কালো স্ক্রিন সমস্যা থাকবে না
    has_thumb = bool(getattr(media, "thumbs", None))
    thumb_url = ""
    if has_thumb:
        thumb_url = urllib.parse.urljoin(
            URL,
            f"thumb/{id}/{url_name}?hash={secure_hash}",
        )

    tag = file_data.mime_type.split("/")[0].strip() if file_data.mime_type else "video"
    file_size = humanbytes(file_data.file_size)

    if tag in ["video", "audio"]:
        template_file = "web/template/webav.html"
    else:
        template_file = "web/template/dl.html"
        async with aiohttp.ClientSession() as s:
            async with s.get(src) as u:
                content_len = u.headers.get("Content-Length")
                if content_len:
                    file_size = humanbytes(int(content_len))

    async with aiofiles.open(template_file, mode='r') as f:
        template_content = await f.read()
        template = jinja2.Template(template_content)

    file_get_link = f"https://t.me/{temp.U_NAME}?start=file_{id}"

    # HTML-এ বসানোর নাম escape করা (ক্যাপশনে ' " < > থাকলেও পেজ ভাঙবে না)
    safe_html_name = html.escape(display_name, quote=True)
    # JS-এর ভেতরে বসানোর নাম JSON-safe (parseFileInfo ইত্যাদি জায়গায়)
    safe_js_name = json.dumps(display_name, ensure_ascii=False).replace("</", "<\\/")

    return template.render(
        file_name=safe_html_name,
        file_name_js=safe_js_name,
        file_url=src,
        file_size=file_size,
        file_get=file_get_link,
        file_unique_id=file_data.unique_id,
        thumb_url=thumb_url,
    )
