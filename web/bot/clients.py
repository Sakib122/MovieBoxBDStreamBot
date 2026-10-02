import asyncio
import logging
import os
from info import *
from pyrogram import Client
from web.utils.config_parser import TokenParser
from . import multi_clients, work_loads, WebavBot


def parse_sessions():
    """MULTI_SESSION1, MULTI_SESSION2... এনভায়রনমেন্ট থেকে সেশন স্ট্রিং সংগ্রহ"""
    sessions = {}
    for key, val in os.environ.items():
        key = key.strip()
        val = (val or "").strip()
        if key.startswith("MULTI_SESSION") and val:
            try:
                num = int(key.replace("MULTI_SESSION", ""))
                sessions[num] = val
            except ValueError:
                pass
    return dict(sorted(sessions.items()))


async def initialize_clients():
    multi_clients[0] = WebavBot
    work_loads[0] = 0

    all_tokens = TokenParser().parse_from_env()
    all_sessions = parse_sessions()

    if not all_tokens and not all_sessions:
        print("No additional clients found, using default client")
        return

    async def start_bot_client(client_id, token):
        try:
            print(f"Starting - Client {client_id} (Bot)")
            client = await Client(
                name=str(client_id),
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=token,
                sleep_threshold=SLEEP_THRESHOLD,
                no_updates=True,
                in_memory=True,
            ).start()
            work_loads[client_id] = 0
            return client_id, client
        except Exception:
            logging.error(f"Failed starting Bot Client - {client_id}", exc_info=True)
            return None

    async def start_session_client(client_id, session_string):
        try:
            print(f"Starting - Client {client_id} (Premium Session)")
            client = await Client(
                name=str(client_id),
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string,
                sleep_threshold=SLEEP_THRESHOLD,
                no_updates=True,
                in_memory=True,
            ).start()
            work_loads[client_id] = 0
            return client_id, client
        except Exception:
            logging.error(f"Failed starting Session Client - {client_id}", exc_info=True)
            return None

    tasks = []
    next_id = 1
    for _, token in sorted(all_tokens.items()):
        tasks.append(start_bot_client(next_id, token))
        next_id += 1
    for _, sess in sorted(all_sessions.items()):
        tasks.append(start_session_client(next_id, sess))
        next_id += 1

    clients = await asyncio.gather(*tasks)
    clients = [c for c in clients if c]
    multi_clients.update(dict(clients))

    if len(multi_clients) != 1:
        print(f"Multi-Client Mode Enabled ({len(multi_clients)} clients)")
    else:
        print("No additional clients were initialized, using default client")
