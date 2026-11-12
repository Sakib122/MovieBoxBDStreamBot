import sys
import glob
import importlib
import asyncio
import logging
import logging.config
import traceback
from pathlib import Path
from datetime import date, datetime
import pytz
from aiohttp import web
from pyrogram import idle, __version__
from pyrogram.raw.all import layer
import pyrogram.utils
from pyrogram.types import Message
from info import *
from utils import temp, ping_server
from Script import script
from web import web_server, check_expired_premium
from web.bot import WebavBot
from web.bot.clients import initialize_clients
from web.server.support_logger import setup_support_logger

logger = setup_support_logger()

# ===============================
# GLOBAL EXCEPTION CATCH
# ===============================
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.critical(
        "UNHANDLED EXCEPTION",
        exc_info=(exc_type, exc_value, exc_traceback)
    )

sys.excepthook = handle_exception

# Hack for Channel ID limits
pyrogram.utils.MIN_CHANNEL_ID = -1009147483647

import session_patch   # ⚡ CPU-100% ফিক্স — PyroFork Session churn patch

# ===============================
# Logging Config
# ===============================
try:
    logging.config.fileConfig("logging.conf")
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)

# Plugin Path
ppath = "plugins/*.py"
files = glob.glob(ppath)

# ===============================
# MAIN START FUNCTION
# ===============================
async def Webav_start():
    print("\n")
    print("Credit - Telegram @FlixBoxsStreamBot")

    try:
        # 1. Initialize additional clients
        await initialize_clients()

        # 2. Start the main bot
        print("Starting WebavBot...")
        await WebavBot.start()

        bot_info = await WebavBot.get_me()
        WebavBot.username = bot_info.username

        # 3. Import Plugins
        print("Importing Plugins...")
        for name in files:
            try:
                patt = Path(name)
                plugin_name = patt.stem
                import_path = f"plugins.{plugin_name}"

                spec = importlib.util.spec_from_file_location(import_path, patt)
                if spec and spec.loader:
                    load = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(load)
                    sys.modules["plugins." + plugin_name] = load
                    print(f"✅ Imported => {plugin_name}")
            except Exception:
                logger.error("PLUGIN IMPORT FAILED", exc_info=True)

        # 4. Heroku Ping
        if ON_HEROKU:
            asyncio.create_task(ping_server())

        # 5. Set Global Info
        me = await WebavBot.get_me()
        temp.ME = me.id
        temp.U_NAME = me.username
        temp.B_NAME = me.first_name
        temp.B_LINK = me.mention
        WebavBot.username = "@" + me.username

        logging.info(
            f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on {me.username}."
        )

        # 6. Date / Time
        tz = pytz.timezone("Asia/Kolkata")
        today = date.today()
        now = datetime.now(tz)
        time = now.strftime("%H:%M:%S %p")

        # 7. Background Tasks
        asyncio.create_task(check_expired_premium(WebavBot))

        # 8. Startup Messages
        try:
            await WebavBot.send_message(
                chat_id=LOG_CHANNEL,
                text=script.RESTART_TXT.format(today, time)
            )
        except Exception:
            logger.error("LOG CHANNEL ERROR", exc_info=True)

        if SUPPORT_GROUP:
            try:
                await WebavBot.send_message(
                    chat_id=SUPPORT_GROUP,
                    text=f"<b>{me.mention} ʀᴇsᴛᴀʀᴛᴇᴅ 🤖</b>"
                )
            except Exception:
                logger.error("SUPPORT GROUP ERROR", exc_info=True)

        if ADMINS:
            try:
                await WebavBot.send_message(
                    chat_id=ADMINS[0],
                    text="<b>ʙᴏᴛ ʀᴇsᴛᴀʀᴛᴇᴅ !!</b>"
                )
            except Exception:
                pass

        # 9. Start Web Server
        # 🔧 FIX: handler_cancellation=True — ইউজার স্ট্রিম ছেড়ে চলে গেলে
        # (সিক দিলে/ট্যাব বন্ধ করলে) পুরনো রিকোয়েস্ট সাথে সাথে বন্ধ হয়।
        # আগে ওগুলো জম্বি হয়ে 1GB পর্যন্ত টেনে নিত — কাউন্টার আটকে থাকত,
        # সার্ভারের ব্যান্ডউইডথ শেষ হয়ে অন্য স্ট্রিম ঝুলে যেত।
        app = web.AppRunner(await web_server(), handler_cancellation=True)
        await app.setup()
        bind_address = "0.0.0.0"
        await web.TCPSite(app, bind_address, PORT).start()
        logging.info(f"Web Server Started on Port {PORT}")

        # 10. Idle
        await idle()

    except Exception:
        logger.critical("STARTUP SEQUENCE FAILED", exc_info=True)
        sys.exit(1)

# ===============================
# ENTRY POINT
# ===============================
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(Webav_start())
    except KeyboardInterrupt:
        logging.info("Service Stopped Bye 👋")
    except Exception:
        logger.critical("CRITICAL RUNTIME ERROR", exc_info=True)
