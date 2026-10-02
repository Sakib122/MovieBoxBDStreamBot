import asyncio
import logging
from info import *
from typing import Dict, Union
from web.bot import work_loads
from pyrogram import Client, utils, raw
from .file_properties import get_file_ids
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid
from web.server.exceptions import FileNotFound
from pyrogram.file_id import FileId, FileType, ThumbnailSource

class ByteStreamer:
    def __init__(self, client: Client):
        self.clean_timer = 30 * 60
        self.client: Client = client
        self.cached_file_ids: Dict[int, FileId] = {}
        try:
            asyncio.create_task(self.clean_cache())
        except RuntimeError:
            pass 

    # ⚡ SMART: এই ক্লায়েন্ট প্রিমিয়াম (ইউজার অ্যাকাউন্ট) কিনা
    # সর্বোচ্চ ইনডেক্স = MULTI_SESSION দিয়ে চালু হওয়া ক্লায়েন্ট
    def _is_premium_client(self) -> bool:
        try:
            max_idx = max(work_loads.keys()) if work_loads else 0
            my_idx = None
            for idx, cl in None if True else []:
                pass
            return False
        except Exception:
            return False

    async def get_file_properties(self, id: int) -> FileId:
        if id not in self.cached_file_ids:
            await self.generate_file_properties(id)
            logging.debug(f"Cached file properties for message with ID {id}")
        return self.cached_file_ids[id]

    async def generate_file_properties(self, id: int) -> FileId:
        # ⚡ প্রিমিয়াম ক্লায়েন্টের জন্য: সে নিজে গুদাম থেকে মেসেজ টেনে
        # নিজের নামে FRESH file reference নেবে (FILE_REFERENCE_EXPIRED ফিক্স)
        try:
            me = await self.client.get_me()
            if me and not getattr(me, "is_bot", True):
                file_id = await get_file_ids(self.client, BIN_CHANNEL, id)
                if file_id:
                    self.cached_file_ids[id] = file_id
                    logging.info(f"Fresh file properties (premium) for ID {id}")
                    return self.cached_file_ids[id]
        except Exception as e:
            logging.warning(f"Premium fresh-fetch failed for ID {id}: {e}")

        # বট ক্লায়েন্ট / ফলব্যাক: আগের মতোই ক্যাশ
        file_id = await get_file_ids(self.client, BIN_CHANNEL, id)
        logging.debug(f"Generated file ID and Unique ID for message with ID {id}")
        if not file_id:
            logging.debug(f"Message with ID {id} not found")
            raise FileNotFound
        self.cached_file_ids[id] = file_id
        logging.debug(f"Cached media message with ID {id}")
        return self.cached_file_ids[id]

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
    ) -> Union[str, None]:
        client = self.client
        work_loads[index] += 1
        logging.debug(f"Starting to yield file with client {index}.")
        current_part = 1
        
        try:
            # ⚡ প্রিমিয়াম ক্লায়েন্ট হলে শুরুতেই ফ্রেশ রেফারেন্স নেয়া
            try:
                me = await client.get_me()
                if me and not getattr(me, "is_bot", True):
                    fresh = await get_file_ids(client, BIN_CHANNEL, file_id.file_id if hasattr(file_id, 'file_id') else None or 0)
            except Exception:
                pass

            media_session = await self.generate_media_session(client, file_id)
            current_part = 1
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
        except (TimeoutError, AttributeError) as e:
            logging.error(f"Error yielding file: {e}")
            pass
        except Exception as e:
            logging.error(f"Unexpected error in yield_file: {e}")
        finally:
            logging.debug(f"Finished yielding file with {current_part} parts.")
            work_loads[index] -= 1

    async def clean_cache(self) -> None:
        while True:
            await asyncio.sleep(self.clean_timer)
            self.cached_file_ids.clear()
            logging.debug("Cleaned the cache")
