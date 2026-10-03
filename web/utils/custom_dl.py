import asyncio
import time
import logging
from info import *
from typing import Dict, Union
from web.bot import work_loads
from pyrogram import Client, utils, raw
from .file_properties import get_file_ids
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid, FileReferenceExpired
from web.server.exceptions import FileNotFound
from pyrogram.file_id import FileId, FileType, ThumbnailSource

class ByteStreamer:
    def __init__(self, client: Client):
        self.clean_timer = 30 * 60
        self.client: Client = client
        # ⚡ ক্যাশ এখন (file_id, timestamp) — টিকিট ১০ মিনিটের বেশি পুরনো হলে অটো ফ্রেশ
        self.cached_file_ids: Dict[int, tuple] = {}
        self.ref_ttl = 10 * 60
        self.premium_cache: Dict[int, tuple] = {}
        self.premium_ttl = 5 * 60
        try:
            asyncio.create_task(self.clean_cache())
        except RuntimeError:
            pass 

    async def get_file_properties(self, id: int) -> FileId:
        now = time.time()

        # ⚡ প্রিমিয়াম (ইউজার অ্যাকাউন্ট): নিজের নামে ফ্রেশ টিকিট, ৫ মিনিট ক্যাশ
        try:
            me = await self.client.get_me()
            if me and not getattr(me, "is_bot", True):
                e = self.premium_cache.get(id)
                if e and (now - e[1]) < self.premium_ttl:
                    logging.debug(f"Premium cached reference for ID {id}")
                    return e[0]
                fid = await get_file_ids(self.client, BIN_CHANNEL, id)
                if fid:
                    self.premium_cache[id] = (fid, now)
                    logging.info(f"Fresh file reference (premium) for ID {id}")
                    return fid
        except Exception as e:
            logging.warning(f"Premium fresh-fetch failed for ID {id}: {e}")

        # ⚡ বট ক্লায়েন্ট: ক্যাশ, কিন্তু ১০ মিনিটের পুরনো টিকিট হলে ফ্রেশ নেয়
        e = self.cached_file_ids.get(id)
        if e and (now - e[1]) < self.ref_ttl:
            return e[0]

        fid = await get_file_ids(self.client, BIN_CHANNEL, id)
        if not fid:
            raise FileNotFound
        self.cached_file_ids[id] = (fid, now)
        logging.debug(f"Generated file properties for message with ID {id}")
        return fid

    async def generate_file_properties(self, id: int) -> FileId:
        fid = await get_file_ids(self.client, BIN_CHANNEL, id)
        if not fid:
            raise FileNotFound
        self.cached_file_ids[id] = (fid, time.time())
        return fid

    async def generate_media_session(self, client: Client, file_id: FileId) -> Session:
        media_session = client.media_sessions.get(file_id.dc_id, None)

        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(
                        client, file_id.dc_id, await client.storage.test_mode()
                    ).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

                for _ in range(6):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                    )
                    try:
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logging.warning(f"Invalid authorization bytes for DC {file_id.dc_id}")
                        continue
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
            logging.debug(f"Created media session for DC {file_id.dc_id}")
            client.media_sessions[file_id.dc_id] = media_session
        else:
            logging.debug(f"Using cached media session for DC {file_id.dc_id}")
        return media_session

    @staticmethod
    async def get_location(file_id: FileId):
        file_type = file_id.file_type
        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )
            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return location

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        msg_id: int = None,
    ) -> Union[str, None]:
        client = self.client
        work_loads[index] += 1
        current_part = 1

        try:
            # 🔁 টিকিট মেয়াদ শেষ হলে নিজে নিজে ফ্রেশ নিয়ে ১ বার রিট্রাই
            for attempt in range(2):
                try:
                    media_session = await self.generate_media_session(client, file_id)
                    location = await self.get_location(file_id)

                    r = await media_session.send(
                        raw.functions.upload.GetFile(
                            location=location, offset=offset, limit=chunk_size
                        ),
                    )
                    if isinstance(r, raw.types.upload.File):
                        while True:
                            chunk = r.bytes
                            if not chunk:
                                break
                            elif part_count == 1:
                                yield chunk[first_part_cut:last_part_cut]
                            elif current_part == 1:
                                yield chunk[first_part_cut:]
                            elif current_part == part_count:
                                yield chunk[:last_part_cut]
                            else:
                                yield chunk

                            current_part += 1
                            offset += chunk_size

                            if current_part > part_count:
                                break

                            r = await media_session.send(
                                raw.functions.upload.GetFile(
                                    location=location, offset=offset, limit=chunk_size
                                ),
                            )
                    return
                except FileReferenceExpired:
                    logging.warning(f"FILE_REFERENCE_EXPIRED for msg {msg_id} (attempt {attempt + 1}) — refreshing...")
                    if msg_id and attempt == 0:
                        fid = await get_file_ids(client, BIN_CHANNEL, msg_id)
                        if fid:
                            file_id = fid
                            now = time.time()
                            self.cached_file_ids[msg_id] = (fid, now)
                            self.premium_cache[msg_id] = (fid, now)
                            logging.info(f"Refreshed file reference for msg {msg_id} — retrying stream")
                        continue
                    else:
                        raise
        except (TimeoutError, AttributeError) as e:
            logging.error(f"Error yielding file: {e}")
        except Exception as e:
            logging.error(f"Unexpected error in yield_file: {e}")
        finally:
            work_loads[index] -= 1

    async def clean_cache(self) -> None:
        while True:
            await asyncio.sleep(self.clean_timer)
            now = time.time()
            self.cached_file_ids = {k: v for k, v in self.cached_file_ids.items() if (now - v[1]) < self.ref_ttl}
            self.premium_cache = {k: v for k, v in self.premium_cache.items() if (now - v[1]) < self.premium_ttl}
            logging.debug("Cleaned the cache")
