import os
import re
from urllib.parse import quote

from pyrogram import Client
from typing import Any, Optional
from pyrogram.types import Message
from pyrogram.file_id import FileId
from pyrogram.raw.types.messages import Messages
from web.server.exceptions import FileNotFound

# ⚡ সুন্দর নাম হেল্পার: ইউজারের দেওয়া নাম (ক্যাপশন) আগে, নইলে আসল ফাইলের নাম
_MEDIA_EXT_RE = r"\.(mkv|mp4|avi|webm|mov|m4v|ts|flv|wmv|mp3|m4a|flac|ogg|opus|wav)$"


def display_name_from(caption: str, real_name: str) -> str:
    name = ""
    if caption:
        name = caption.strip().split("\n")[0].strip()
        name = re.sub(r"[*_`~\[\]<>]", "", name)
        name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = real_name or ""
    return name[:190].strip() or "Unknown File"


def url_name_from(display: str, real_name: str, fallback_id: int) -> str:
    """লিংকে যাওয়ার নাম: সুন্দর নাম + আসল এক্সটেনশন (খারাপ ক্যারেক্টার বাদ)"""
    ext = os.path.splitext(real_name or "")[1].lower()
    if not re.match(_MEDIA_EXT_RE, ext):
        ext = ".mkv"
    base = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", display or "").strip()
    base = re.sub(_MEDIA_EXT_RE, "", base, flags=re.I)  # নামে ইতিমধ্যে এক্সটেনশন থাকলে দ্বিগুণ হবে না
    base = re.sub(r"\s+", " ", base)[:180].strip()
    if not base:
        base = f"File_{fallback_id}"
    return quote(f"{base}{ext}", safe="()")


async def parse_file_id(message: "Message") -> Optional[FileId]:
    media = get_media_from_message(message)
    if media:
        return FileId.decode(media.file_id)

async def parse_file_unique_id(message: "Messages") -> Optional[str]:
    media = get_media_from_message(message)
    if media:
        return media.file_unique_id

async def get_file_ids(client: Client, chat_id: int, id: int) -> Optional[FileId]:
    message = await client.get_messages(chat_id, id)
    if message.empty:
        raise FileNotFound
    media = get_media_from_message(message)
    file_unique_id = await parse_file_unique_id(message)
    file_id = await parse_file_id(message)
    setattr(file_id, "file_size", getattr(media, "file_size", 0))
    setattr(file_id, "mime_type", getattr(media, "mime_type", ""))
    setattr(file_id, "file_name", getattr(media, "file_name", ""))
    setattr(file_id, "unique_id", file_unique_id)
    return file_id

def get_media_from_message(message: "Message") -> Any:
    media_types = (
        "audio",
        "document",
        "photo",
        "sticker",
        "animation",
        "video",
        "voice",
        "video_note",
    )
    for attr in media_types:
        media = getattr(message, attr, None)
        if media:
            return media

def get_hash(media_msg: Message) -> str:
    media = get_media_from_message(media_msg)
    return getattr(media, "file_unique_id", "")[:6]

def get_name(media_msg: Message) -> str:
    media = get_media_from_message(media_msg)
    return getattr(media, "file_name", "")

def get_media_file_size(m):
    media = get_media_from_message(m)
    return getattr(media, "file_size", 0)
