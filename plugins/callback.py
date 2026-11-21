import os
import random
import asyncio
import time
import re
import pytz
import json
import logging
from Script import script
from database.users_db import db
from pyrogram import Client, filters, enums
from pyrogram.errors import *
from pyrogram.types import Message
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
# info থেকে নতুন ভেরিয়েবলগুলো ইমপোর্ট করা হয়েছে
from info import (
    ADMINS, URL, OWNER_USERNAME, SUPPORT, CHANNEL, BIN_CHANNEL, 
    QR_CODE, FILE_CAPTION, AUTO_DELETE, AUTO_DELETE_TIME
)
from datetime import datetime
from web.utils.file_properties import get_hash
from utils import temp, get_readable_time, get_size
from web.utils import StartTime, __version__

logger = logging.getLogger(__name__)

# --- অটো ডিলিট হেল্পার ফাংশন ---
async def auto_delete_message(message, delay=None):
    if not AUTO_DELETE:
        return
    wait_time = delay if delay is not None else AUTO_DELETE_TIME
    await asyncio.sleep(wait_time)
    try:
        await message.delete()
    except Exception:
        pass

@Client.on_callback_query()
async def cb_handler(client: Client, query: CallbackQuery):
    # 'query.message' None কি না তা চেক করা হচ্ছে এরর এড়াতে
    if not query.message:
        return await query.answer("Message is inaccessible!", show_alert=True)

    if query.data == "close_data":
        try:
            await query.message.delete()
        except Exception:
            await query.answer("Message already deleted.")
            
    elif query.data == "about":
        buttons = [[
            InlineKeyboardButton('💻 sᴏᴜʀᴄᴇ ᴄᴏᴅᴇ', url='https://t.me/flixboxs')
        ],[
            InlineKeyboardButton('• ʜᴏᴍᴇ •', callback_data='start'),
            InlineKeyboardButton('• ᴄʟᴏsᴇ •', callback_data='close_data')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.ABOUT_TXT.format(temp.B_NAME, temp.B_NAME, get_readable_time(time.time() - StartTime), __version__),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            pass
    
    elif query.data == "start":
        buttons = [[
            InlineKeyboardButton('• ᴜᴘᴅᴀᴛᴇᴅ •', url=CHANNEL),
            InlineKeyboardButton('• sᴜᴘᴘᴏʀᴛ •', url=SUPPORT)
        ],[
            InlineKeyboardButton('• ʜᴇʟᴘ •', callback_data='help'),
            InlineKeyboardButton('• ᴀʙᴏᴜᴛ •', callback_data='about')
        ],[
            InlineKeyboardButton('✨ ʙᴜʏ ꜱᴜʙꜱᴄʀɪᴘᴛɪᴏɴ : ʀᴇᴍᴏᴠᴇ ᴀᴅꜱ ✨', callback_data="premium_info")
        ],
            [
                InlineKeyboardButton(
                    '🎁 ʀᴇғᴇʀ & ᴇᴀʀɴ 🎁',
                    callback_data="reffff"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.START_TXT.format(query.from_user.mention, temp.U_NAME),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            pass
        
    elif query.data == "help":
        buttons = [[
            InlineKeyboardButton('• ᴀᴅᴍɪɴ •', callback_data='admincmd')
        ],[
            InlineKeyboardButton('• ʜᴏᴍᴇ •', callback_data='start'),
            InlineKeyboardButton('• ᴄʟᴏsᴇ •', callback_data='close_data')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.HELP_TXT,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )  
        except MessageNotModified:
            pass
        except Exception:
            pass

    elif query.data == "admincmd":
        if not query.from_user.id in ADMINS:
            return await query.answer('This Feature Is Only For Admins !' , show_alert=True)
        buttons = [[
            InlineKeyboardButton('• ʜᴏᴍᴇ •', callback_data='start')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.ADMIN_CMD_TXT,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
           )
        except MessageNotModified:
            pass
        except Exception:
            pass

    elif query.data == "premium_info":
        buttons = [[
            InlineKeyboardButton('🍁 ᴄʟɪᴄᴋ ᴀʟʟ ᴘʟᴀɴꜱ & ᴘʀɪᴄᴇ 🍁', callback_data='check_plan')
        ],[
            InlineKeyboardButton('⋞ ʙᴀᴄᴋ ᴛᴏ ʜᴏᴍᴇ ', callback_data='start')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.PREMIUM_TEXT.format(query.from_user.mention),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    elif query.data == "check_plan":
        buttons = [
            [
                InlineKeyboardButton(
                    "☆📸 ꜱᴇɴᴅ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ 📸☆",
                    url=f"https://t.me/{OWNER_USERNAME}"),
            ],[
                InlineKeyboardButton("• ʙᴀᴄᴋ •", callback_data='premium_info'),
                InlineKeyboardButton("• ᴄʟᴏꜱᴇ •", callback_data="close_data"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.CHECK_PLAN_TXT.format(query.from_user.mention),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    elif query.data == "seeplans":
        btn = [[
            InlineKeyboardButton('🍁 ᴄʟɪᴄᴋ ᴀʟʟ ᴘʟᴀɴꜱ & ᴘʀɪᴄᴇ 🍁', callback_data='check_plan')
        ],[
            InlineKeyboardButton('❌ ᴄʟᴏsᴇ ❌', callback_data='close_data')
        ]]
        reply_markup = InlineKeyboardMarkup(btn)
        try:
            m=await query.message.reply_sticker("CAACAgQAAxkBAAEiLZ9l7VMuTY7QHn4edR6ouHUosQQ9gwACFxIAArzT-FOmYU0gLeJu7x4E") 
            await m.delete()
            await query.message.reply_photo(
                photo=(QR_CODE),
                caption=script.PREMIUM_TEXT.format(query.from_user.mention),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    elif query.data == "reffff":
        user_id = query.from_user.id
        points = await db.get_refer_points(user_id)
        ref_link = f"https://t.me/{temp.U_NAME}?start=reff_{user_id}"
        share_link = f"https://telegram.me/share/url?url={ref_link}&text=Join%20Now%20For%20Movies!"
        buttons = [[
            InlineKeyboardButton("• ɪɴᴠɪᴛᴇ ʟɪɴᴋ •", url=share_link),
            InlineKeyboardButton(f'⏳ {points}', callback_data='ref_point'),
        ],[
            InlineKeyboardButton('⋞ ʙᴀᴄᴋ ᴛᴏ ʜᴏᴍᴇ ', callback_data='start')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        try:
            await query.message.edit_text(
                text=script.REFER_TEXT.format(query.from_user.mention, points, ref_link),
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    elif query.data == "ref_point":
        points = await db.get_refer_points(query.from_user.id)
        await query.answer(f"💰 Your Points: {points}", show_alert=True)
		
    elif query.data.startswith("stream"):
        try:
            msg_id = int(query.data.split('#', 1)[1])
            original_msg = await client.get_messages(int(BIN_CHANNEL), msg_id)
            if not original_msg or original_msg.empty:
                return await query.answer("❌ File not found.", show_alert=True)
            online = f"{URL}watch/{original_msg.id}?hash={get_hash(original_msg)}"
            download = f"{URL}{original_msg.id}?hash={get_hash(original_msg)}"
            btn = [[
                InlineKeyboardButton("ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ", url=online),
                InlineKeyboardButton("ꜰᴀsᴛ ᴅᴏᴡɴʟᴏᴀᴅ", url=download)
            ],[
                InlineKeyboardButton('❌ ᴄʟᴏsᴇ ❌', callback_data='close_data')
            ]]
            await query.edit_message_reply_markup(InlineKeyboardMarkup(btn))
        except MessageNotModified:
            pass
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)

    elif query.data.startswith("filespage_"):
        page = int(query.data.split("_")[1])
        user_id = query.from_user.id      
        files = await db.files.find({"user_id": user_id}).to_list(length=100)
        per_page = 7
        total_pages = (len(files) + per_page - 1) // per_page
        if not files or page < 1 or page > total_pages:
            return await query.answer("⚠️ Nᴏ ᴍᴏʀᴇ ғɪʟᴇꜱ.", show_alert=True)
        start = (page - 1) * per_page
        end = start + per_page
        btns = []
        for f in files[start:end]:
            name = f["file_name"][:40]
            btns.append([InlineKeyboardButton(name, callback_data=f"sendfile_{f['file_id']}")])
        nav_btns = []
        if page > 1:
            nav_btns.append(InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data=f"filespage_{page - 1}"))
        if page < total_pages:
            nav_btns.append(InlineKeyboardButton("➡️ Nᴇxᴛ", callback_data=f"filespage_{page + 1}"))
        nav_btns.append(InlineKeyboardButton("❌ ᴄʟᴏsᴇ ❌", callback_data="close_data"))
        btns.append(nav_btns)
        try:
            await query.message.edit_text(
                f"📁 Tᴏᴛᴀʟ ғɪʟᴇꜱ: {len(files)} | Pᴀɢᴇ {page}/{total_pages}",
                reply_markup=InlineKeyboardMarkup(btns)
            )
        except MessageNotModified:
            pass
        except Exception:
            pass
        return await query.answer()

    elif query.data.startswith("delfilespage_"):
        page = int(query.data.split("_")[1])
        user_id = query.from_user.id      
        files = await db.files.find({"user_id": user_id}).to_list(length=100)
        per_page = 7
        total_pages = (len(files) + per_page - 1) // per_page
        if not files or page < 1 or page > total_pages:
            return await query.answer("⚠️ Nᴏ ᴍᴏʀᴇ ғɪʟᴇꜱ.", show_alert=True)
        start = (page - 1) * per_page
        end = start + per_page
        btns = []
        for f in files[start:end]:
            name = f["file_name"][:40]
            btns.append([InlineKeyboardButton(name, callback_data=f"deletefile_{f['file_id']}")])
        nav_btns = []
        if page > 1:
            nav_btns.append(InlineKeyboardButton("⬅️ Bᴀᴄᴋ", callback_data=f"delfilespage_{page - 1}"))
        if page < total_pages:
            nav_btns.append(InlineKeyboardButton("➡️ Nᴇxᴛ", callback_data=f"delfilespage_{page + 1}"))
        nav_btns.append(InlineKeyboardButton("❌ ᴄʟᴏsᴇ ❌", callback_data="close_data"))
        btns.append(nav_btns)
        try:
            await query.message.edit_text(
                f"📁 Tᴏᴛᴀʟ ғɪʟᴇꜱ: {len(files)} | Pᴀɢᴇ {page}/{total_pages}",
                reply_markup=InlineKeyboardMarkup(btns)
            )
        except MessageNotModified:
            pass
        except Exception:
            pass
        return await query.answer()

    elif query.data.startswith("sendfile_"):
        file_id = int(query.data.split("_")[1])
        user_id = query.from_user.id
        file_data = await db.files.find_one({"file_id": file_id, "user_id": user_id})
        if not file_data:
            return await query.answer("⚠️ Nᴏ ᴍᴏʀᴇ ғɪʟᴇꜱ.", show_alert=True)
        try:
            original_message = await client.get_messages(BIN_CHANNEL, file_id)
            media = original_message.document or original_message.video or original_message.audio
            caption = None
            if media:
                file_name = getattr(media, "file_name", "Unnamed") 
                file_size = get_size(media.file_size)
                caption = FILE_CAPTION.format(CHANNEL, file_name)
            
            # ফাইল পাঠানো হচ্ছে
            sent_msg = await client.copy_message(
                chat_id=user_id,
                from_chat_id=BIN_CHANNEL,
                message_id=file_id,
                caption=caption
            )
            
            # ইউজারকে সতর্কবার্তা দেওয়া
            if AUTO_DELETE:
                await query.answer(f"✅ ফাইল পাঠানো হয়েছে! এটি {AUTO_DELETE_TIME} সেকেন্ড পর অটো ডিলিট হবে।", show_alert=True)
                # ব্যাকগ্রাউন্ডে অটো ডিলিট ফাংশন রান করা হচ্ছে
                asyncio.create_task(auto_delete_message(sent_msg, AUTO_DELETE_TIME))
            else:
                await query.answer("✅ ফাইল পাঠানো হয়েছে!", show_alert=True)
            
            return
        except Exception:
            return await query.answer("⚠️ Failed to send file.", show_alert=True)
        
    elif query.data.startswith("deletefile_"):
        file_msg_id = int(query.data.split("_")[1])
        user_id = query.from_user.id
        file_data = await db.files.find_one({"file_id": file_msg_id})
        if not file_data:
            return await query.answer("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ ᴏʀ ᴀʟʀᴇᴀᴅʏ ᴅᴇʟᴇᴛᴇᴅ.", show_alert=True)
        if file_data["user_id"] != user_id:
            return await query.answer("⚠️ Yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴅᴇʟᴇᴛᴇ ᴛʜɪꜱ ғɪʟᴇ!", show_alert=True)
        await db.files.delete_one({"file_id": file_msg_id})
        try:
            await client.delete_messages(BIN_CHANNEL, file_msg_id)
        except:
            pass
        await query.answer("✅ Fɪʟᴇ ᴅᴇʟᴇᴛᴇᴅ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟʏ!", show_alert=True)
        try:
            await query.message.edit_text("🗑️ Fɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ᴅᴇʟᴇᴛᴇᴅ ꜱᴜᴄᴄᴇꜱғᴜʟʟʏ.")
        except MessageNotModified:
            pass
        except Exception:
            pass
