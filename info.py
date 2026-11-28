import re
import os
import time
import asyncio
import threading
import urllib.request
from datetime import datetime
from os import environ, getenv
from Script import script

# --- Helper Functions ---
def is_enabled(value, default):
    if value.lower() in ["true", "yes", "1", "on"]:
        return True
    elif value.lower() in ["false", "no", "0", "off"]:
        return False
    return default

# =========================================================
# 🤖 BOT INFO & CREDENTIALS
# =========================================================
SESSION = environ.get('SESSION', 'Webavbot')
SESSION1 = environ.get('SESSION1', 'Webavbot')
API_ID = int(environ.get('API_ID', '29904834'))
API_HASH = environ.get('API_HASH', '8b4fd9ef578af114502feeafa2d31938')
BOT_TOKEN = environ.get('BOT_TOKEN', '8714836567:AAEUM36b-_Nri1HFjmAa0Yv1r_A_TPxI0eU')

# Admin Settings
# Fix: re.split allows space and comma separated unlimited IDs
ADMINS = [int(x.strip()) for x in re.split(r'[,\s]+', environ.get('ADMINS', '7120801813')) if x.strip()]
OWNER_USERNAME = environ.get("OWNER_USERNAME", 'ya_movies')

# =========================================================
# 🗄️ DATABASE CONNECTION
# =========================================================
DB_URL = environ.get('DATABASE_URI', "mongodb+srv://Testbot:Testbot@cluster0.5iukc4c.mongodb.net/?appName=Cluster0")
DB_NAME = environ.get('DATABASE_NAME', "FlixBoxsStreamBot")

# =========================================================
# 📢 CHANNELS & LOGS
# =========================================================
# Mandatory Channels
BIN_CHANNEL = int(environ.get("BIN_CHANNEL", '-1004498638459'))
LOG_CHANNEL = int(environ.get("LOG_CHANNEL", '-1004498638459'))

# Feature Specific Logs
PREMIUM_LOGS = int(environ.get("PREMIUM_LOGS", '-1004498638459'))
VERIFIED_LOG = int(environ.get('VERIFIED_LOG', '-1004498638459'))
SUPPORT_GROUP = int(environ.get("SUPPORT_GROUP", "-1004498638459"))

# Auth Channels (Safe Parsing)
auth_channel_str = environ.get("AUTH_CHANNEL", "-1004498638459")
# Fix: Support for multiple auth channels with comma or space
AUTH_CHANNEL = [int(x.strip()) for x in re.split(r'[,\s]+', auth_channel_str) if x.strip()] if auth_channel_str else []

# =========================================================
# 🔗 LINKS & URLS
# =========================================================
CHANNEL = environ.get('CHANNEL', 'https://t.me/flixboxs')
SUPPORT = environ.get('SUPPORT', 'https://t.me/flixboxschat')
TUTORIAL_LINK_1 = environ.get('TUTORIAL_LINK_1', 'https://t.me/flixboxs')
TUTORIAL_LINK_2 = environ.get('TUTORIAL_LINK_2', 'https://t.me/flixboxs')

# =========================================================
# 🔐 VERIFICATION & SHORTENER
# =========================================================
IS_VERIFY = is_enabled(environ.get("IS_VERIFY", "False"), True)
IS_SECOND_VERIFY = is_enabled(environ.get("IS_SECOND_VERIFY", "True"), True)
IS_SHORTLINK = is_enabled(environ.get('IS_SHORTLINK', "True"), True)

# Verification Config
VERIFY_EXPIRE = int(environ.get('VERIFY_EXPIRE', 60)) # In Minutes/Hours based on logic
SHORTLINK_URL = environ.get('SHORTLINK_URL', 'urlbotsot.vercel.app')
SHORTLINK_API = environ.get('SHORTLINK_API', 'akashdeveloper')

# Second Verification Config
SHORTLINK_WEBSITE2 = environ.get("SHORTENER_WEBSITE2", "urlbotsot.vercel.app")
SHORTLINK_API2 = environ.get("SHORTENER_API2", "akashdeveloper")

# =========================================================
# ⚙️ SETTINGS & LIMITS
# =========================================================
FSUB = is_enabled(environ.get("FSUB", "True"), True)
ENABLE_LIMIT = is_enabled(environ.get("ENABLE_LIMIT", "True"), True)
MAINTENANCE_MODE = is_enabled(environ.get("MAINTENANCE_MODE", "False"), False)

# Time & Rate Limits
TIMEZONE = environ.get("TIMEZONE", "Asia/Kolkata")
PING_INTERVAL = int(environ.get("PING_INTERVAL", "1200"))
SLEEP_THRESHOLD = int(getenv('SLEEP_THRESHOLD', '60'))
RATE_LIMIT_TIMEOUT = int(environ.get("RATE_LIMIT_TIMEOUT", "600"))

# File Limits
MAX_FILES = int(environ.get("MAX_FILES", "50"))
BATCH_LIMIT = int(environ.get('BATCH_LIMIT', 60))

# 🗑️ AUTO DELETE SETTINGS
AUTO_DELETE = is_enabled(environ.get("AUTO_DELETE", "True"), True)
AUTO_DELETE_TIME = int(environ.get("AUTO_DELETE_TIME", "60")) # ১০ মিনিটের জন্য ৬০০ সেকেন্ড দিন

# 🔄 KEEP ALIVE / UPTIME SETTINGS
AUTO_KEEP_ALIVE = is_enabled(environ.get("AUTO_KEEP_ALIVE", "True"), True)

# 🛡️ SELF-HEAL SETTINGS (অটো-সেলফ-হিলিং সিস্টেম)
# SELFHEAL_MEM_LIMIT: RAM এত % ছুঁলে সিস্টেম জেগে ওঠে (ডিফল্ট 90)।
#   ⚠️ রিলোডের একমাত্র ট্রিগার মেমোরি — CPU 100% হলেও কিছু হবে না।
#   ⚠️ গুরুত্বপূর্ণ: Koyeb-এর Settings → Environment Variables-এ যদি
#     SELFHEAL_MEM_LIMIT থাকে, সেটাই কোডের ডিফল্টকে ওভাররাইড করে!
# SELFHEAL_CACHE_INTERVAL: কত সেকেন্ড পর পর সাইলেন্ট ক্যাশ-ক্লিন হবে
#   (ডিফল্ট 900 = ১৫ মিনিট)। ক্লিনের সময় বট বন্ধ হয় না, লগ চ্যানেলে
#   কোনো মেসেজ যায় না (এটা সম্পূর্ণ সাইলেন্ট)।
SELFHEAL_ENABLED = is_enabled(environ.get("SELFHEAL_ENABLED", "True"), True)
SELFHEAL_MEM_LIMIT = int(environ.get("SELFHEAL_MEM_LIMIT", "90"))
SELFHEAL_CACHE_INTERVAL = int(environ.get("SELFHEAL_CACHE_INTERVAL", "900"))
SELFHEAL_CHECK_INTERVAL = int(environ.get("SELFHEAL_CHECK_INTERVAL", "5"))

# 🔄 ESCALATION (৫ বার ক্লিন → ছয়বারে Redeploy):
#   RAM টার্গেট ছুঁলে = ১টি ক্রসিং। ১ম-৫ম ক্রসিং → পূর্ণ মেমোরি ক্লিন+রিসেট
#   (লগ চ্যানেলে রিপোর্ট যাবে)। ৬ষ্ঠ ক্রসিং → নিজে নিজে Koyeb Redeploy।
#   SELFHEAL_REARM_SECONDS: RAM টানা উপরে থাকলে পরের প্রচেষ্টার আগে কত সেকেন্ড
#   অপেক্ষা (ক্লিনের ফল দেখার সময়)।
#   SELFHEAL_CLEAN_RESET_SECONDS: RAM টানা এত সেকেন্ড স্বাভাবিক থাকলে ক্লিন-গণনা
#   আবার ০ থেকে শুরু হয় (পুরনো ঘটনা জমে ভুল redeploy হওয়া আটকায়)।
SELFHEAL_MAX_CLEANS = int(environ.get("SELFHEAL_MAX_CLEANS", "5"))
SELFHEAL_REARM_SECONDS = int(environ.get("SELFHEAL_REARM_SECONDS", "120"))
SELFHEAL_CLEAN_RESET_SECONDS = int(environ.get("SELFHEAL_CLEAN_RESET_SECONDS", "900"))

# 🚀 KOYEB AUTO-REDEPLOY (৫ বার ক্লিনের পরও RAM ফুল হলে নিজে নিজে Redeploy)
# পূর্ণ অটো-রিডিপ্লয় চালু করতে:
#   1. Koyeb → Account Settings → API → Create API token
#   2. Koyeb-এ এই env ভ্যারিয়েবলগুলো যোগ করো:
#      KOYEB_API_TOKEN = <তোমার টোকেন>
#      KOYEB_SERVICE_NAME = <ড্যাশবোর্ডে সার্ভিসের নাম, যেমন: FlixBoxsStreamBot>
# টোকেন না দিলেও সিস্টেম কাজ করবে — তখন RAM 95%-এ fresh restart হবে
# (মেমোরি জিরো হবে), কিন্তু পূর্ণ "Redeploy" হবে না।
KOYEB_API_TOKEN = environ.get('KOYEB_API_TOKEN', '')
KOYEB_SERVICE_NAME = environ.get('KOYEB_SERVICE_NAME', 'FlixBoxsStreamBot')
# 🚀 KOYEB_SERVICE_ID (ঐচ্ছিক): সার্ভিসের id সরাসরি জানা থাকলে বসাও —
# না দিলেও সমস্যা নেই, সিস্টেম নাম/বানান-মিল/ডোমেইন দিয়ে নিজে খুঁজে নেবে।
KOYEB_SERVICE_ID = environ.get('KOYEB_SERVICE_ID', '')

# 🧹 LOG CHANNEL AUTO-CLEAN (লগ চ্যানেল সবসময় পরিষ্কার রাখা)
# SELFHEAL_LOG_CLEAN_INTERVAL: কত সেকেন্ড পরপর লগ চ্যানেলের মেসেজ
#   অটো-ডিলিট হবে (ডিফল্ট 300 = ৫ মিনিট)।
# ⚠️ সেফটি: BIN_CHANNEL আর LOG_CHANNEL একই চ্যানেল হলে সিস্টেম শুধু
#   টেক্সট/স্ট্যাটাস মেসেজ মুছবে — ভিডিও/ফাইল মেসেজ স্পর্শ করবে না
#   (ওগুলোই স্ট্রিম লিঙ্কের উৎস; মুছলে পুরনো লিঙ্ক মরে যায়)।
#   ফাইলও অটো-মুছতে চাইলে BIN_CHANNEL আলাদা প্রাইভেট চ্যানেল করে দাও।
# SELFHEAL_LOG_CLEAN_KEEP_PINNED: পিন করা মেসেজ বাঁচবে কি না (ডিফল্ট বাঁচবে)।
SELFHEAL_LOG_CLEAN_ENABLED = is_enabled(environ.get("SELFHEAL_LOG_CLEAN_ENABLED", "True"), True)
SELFHEAL_LOG_CLEAN_INTERVAL = int(environ.get("SELFHEAL_LOG_CLEAN_INTERVAL", "300"))
SELFHEAL_LOG_CLEAN_KEEP_PINNED = is_enabled(environ.get("SELFHEAL_LOG_CLEAN_KEEP_PINNED", "True"), True)

# =========================================================
# 🖼️ MEDIA & CAPTIONS
# =========================================================
QR_CODE = environ.get('QR_CODE', 'https://graph.org/file/6afb4093d5ec5c4176979.jpg')
VERIFY_IMG = environ.get("VERIFY_IMG", "https://graph.org/file/1669ab9af68eaa62c3ca4.jpg")
AUTH_PICS = environ.get('AUTH_PICS', 'https://envs.sh/AwV.jpg')
PICS = environ.get('PICS', 'https://ibb.co/VpTJNNCN')
FILE_PIC = environ.get('FILE_PIC', 'https://i.postimg.cc/0xKQbfbs/IMG-20260624-040804-844.jpg')

FILE_CAPTION = environ.get('FILE_CAPTION', script.CAPTION)

# =========================================================
# 🌐 SERVER & APP CONFIG
# =========================================================
WORKERS = int(getenv('WORKERS', '50004'))
MULTI_CLIENT = False
name = str(environ.get('name', 'avbotz'))

# Heroku & Port Config
if 'DYNO' in environ:
    ON_HEROKU = True
    APP_NAME = str(getenv('APP_NAME'))
else:
    ON_HEROKU = False
    APP_NAME = None

PORT = int(getenv('PORT', '2626'))
NO_PORT = is_enabled(getenv("NO_PORT", "True"), False)
HAS_SSL = is_enabled(getenv("HAS_SSL", "False"), False)
BIND_ADDRESS = getenv("WEB_SERVER_BIND_ADDRESS", "127.0.0.1")

# URL Generation
custom_url = environ.get("URL")
if custom_url:
    URL = custom_url
else:
    FQDN = getenv("FQDN", BIND_ADDRESS)
    PROTOCOL = "http" if HAS_SSL else "https"
    PORT_SEGMENT = "" if NO_PORT else f":{PORT}"
    URL = f"{PROTOCOL}://{FQDN}{PORT_SEGMENT}/"

# Default fallback if nothing works
if not URL or URL == "/" or URL.startswith("https://127.0.0.1"):
    URL = "https://forward-jolyn-vnnmbs-62200c9e.koyeb.app/"

# =========================================================
# 🚀 AUTO UPTIME / KEEP-ALIVE LOGIC
# =========================================================
def ping_server():
    while True:
        try:
            urllib.request.urlopen(URL)
            print(f"Ping successful to: {URL}")
        except Exception as e:
            print(f"Ping failed: {e}")
        time.sleep(PING_INTERVAL)

if AUTO_KEEP_ALIVE:
    threading.Thread(target=ping_server, daemon=True).start()

# =========================================================
# 🗑️ UNIVERSAL AUTO-DELETE ENGINE (FOR ALL MEDIA TYPES)
# =========================================================
async def auto_delete_message(message, delay=None):
    """
    এটি ভিডিও, অডিও, ফটো, এপিকে, টেক্সট বা লিঙ্ক—যেকোনো মেসেজ
    নির্ধারিত সময় পর অটো ডিলিট করবে।
    """
    if not AUTO_DELETE:
        return
    
    # সময় নির্ধারণ (যদি আলাদা সময় না থাকে তবে ডিফল্ট নেবে)
    wait_time = delay if delay is not None else AUTO_DELETE_TIME
    
    await asyncio.sleep(wait_time)
    
    try:
        # মেইন মেসেজ ডিলিট করবে
        await message.delete()
        
        # যদি ওই মেসেজের সাথে কোনো ফাইল রিপ্লাই হিসেবে থাকে সেটিও ডিলিট করবে
        if hasattr(message, 'reply_to_message') and message.reply_to_message:
            try:
                await message.reply_to_message.delete()
            except:
                pass
    except Exception:
        # মেসেজ ডিলিট করতে না পারলে (যদি অলরেডি ডিলিট থাকে) স্কিপ করবে
        pass
