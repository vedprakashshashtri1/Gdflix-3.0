import re
import math
import os
import gc
import io
import json
import html
import logging
from PIL import Image
import asyncio
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import tempfile
import uuid
import shutil
from cachetools import TTLCache
from curl_cffi import requests as cf_requests
from datetime import datetime
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.ext import Application, CommandHandler, MessageHandler, InlineQueryHandler, CallbackQueryHandler, filters, ContextTypes
from poster_utils import get_multiple_posters, get_best_poster, format_clean_poster_title
from media_utils import (
    get_media_stream_url,
    download_media_chunk,
    extract_mediainfo_from_file,
    format_mediainfo_text,
    generate_local_screenshots_pyav,
    extract_screenshots_smart,
    is_archive_filename,
    format_archive_mediainfo_fallback,
    get_zip_video_entries_sync,
    upload_screenshots,
    ensure_fast_poster_url,
    cleanup_temp_files,
    cleanup_all_temp_dirs,
    extract_audio_languages_from_url,
)
from database import db
from config import (
    ADMIN_ID,
    BOT_TOKEN,
    CHANNEL_ID,
    POST_CHANNEL_ID,
    DATA_FILE,
    GDFLIX_HOST,
    SPIDY_DDL_API,
    key,
    GDRIVE_API_KEY,
    API_ID,
    API_HASH,
    SESSION_STRING,
    AUDIO_EMOJI_ID,
    CROWN_EMOJI_ID,
    SPARKLE_EMOJI_ID,
)
from googleapiclient.discovery import build
from pyrogram import Client as PyroClient

logging.basicConfig(level=logging.INFO)

# ── Pyrogram Premium Client ──────────────────────────────────────────────────
pyro_client = None

async def get_pyro_client():
    global pyro_client
    if not API_ID or not API_HASH:
        logging.warning("Pyrogram: API_ID/API_HASH missing!")
        return None
    if not SESSION_STRING:
        # BOT_TOKEN mode duplicates python-telegram-bot, ~150MB waste for zero benefit.
        # OOM fix (exit code 9): skip Pyrogram unless premium session is set.
        return None
    try:
        if pyro_client is None:
            if SESSION_STRING:
                logging.info("Pyrogram: Starting client with SESSION_STRING (colored buttons enabled)...")
                pyro_client = PyroClient(
                    name="gdflix_pyro",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=SESSION_STRING,
                )
            else:
                logging.info("Pyrogram: Starting client with BOT_TOKEN (no colored buttons — set SESSION_STRING for colors)...")
                pyro_client = PyroClient(
                    name="gdflix_pyro",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    bot_token=BOT_TOKEN,
                )
            await pyro_client.start()
            logging.info("Pyrogram: Client started successfully!")
        return pyro_client
    except Exception as e:
        logging.warning(f"Pyrogram client start failed: {e}")
        return None


async def ensure_pyro_entity(chat_id):
    """Ensure pyrogram has cached entity for this chat_id"""
    try:
        client = await get_pyro_client()
        if client is None:
            return False
        # Force entity cache using raw API
        from pyrogram import raw
        try:
            await client.invoke(
                raw.functions.messages.GetPeerSettings(
                    peer=await client.resolve_peer(chat_id)
                )
            )
            return True
        except Exception:
            # Fallback: use get_chat
            try:
                await client.get_chat(chat_id)
                return True
            except Exception:
                return False
    except Exception:
        return False

async def pyro_send_message(chat_id, text):
    try:
        client = await get_pyro_client()
        if client is None:
            logging.warning("Pyrogram: client is None, falling back to bot")
            return False
        
        # Convert <tg-emoji emoji-id="..."> to Pyrogram's <emoji id="...">
        if text:
            text = text.replace("<tg-emoji emoji-id=", "<emoji id=").replace("</tg-emoji>", "</emoji>")

        # Username aur ID dono support karne ke liye safe conversion
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            chat_id_int = chat_id
        await client.send_message(chat_id_int, text, parse_mode="html", disable_web_page_preview=True)
        logging.info(f"Pyrogram: Message sent successfully to {chat_id_int}")
        return True
    except Exception as e:
        logging.warning(f"Pyrogram send failed: {e}")
        return False

async def pyro_send_photo(chat_id, photo_url: str, caption: str = "") -> bool:
    """
    Photo + caption Pyrogram (premium account) se bhejo taaki tg-emoji render ho.
    Pyrogram doesn't take direct URL — download first, then send via BytesIO.
    """
    try:
        client = await get_pyro_client()
        if client is None:
            logging.warning("Pyrogram: client is None, photo fallback to bot")
            return False
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            chat_id_int = chat_id

        # First download photo from URL to bytes
        async with httpx.AsyncClient() as http:
            resp = await http.get(photo_url, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            photo_bytes = resp.content

        import io
        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = "poster.jpg"  # Pyrogram ko extension chahiye

        # Convert <tg-emoji emoji-id="..."> to Pyrogram's <emoji id="...">
        if caption:
            caption = caption.replace("<tg-emoji emoji-id=", "<emoji id=").replace("</tg-emoji>", "</emoji>")

        await client.send_photo(chat_id_int, photo_io, caption=caption, parse_mode="html")
        logging.info(f"Pyrogram: Photo sent successfully to {chat_id_int}")
        return True
    except Exception as e:
        logging.warning(f"Pyrogram photo send failed: {e}")
        return False



# -----------------------------------
# Colored Buttons (Poster Verse Style) — Bot API 9.4 native style
# -----------------------------------

def build_colored_markup(user_id, index, total, poster_url=None):
    """Colored inline buttons — Poster Verse style (Bot API 9.4 native)"""
    rows = [
        [
            InlineKeyboardButton("⬅️ Prev", callback_data=f"poster_prev:{user_id}", style="primary"),
            InlineKeyboardButton(f"🔢 {index + 1}/{total}", callback_data="poster_noop"),
            InlineKeyboardButton("Next ➡️", callback_data=f"poster_next:{user_id}", style="primary"),
        ]
    ]
    if poster_url:
        rows.append([
            InlineKeyboardButton("🔗 Open/Copy URL", url=poster_url, style="success")
        ])
    rows.append([
        InlineKeyboardButton("✅ Select", callback_data=f"poster_select:{user_id}:{index}", style="success")
    ])
    rows.append([
        InlineKeyboardButton("❌ Close", callback_data="poster_close", style="danger")
    ])
    return InlineKeyboardMarkup(rows)


async def pyro_send_photo_with_buttons(chat_id, photo_url, caption, markup):
    """Pyrogram se photo + colored buttons bhejo"""
    try:
        client = await get_pyro_client()
        if client is None:
            return False, None

        import io
        async with httpx.AsyncClient() as http:
            resp = await http.get(photo_url, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            photo_bytes = resp.content

        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = "poster.jpg"

        caption = caption.replace("<tg-emoji emoji-id=", "<emoji id=").replace("</tg-emoji>", "</emoji>")

        msg = await client.send_photo(
            chat_id,
            photo_io,
            caption=caption,
            parse_mode="html",
            reply_markup=markup
        )
        logging.info(f"Pyrogram: Photo+Buttons sent to {chat_id}")
        return True, msg
    except Exception as e:
        logging.warning(f"Pyrogram photo+buttons failed: {e}")
        return False, None


async def pyro_edit_photo_with_buttons(message, photo_url, caption, markup):
    """Edit pyrogram message photo + buttons"""
    try:
        client = await get_pyro_client()
        if client is None:
            return False

        import io
        from pyrogram.types import InputMediaPhoto
        async with httpx.AsyncClient() as http:
            resp = await http.get(photo_url, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            photo_bytes = resp.content

        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = "poster.jpg"

        caption = caption.replace("<tg-emoji emoji-id=", "<emoji id=").replace("</tg-emoji>", "</emoji>")

        await client.edit_message_media(
            message.chat.id,
            message.id,
            InputMediaPhoto(media=photo_io, caption=caption, parse_mode="html"),
            reply_markup=markup
        )
        logging.info(f"Pyrogram: Photo+Buttons edited")
        return True
    except Exception as e:
        logging.warning(f"Pyrogram edit photo+buttons failed: {e}")
        return False


folder_cache = TTLCache(maxsize=100, ttl=3600)
gdflix_cache = TTLCache(maxsize=1000, ttl=3600)
channel_post_cache = TTLCache(maxsize=500, ttl=86400)

def detect_ott_from_filename(filename):
    """Detect OTT platform from filename"""
    fn = filename.lower()
    ott_map = {
        "nf": "Netflix", "netflix": "Netflix",
        "amzn": "Amazon Prime", "prime": "Amazon Prime", "amazon": "Amazon Prime",
        "hbo": "HBO Max", "hmax": "HBO Max",
        "dsnp": "Disney+ Hotstar", "disney": "Disney+ Hotstar", "hotstar": "Disney+ Hotstar",
        "atvp": "Apple TV+", "aptv": "Apple TV+", "apple": "Apple TV+",
        "pmtp": "Paramount+", "paramount": "Paramount+",
        "sony": "SonyLIV", "sonyliv": "SonyLIV",
        "zee5": "ZEE5", "zee": "ZEE5",
        "jio": "JioCinema", "jiocinema": "JioCinema",
        "mx": "MX Player", "mxplayer": "MX Player",
        "viki": "Rakuten Viki",
        "crave": "Crave",
        "stan": "Stan",
        "showtime": "Showtime",
        "starz": "Starz",
        "hulu": "Hulu",
        "peacock": "Peacock",
        "Discovery+": "Discovery+", "discovery": "Discovery+",
        "ALTBalaji": "ALTBalaji", "altbalaji": "ALTBalaji",
        "eros": "Eros Now", "erosnow": "Eros Now",
        "shemaroo": "Shemaroo",
        "youtube": "YouTube Premium",
        "yts": "YTS",
        "bms": "BookMyShows", "bookmyshow": "BookMyShows", "bookmyshows": "BookMyShows",
    }
    for code, platform in ott_map.items():
        if code in fn:
            return platform
    return ""


def is_owner(user_id):
    return user_id == ADMIN_ID


async def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True
    admins = await db.get_admins()
    return user_id in admins


async def is_allowed(user_id, chat_id):
    # Owner/admins always allowed
    if await is_admin(user_id):
        return True
    # In groups — all users can use the bot
    if chat_id and chat_id < 0:
        return True
    # Private chat — all users allowed
    return True


def convert_size(bytes):
    size_bytes = int(bytes)
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])


def extract_file_id(link):
    pattern = r'(?:/d/|id=|file/d/)([\w-]+)'
    match = re.search(pattern, link)
    return match.group(1) if match else None

def normalize_gdrive_link(link):
    """Convert uc?id= and other formats to standard /file/d/ID format"""
    file_id = extract_file_id(link)
    if file_id and 'folders' not in link:
        return f"https://drive.google.com/file/d/{file_id}/view"
    return link


async def gdflix_convert(gdrive_link, api_key=None):
    try:
        ID = extract_file_id(gdrive_link)
        if not ID: return None, None, None, "Invalid Google Drive link.", None
    except Exception as e: return None, None, None, str(e), None

    cache_key = f"{ID}_{api_key}"
    if cache_key in gdflix_cache:
        return gdflix_cache[cache_key]

    try:
        def _fetch():
            return cf_requests.get(
                "https://" + GDFLIX_HOST + "/v2/share?id=" + ID + "&key=" + (api_key or key),
                impersonate="chrome",
                timeout=30.0
            )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _fetch)
        if response.status_code != 200:
            return None, None, None, f"Server returned HTTP {response.status_code}. Try again later.", None
        raw = response.text.strip()
        if not raw: return None, None, None, "Server returned an empty response.", None
        try: res = response.json()
        except Exception: return None, None, None, f"Unexpected server response (not JSON)", None
        if "name" not in res or "key" not in res:
            msg = res.get("message") or res.get("error") or str(res)
            return None, None, None, f"API error: {msg}", None
        filename = res["name"]
        size = convert_size(res["size"])
        link = "https://gdflix.dev/file/" + res["key"]
        result = (filename, size, link, None, ID)
        gdflix_cache[cache_key] = result
        return result
    except Exception as e:
        return None, None, None, "API Error: " + str(e), None


def extract_folder_id(link):
    # Sirf /folders/ wala format folder hai
    # uc?id= aur /file/d/ folder nahi hain
    if 'uc?id=' in link or '/uc?' in link:
        return None
    match = re.search(r'folders/([-\w]+)', link)
    if match:
        return match.group(1)
    # folderview?id= wala format
    if 'folderview' in link:
        match = re.search(r'[?&]id=([-\w]+)', link)
        if match:
            return match.group(1)
    return None





async def get_folder_details(folder_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_folder_details_sync, folder_id)


def _get_folder_details_sync(folder_id):
    try:
        drive_service = build('drive', 'v3', developerKey=GDRIVE_API_KEY)
        folder_meta = drive_service.files().get(fileId=folder_id, fields='name').execute()
        folder_name = folder_meta.get('name', 'GDFlix Pack')
        
        all_files = []
        page_token = None
        while True:
            response = drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces='drive',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields='nextPageToken, files(id, name, mimeType, size)',
                pageToken=page_token
            ).execute()
            
            files_in_page = response.get('files', [])
            for file in files_in_page:
                if file.get('mimeType') != 'application/vnd.google-apps.folder':
                    all_files.append(file)
            
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
                
        def get_natural_key(f):
            name = re.sub(r'[^a-zA-Z0-9]', ' ', f.get('name', ''))
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', name)]
            
        all_files.sort(key=get_natural_key)
        
        total_size_bytes = sum(int(f.get('size', 0)) for f in all_files)
        total_size_str = convert_size(total_size_bytes)
        
        first_filename = all_files[0].get('name', '') if all_files else ''
        return folder_name, total_size_str, [f"https://drive.google.com/file/d/{f.get('id')}/view" for f in all_files], first_filename
    except Exception as e:
        logging.warning(f"Failed to fetch folder details for {folder_id}: {e}")
        return None, "0 B", [], ""


async def get_files_from_folder(folder_id):
    if folder_id in folder_cache:
        return folder_cache[folder_id]
        
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _get_files_from_folder_sync, folder_id)
    if result:
        folder_cache[folder_id] = result
    return result

def _get_files_from_folder_sync(folder_id):
    try:
        drive_service = build('drive', 'v3', developerKey=GDRIVE_API_KEY)
        all_files = []
        page_token = None
        while True:
            response = drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces='drive',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields='nextPageToken, files(id, name, mimeType)',
                pageToken=page_token
            ).execute()
            
            files_in_page = response.get('files', [])
            logging.info(f"Folder {folder_id}: page mein {len(files_in_page)} files mili")
            
            for file in files_in_page:
                if file.get('mimeType') != 'application/vnd.google-apps.folder':
                    all_files.append(file)
            
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
                
        logging.info(f"Folder {folder_id}: total {len(all_files)} files extract hui")
        
        def get_natural_key(f):
            name = re.sub(r'[^a-zA-Z0-9]', ' ', f.get('name', ''))
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', name)]
            
        all_files.sort(key=get_natural_key)
        
        return [f"https://drive.google.com/file/d/{f.get('id')}/view" for f in all_files]
    except Exception as e:
        logging.warning(f"Failed to fetch folder {folder_id}: {e}")
        return []

DEFAULT_BACKUP = "HrXRiPS.t.me"

async def get_backup(user_id):
    try:
        if not await db.is_backup_tag_enabled(user_id):
            return None
    except Exception:
        pass
    user_backup = await db.get_user_backup(user_id)
    if user_backup:
        return user_backup
    # If user has set their own backup channel with @username or link, use that
    try:
        user_chan = await db.get_user_backup_channel(user_id)
        if user_chan:
            s_chan = str(user_chan).strip()
            if s_chan.startswith("@") or "t.me/" in s_chan:
                return s_chan
            # If numeric private channel ID, do NOT use owner's default
            return None
    except Exception:
        pass
    # Owner/Admin fallback to default backup tag if enabled
    if await is_admin(user_id) and await db.is_default_backup_enabled():
        return DEFAULT_BACKUP
    # Regular users without a custom tag do not get owner's backup tag
    return None


def normalize_backup_channel_input(raw: str) -> str | None:
    """User input (-100xxx / @username / t.me link) -> normalized channel identifier."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # t.me links -> @username or raw invite
    if "t.me/" in s or "telegram.me/" in s:
        # private invite link (t.me/+xxxx / joinchat) can't be used directly — keep as-is so caller can warn
        if "/+" in s or "joinchat" in s:
            return s
        m = re.search(r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)', s)
        if m:
            return "@" + m.group(1)
        return s
    return s


def resolve_channel_id_for_api(channel_id: str | int):
    """Convert stored channel id to int when numeric, else keep string."""
    try:
        return int(str(channel_id).strip())
    except (ValueError, AttributeError):
        return str(channel_id).strip()


def format_backup_link(raw_backup: str | None) -> tuple[str, str]:
    """Parse backup string into (display_text, url) for post footer"""
    if not raw_backup:
        return ("HrXRiPS.t.me", "https://t.me/HrXRiPS")
    raw = str(raw_backup).strip()
    if not raw:
        return ("HrXRiPS.t.me", "https://t.me/HrXRiPS")
    
    if raw.startswith("http://") or raw.startswith("https://"):
        url = raw
        clean = re.sub(r"^https?://(www\.)?", "", raw).rstrip("/")
        if "t.me/" in clean:
            chan = clean.split("t.me/")[-1].lstrip("@")
            if chan.startswith("+") or chan.startswith("joinchat/"):
                display_text = "Join Channel"
            else:
                display_text = f"{chan}.t.me"
        else:
            display_text = clean
    elif raw.startswith("@"):
        chan = raw.lstrip("@")
        display_text = f"{chan}.t.me"
        url = f"https://t.me/{chan}"
    elif "t.me/" in raw:
        chan = raw.split("t.me/")[-1].lstrip("@")
        if chan.startswith("+") or chan.startswith("joinchat/"):
            display_text = "Join Channel"
        else:
            display_text = f"{chan}.t.me"
        url = f"https://t.me/{chan}"
    elif raw.endswith(".t.me"):
        chan = raw[:-5]
        display_text = raw
        url = f"https://t.me/{chan}"
    else:
        display_text = f"{raw}.t.me"
        url = f"https://t.me/{raw}"
    return (display_text, url)

# ── HTML Cleaner helper ─────────────────────────────────────────────────────
def safe_html(text):
    """Safe for Telegram HTML — escape only &"""
    if not text:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    return text.strip()


def format_response(filename, size, link):
    # Clean filename properly
    clean = filename.replace("_", ".")
    clean = re.sub(r'\.(mkv|mp4|avi|wmv|flv|mov|ts|webm)$', '', clean, flags=re.IGNORECASE)
    
    # Detect audio
    audio_tags = []
    if re.search(r'\bHindi\b', clean, re.IGNORECASE): audio_tags.append("Hindi")
    if re.search(r'\bEnglish\b', clean, re.IGNORECASE): audio_tags.append("English")
    audio_str = ", ".join(audio_tags[:2]) if audio_tags else ""

    # Build caption - clean style (no emojis)
    parts = [f"<b>{safe_html(clean)}</b>"]
    parts.append(f"<code>[{size}]</code>")
    parts.append(link)
    if audio_str:
        parts.append(f"🔊 {audio_str}")
    
    return "\n".join(parts)


def format_batch_response(results):
    return "\n\n".join(results)


def chunk_text_for_telegram(text, first_limit=1024, next_limit=4000):
    parts = text.split('\n\n')
    chunks = []
    chunk = ""
    current_limit = first_limit
    for part in parts:
        if len(chunk) + len(part) + 2 <= current_limit:
            chunk += ("\n\n" + part) if chunk else part
        else:
            if chunk:
                chunks.append(chunk)
            chunk = part
            current_limit = next_limit
    if chunk:
        chunks.append(chunk)
    return chunks


async def save_history(user_id, filename, size, link):
    entry = {
        "filename": filename,
        "size": size,
        "link": link,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    await db.add_history(user_id, entry)
    await db.increment_stats(user_id)


async def get_user_key(user_id):
    return await db.get_user_key(user_id, key)


async def set_user_key(user_id, api_key):
    await db.set_user_key(user_id, api_key)


async def remove_user_key(user_id):
    await db.remove_user_key(user_id)


def extract_links(text):
    tokens = text.replace("\n", " ").split()
    return [
        t.strip() for t in tokens
        if "drive.google.com" in t or "mrspidy" in t or "ddl-by" in t or "direct.aspx?id=" in t
    ]


# -----------------------------------
# Poster Function
# -----------------------------------

async def poster_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    data = query.data

    posters = context.user_data.get(
        "poster_results",
        []
    )

    if not posters:
        await query.answer()
        return

    # poster_noop — just answer, do nothing
    if data == "poster_noop":
        await query.answer()
        return

    # -----------------------------------
    # User ownership check
    # -----------------------------------
    if ":" in data:
        try:
            btn_owner = int(data.split(":")[1])
            if btn_owner != update.effective_user.id:
                await query.answer("⚠️ This poster is not yours!", show_alert=True)
                return
        except (ValueError, IndexError):
            pass

    await query.answer()

    current_index = 0

    try:
        btn_text = query.message.reply_markup.inline_keyboard[0][1].text
        btn_text = btn_text.replace("🔢 ", "").strip()
        current_index = int(btn_text.split("/")[0]) - 1
    except Exception:
        pass

    # -----------------------------------
    # NEXT
    # -----------------------------------
    if data.startswith("poster_next"):
        if current_index < len(posters) - 1:
            current_index += 1

    # -----------------------------------
    # PREV
    # -----------------------------------
    elif data.startswith("poster_prev"):
        if current_index > 0:
            current_index -= 1

    # -----------------------------------
    # CLOSE
    # -----------------------------------
    elif data == "poster_close":
        await query.message.delete()
        return

    # -----------------------------------
    # SELECT — Done message with poster + links
    # -----------------------------------
    elif data.startswith("poster_select"):
        try:
            select_index = int(data.split(":")[2])
            if 0 <= select_index < len(posters):
                current_index = select_index
        except (IndexError, ValueError):
            pass

        final_msg = context.user_data.get("final_msg")
        selected = posters[current_index]

        if final_msg is None:
            # Standalone /poster mode
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=selected["poster"],
                    caption=f"✨ <b>{selected['title']}</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.warning(f"User send failed: {e}")
        else:
            # DONE MESSAGE
            chunks = chunk_text_for_telegram(final_msg, first_limit=1024, next_limit=4000)
            done_caption = chunks[0] if chunks else f"✨ <b>{selected['title']}</b>"

            try:
                client = await get_pyro_client()
                if client:
                    done_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Done", callback_data="poster_noop", style="success")]
                    ])

                    import io as _io
                    async with httpx.AsyncClient() as http:
                        resp = await http.get(selected["poster"], timeout=20.0, follow_redirects=True)
                        resp.raise_for_status()
                        photo_bytes = resp.content
                    photo_io = _io.BytesIO(photo_bytes)
                    photo_io.name = "poster.jpg"

                    if CHANNEL_ID and update.effective_chat.type == "private":
                        try:
                            await client.send_photo(
                                int(CHANNEL_ID),
                                photo_io,
                                caption=done_caption,
                                parse_mode="html",
                                reply_markup=done_markup
                            )
                            for c in chunks[1:]:
                                await client.send_message(int(CHANNEL_ID), c, parse_mode="html")
                                await asyncio.sleep(1)
                        except Exception as e:
                            logging.warning(f"Channel Done send failed: {e}")

                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=selected["poster"], caption=done_caption, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="poster_noop", style="success")]]))
                    for c in chunks[1:]:
                        await context.bot.send_message(chat_id=update.effective_chat.id, text=c, parse_mode="HTML", disable_web_page_preview=True)
                        await asyncio.sleep(1)
                else:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=selected["poster"], caption=done_caption, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="poster_noop", style="success")]]))
                    for c in chunks[1:]:
                        await context.bot.send_message(chat_id=update.effective_chat.id, text=c, parse_mode="HTML", disable_web_page_preview=True)
                        await asyncio.sleep(1)
            except Exception as e:
                logging.warning(f"Done message failed: {e}")

        await query.message.delete()
        return

    # -----------------------------------
    # NAVIGATE — Poster verse style card with metadata + colored buttons
    # -----------------------------------
    current = posters[current_index]

    # Metadata card — detect OTT from API first, then filename
    raw_fn = context.user_data.get("raw_filename", "")
    api_ott = current.get("ott", "")
    detected_ott = api_ott or detect_ott_from_filename(raw_fn)
    card_lines = [f"🎬 <b>{safe_html(current['title'])}</b>", ""]
    card_lines.append(f"✦ 📅 <b>Year :</b> {current.get('year') or 'N/A'}")
    card_lines.append(f"✦ 📺 <b>Platform :</b> {detected_ott or '🌐 Web'}")
    card_lines.append(f"✦ 🖼 <b>Type :</b> Landscape")
    card_lines.append(f"\n⚡ <i>Powered by @HrXRiPS</i>")
    caption = "\n".join(card_lines)

    colored_markup = build_colored_markup(
        user_id=update.effective_user.id,
        index=current_index,
        total=len(posters),
        poster_url=current["poster"]
    )

    # python-telegram-bot (native Bot API 9.4 colored buttons)
    try:
        await query.message.edit_media(
            media=InputMediaPhoto(
                media=current["poster"],
                caption=caption,
                parse_mode="HTML"
            ),
            reply_markup=build_poster_keyboard(
                user_id=update.effective_user.id,
                index=current_index,
                total=len(posters),
                poster_url=current["poster"]
            )
        )
    except Exception as e:
        logging.warning(f"Edit poster failed: {e}")

def clean_filename_for_search(filename: str) -> str:
    """Raw GDFlix filename se clean movie/show title nikalta hai poster search ke liye"""
    # File extension hata do
    name = re.sub(r'\.\w{2,4}$', '', filename)
    # Dots aur underscores ko space mein badlo
    name = name.replace(".", " ").replace("_", " ")
    # Yahan se sab junk hata do — quality/source/codec/group se pehle jo title hai wo lo
    # Pattern: title + year tak sab lo, baad ka sab junk hai
    # Year ke baad sab hata do
    match = re.match(r'^(.+?\b(19|20)\d{2}\b)', name)
    if match:
        name = match.group(1)
    else:
        # Year nahi mila to manually junk hata do
        junk = re.compile(
            r'\b(4k|2160p|1080p|720p|480p|360p|bluray|blu ray|webrip|web dl|webdl|'
            r'hdtv|dvdrip|hdrip|bdrip|x264|x265|h264|h265|hevc|avc|xvid|10bit|hdr|sdr|'
            r'dts|aac|ac3|ddp|dd5|atmos|truehd|dovi|remux|proper|repack|nf|amzn|hbo|'
            r'apex|yts|rarbg|mkv|mp4|avi|extended|theatrical|directors|uncut|'
            r'multi|dubbed|hindi|english|tamil|telugu)\b',
            re.IGNORECASE
        )
        name = junk.sub('', name)
    # Extra spaces and trailing dash/dot cleanup
    name = re.sub(r'[-–]+$', '', name.strip())
    name = re.sub(r'\s+', ' ', name).strip()
    return name


async def fetch_posters(filename: str) -> list[dict]:
    """poster_utils format_clean_poster_title handles cleaning — pass raw filename"""
    logging.info(f"Poster search for: '{filename}'")
    return await get_multiple_posters(filename)


# -----------------------------------
# Build Keyboard
# -----------------------------------

def build_poster_keyboard(
    user_id: int,
    index: int,
    total: int,
    poster_url: str | None = None
) -> InlineKeyboardMarkup:

    rows = [[
        InlineKeyboardButton(
            "⬅️ Prev",
            callback_data=f"poster_prev:{user_id}",
            style="primary"
        ),

        InlineKeyboardButton(
            f"{index + 1}/{total}",
            callback_data="poster_noop"
        ),

        InlineKeyboardButton(
            "Next ➡️",
            callback_data=f"poster_next:{user_id}",
            style="primary"
        )
    ]]

    if poster_url:
        rows.append([
            InlineKeyboardButton(
                "🔗 Open/Copy URL",
                url=poster_url,
                style="success"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "✅ Select",
            callback_data=f"poster_select:{user_id}:{index}",
            style="success"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "❌ Close",
            callback_data="poster_close",
            style="danger"
        )
    ])

    return InlineKeyboardMarkup(rows)

# ── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Pyrogram entity cache — colored buttons ke liye
    try:
        client = await get_pyro_client()
        if client:
            await ensure_pyro_entity(chat_id)
    except Exception:
        pass

    if not await is_allowed(user_id, chat_id):
        user_name = update.effective_user.first_name or "User"
        msg = (
            "⛔ *RESTRICTED BOT* ⛔\n\n"
            "Hey *" + user_name + "*,\n"
            "You are *not authorized* to use this bot.\n"
            "This is a *private & invite-only* service.\n\n"
            "📩 Contact admin to request access."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return
    user_name = update.effective_user.first_name or "User"
    msg = (
        f"<b>👋 Welcome, {safe_html(user_name)}!</b>\n\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"<b>🎬 GDFlix Bot v2.0</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        f"<b>Send any Google Drive link and I will automatically generate GDFLIX link, MediaInfo, screenshots and channel post.</b>\n\n"
        f"<b>📌 Commands:</b>\n"
        f"<b>├ /gdflix - Convert Drive link</b>\n"
        f"<b>├ /poster - Search movie poster</b>\n"
        f"<b>├ /history - Your last 10 links</b>\n"
        f"<b>├ /settings - Backup tag + backup channel</b>\n"
        f"<b>└ /help - See all commands</b>\n\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"<b>⚡ Powered by GDFlix</b>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_allowed(user_id, chat_id):
        await update.message.reply_text("Access denied.")
        return
    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>How to Use</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Commands:</b>\n"
        f"├ /gdflix - Convert Drive link\n"
        f"├ /poster - Search movie poster\n"
        f"├ /history - Last 10 conversions\n"
        f"├ /stats - Usage statistics\n"
        f"├ /setkey - Save your API key\n"
        f"├ /mykey - Check saved key\n"
        f"├ /removekey - Remove key\n"
        f"├ /setbackup - Set backup channel\n"
        f"├ /removebackup - Remove backup\n"
        f"└ /settings - Backup tag + backup channel\n\n"
        f"👑 <b>Owner Commands:</b>\n"
        f"├ /addadmin - Add admin by ID\n"
        f"├ /removeadmin - Remove admin\n"
        f"├ /listadmins - List all admins\n"
        f"├ /defaultbackup - Toggle default backup\n"
        f"└ /clearcache - Clear cache\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def get_telegram_file_id(bot_token: str, chat_id: int | str, source: str) -> str | None:
    """
    Upload a local image file or URL to Telegram to obtain a permanent native file_id.
    Sends silently to chat and deletes immediately.
    Guarantees Telegram servers never fail with 'failed to get HTTP URL content'.
    """
    if not source:
        return None
    # If source is already a Telegram file_id, return it immediately
    if not os.path.exists(source) and not source.startswith("http"):
        return source
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    del_url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            content = None
            if os.path.exists(source):
                with open(source, "rb") as f:
                    content = f.read()
            elif source.startswith("http"):
                try:
                    img_resp = await client.get(source, follow_redirects=True, timeout=25.0)
                    if img_resp.status_code == 200 and len(img_resp.content) > 500:
                        content = img_resp.content
                except Exception as e_dl:
                    logging.warning(f"Failed to fetch remote image from {source}: {e_dl}")

            if not content:
                return None

            # Optimize image if large (> 500 KB or PNG) so upload takes < 0.2s
            if len(content) > 500 * 1024:
                try:
                    img = Image.open(io.BytesIO(content))
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                    if img.width > 1280:
                        ratio = 1280 / img.width
                        img = img.resize((1280, int(img.height * ratio)), Image.Resampling.LANCZOS)
                    out_buf = io.BytesIO()
                    img.save(out_buf, format='JPEG', quality=85, optimize=True)
                    content = out_buf.getvalue()
                except Exception as e_opt:
                    logging.warning(f"Image optimization skipped: {e_opt}")

            for attempt in range(3):
                r = await client.post(
                    url,
                    data={"chat_id": chat_id, "disable_notification": True},
                    files={"photo": ("image.jpg", content, "image/jpeg")}
                )
                data = r.json()
                if data.get("ok"):
                    fid = data["result"]["photo"][-1]["file_id"]
                    mid = data["result"]["message_id"]
                    try:
                        await client.post(del_url, data={"chat_id": chat_id, "message_id": mid})
                    except Exception:
                        pass
                    return fid
                if data.get("error_code") == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 2)
                    logging.warning(f"Telegram 429 rate limit in get_telegram_file_id: sleeping {retry_after}s...")
                    await asyncio.sleep(retry_after + 1)
                else:
                    logging.warning(f"sendPhoto failed: {data}")
                    break
    except Exception as e:
        logging.warning(f"Error getting telegram file_id for {source}: {e}")
    return None


async def get_clean_title_and_poster(filename: str) -> tuple[str, str | None]:
    """Helper to return clean movie/series title and best poster URL"""
    clean_meta = format_clean_poster_title(filename)
    clean_title = clean_meta[0] if isinstance(clean_meta, (tuple, list)) else clean_meta
    poster_url = await get_best_poster(filename)
    return clean_title, poster_url


async def publish_channel_post(
    context: ContextTypes.DEFAULT_TYPE,
    channel_id: str | int,
    filename: str | None = None,
    size: str | None = None,
    gdflix_link: str | None = None,
    poster_url: str | None = None,
    formatted_mediainfo: str | None = None,
    screenshots: list[str] | None = None,
    screenshot_urls: list[str] | None = None,
    items: list[dict] | None = None,
    user_id: int | None = None,
    backup_link: str | None = None
) -> tuple[int | None, str | None, str | None]:
    """
    Publish formatted post matching YouthPapa / RGP Rips channel layout:
    - Rich Message with native blocks:
      ✨ Filename [Size]
      • GDFLIX
      ⌄ MediaInfo (Collapsible Box with formatted MediaInfo code block & copy button)
      ⌄ Screenshots (4) (Collapsible Carousel with 4 actual image photos, NO text links!)
      Divider (if multiple items)
      Movie / Show Poster at the bottom!
    """
    try:
        try:
            target_chat_id = int(channel_id)
        except ValueError:
            target_chat_id = str(channel_id)

        upload_chat = ADMIN_ID or target_chat_id

        # Resolve backup link for user or fallback to default
        if backup_link is None:
            if user_id is not None:
                backup_link = await get_backup(user_id)
            elif await db.is_default_backup_enabled():
                backup_link = DEFAULT_BACKUP

        # Build list of items to post
        if items is None:
            items = [{
                "filename": filename or "Media File",
                "size": size or "N/A",
                "gdflix_link": gdflix_link or "#",
                "formatted_mediainfo": formatted_mediainfo or "",
                "screenshots": screenshots or [],
                "screenshot_urls": screenshot_urls or []
            }]

        # Upload poster to Telegram to obtain native file_id
        poster_fid = None
        if poster_url:
            poster_fid = await get_telegram_file_id(BOT_TOKEN, upload_chat, poster_url)

        # Build rich_message blocks
        rich_blocks = []
        all_uploaded_shot_fids = []

        for idx, itm in enumerate(items):
            fn = itm.get("filename", "Media File")
            sz = itm.get("size", "N/A")
            gdl = itm.get("gdflix_link", "#")
            mi = itm.get("formatted_mediainfo", "")
            
            # Prefer already uploaded file_ids, then local files or URLs
            shot_sources = itm.get("screenshot_urls") or itm.get("screenshots") or []
            shot_fids = []
            for s in shot_sources:
                if not s:
                    continue
                s_str = str(s).strip()
                if not s_str.startswith("http") and not os.path.exists(s_str) and len(s_str) > 15 and " " not in s_str:
                    shot_fids.append(s_str)
                else:
                    fid = await get_telegram_file_id(BOT_TOKEN, upload_chat, s_str)
                    if fid:
                        shot_fids.append(fid)
            all_uploaded_shot_fids.extend(shot_fids)

            # Clean filename
            clean_fn = fn.replace("_", ".")
            title_text = f"✨ {clean_fn} [{sz}]"

            # 1. Title paragraph (bold)
            rich_blocks.append({
                "type": "paragraph",
                "text": {
                    "type": "bold",
                    "text": title_text
                }
            })

            # 2. GDFLIX link paragraph
            rich_blocks.append({
                "type": "paragraph",
                "text": {
                    "type": "url",
                    "text": {
                        "type": "bold",
                        "text": "▸ GDFLIX"
                    },
                    "url": gdl
                }
            })

            # 3. MEDIAINFO details block (FIRST)
            if mi:
                rich_blocks.append({
                    "type": "details",
                    "summary": {
                        "type": "bold",
                        "text": "MediaInfo"
                    },
                    "blocks": [
                        {
                            "type": "blockquote",
                            "blocks": [
                                {
                                    "type": "pre",
                                    "text": mi
                                }
                            ]
                        }
                    ]
                })

            # 4. SHOTS details block (SECOND) - Slideshow carousel with left/right buttons
            if shot_fids:
                shot_photo_blocks = [
                    {
                        "type": "photo",
                        "photo": {
                            "type": "photo",
                            "media": fid
                        }
                    }
                    for fid in shot_fids
                ]
                rich_blocks.append({
                    "type": "details",
                    "summary": {
                        "type": "bold",
                        "text": f"Screenshots ({len(shot_fids)})"
                    },
                    "blocks": [
                        {
                            "type": "slideshow",
                            "blocks": shot_photo_blocks
                        }
                    ]
                })

            # Divider between items if there are multiple links/files
            if idx < len(items) - 1:
                rich_blocks.append({"type": "divider"})

        # 5. Backup Channel — above poster, bold + underlined link
        if backup_link:
            rich_blocks.append({"type": "divider"})
            backup_display, backup_url = format_backup_link(backup_link)
            rich_blocks.append({
                "type": "paragraph",
                "text": [
                    {
                        "type": "bold",
                        "text": "✦ Back-Up: "
                    },
                    {
                        "type": "url",
                        "text": {
                            "type": "underline",
                            "text": {
                                "type": "bold",
                                "text": backup_display
                            }
                        },
                        "url": backup_url
                    },
                    {
                        "type": "bold",
                        "text": " ✦"
                    }
                ]
            })

        # 6. Poster at very bottom inside blockquote
        final_poster = poster_fid or (all_uploaded_shot_fids[0] if all_uploaded_shot_fids else None)
        if final_poster:
            rich_blocks.append({
                "type": "blockquote",
                "blocks": [
                    {
                        "type": "photo",
                        "photo": {
                            "type": "photo",
                            "media": final_poster
                        }
                    }
                ]
            })

        main_msg_id = None
        # Attempt to send via Telegram Bot API sendRichMessage
        try:
            rich_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"
            payload = {
                "chat_id": target_chat_id,
                "rich_message": {"blocks": rich_blocks}
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(rich_url, json=payload)
                data = resp.json()
                if data.get("ok"):
                    main_msg_id = data["result"]["message_id"]
                    logging.info(f"Successfully published post via sendRichMessage, message_id={main_msg_id}")
                else:
                    logging.warning(f"sendRichMessage failed: {resp.status_code} - {resp.text}")
        except Exception as e_rich:
            logging.warning(f"sendRichMessage call encountered error: {e_rich}")

        # Fallback to standard send_photo / send_message if sendRichMessage failed
        if not main_msg_id:
            logging.info("Falling back to standard Telegram message posting...")
            fallback_blocks = []
            for itm in items:
                fn = itm.get("filename", "Media File")
                sz = itm.get("size", "N/A")
                gdl = itm.get("gdflix_link", "#")
                mi = itm.get("formatted_mediainfo", "")
                shots = itm.get("screenshot_urls", [])

                block = (
                    f"✨ <b>{safe_html(fn)}</b> [{sz}]\n"
                    f"▸ <a href=\"{gdl}\"><b>GDFLIX</b></a>\n"
                    f"<blockquote expandable><b>MediaInfo</b>\n<pre>{html.escape(mi)}</pre></blockquote>"
                )
                if shots:
                    shots_links = "  ".join([f"• <a href=\"{u}\">Screenshot {i+1}</a>" for i, u in enumerate(shots)])
                    block += f"\n<blockquote expandable><b>Screenshots ({len(shots)})</b>\n{shots_links}</blockquote>"
                fallback_blocks.append(block)

            if len(fallback_blocks) > 1:
                full_text = "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n".join(fallback_blocks)
            else:
                full_text = "\n\n".join(fallback_blocks)
            if backup_link:
                backup_display, backup_url = format_backup_link(backup_link)
                full_text += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n<b>✦ Back-Up: <a href=\"{backup_url}\"><b><u>{backup_display}</u></b></a> ✦</b>"
            main_msg = None

            if poster_url:
                if len(full_text) <= 1024:
                    try:
                        main_msg = await context.bot.send_photo(
                            chat_id=target_chat_id,
                            photo=poster_url,
                            caption=full_text,
                            parse_mode="HTML",
                            show_caption_above_media=True
                        )
                    except Exception as e:
                        logging.warning(f"Fallback send_photo failed: {e}")

                if not main_msg:
                    try:
                        main_msg = await context.bot.send_message(
                            chat_id=target_chat_id,
                            text=full_text,
                            parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(
                                url=poster_url,
                                prefer_large_media=True,
                                show_above_text=False
                            )
                        )
                    except Exception as e:
                        logging.warning(f"Fallback send_message with link preview failed: {e}")

            if not main_msg:
                main_msg = await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=full_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            main_msg_id = main_msg.message_id

        # Construct post link
        post_link = None
        try:
            chat_obj = await context.bot.get_chat(target_chat_id)
            if chat_obj.username:
                post_link = f"https://t.me/{chat_obj.username}/{main_msg_id}"
            else:
                c_clean = str(chat_obj.id).replace("-100", "")
                post_link = f"https://t.me/c/{c_clean}/{main_msg_id}"
        except Exception:
            c_clean = str(target_chat_id).replace("-100", "")
            post_link = f"https://t.me/c/{c_clean}/{main_msg_id}"

        return main_msg_id, post_link, None
    except Exception as e:
        logging.exception(f"Channel publish failed: {e}")
        return None, None, str(e)


async def process_single_drive_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    gdrive_link: str,
    user_id: int
):
    """
    Automatic pipeline for a single Google Drive URL:
    Drive link -> GDFLIX -> Poster -> MediaInfo -> 4 Screenshots -> Upload -> Channel Post -> User Result
    """
    status_msg = await update.message.reply_text("⏳ <b>Processing...</b>", parse_mode="HTML")
    
    temp_dir = tempfile.mkdtemp(prefix="gdflix_post_")
    temp_video = os.path.join(temp_dir, "sample.mkv")
    screenshots = []

    try:
        # Step 1: Detect Google Drive file ID
        gdrive_id = extract_file_id(gdrive_link)
        if not gdrive_id:
            await status_msg.edit_text("❌ <b>Invalid Google Drive link.</b>", parse_mode="HTML")
            return

        await status_msg.edit_text(
            "✓ Google Drive file detected\n⏳ <b>Generating GDFLIX link...</b>",
            parse_mode="HTML"
        )

        # Step 2: Generate GDFLIX URL using EXISTING system
        normalized_lnk = normalize_gdrive_link(gdrive_link)
        filename, size, dl_link, error, file_id = await gdflix_convert(
            normalized_lnk,
            await get_user_key(user_id)
        )

        if not filename or not dl_link:
            err_msg = error or "Failed to generate GDFLIX link."
            await status_msg.edit_text(
                f"✓ Google Drive file detected\n❌ <b>GDFLIX generation failed:</b> {safe_html(err_msg)}",
                parse_mode="HTML"
            )
            return

        # Save conversion history
        await save_history(user_id, filename, size, dl_link)

        # Step 3: Fetch Poster
        await status_msg.edit_text(
            "✓ Google Drive file detected\n"
            "✓ GDFLIX link generated\n"
            "⏳ <b>Finding poster...</b>",
            parse_mode="HTML"
        )

        poster_url = await get_best_poster(filename)
        poster_status = "✓ Poster found\n" if poster_url else "✓ Poster lookup completed\n"

        # Step 4: Extract MediaInfo
        await status_msg.edit_text(
            f"✓ Google Drive file detected\n"
            f"✓ GDFLIX link generated\n"
            f"{poster_status}"
            f"⏳ <b>Extracting MediaInfo...</b>",
            parse_mode="HTML"
        )

        stream_url = await get_media_stream_url(dl_link, file_id or gdrive_id)
        formatted_mi = ""
        info_json = {}

        # Parse real file size in bytes from GDFLIX size string (e.g. "2.1 GB", "850 MB")
        def parse_size_to_bytes(s: str) -> int:
            try:
                s = s.strip().upper().replace(",", ".")
                if "GB" in s:
                    return int(float(s.replace("GB", "").strip()) * 1024**3)
                elif "MB" in s:
                    return int(float(s.replace("MB", "").strip()) * 1024**2)
                elif "KB" in s:
                    return int(float(s.replace("KB", "").strip()) * 1024)
            except Exception:
                pass
            return 0

        real_total_bytes = parse_size_to_bytes(size or "")

        spidy_ddl = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={file_id or gdrive_id}" if (file_id or gdrive_id) else None
        downloaded = False
        first_chunk_url = (spidy_ddl if is_archive_filename(filename) and spidy_ddl else stream_url) or spidy_ddl
        if first_chunk_url:
            try:
                downloaded = await asyncio.wait_for(
                    download_media_chunk(first_chunk_url, temp_video, max_bytes=10 * 1024 * 1024, timeout=15.0),
                    timeout=20.0
                )
            except Exception as e_dl:
                logging.warning(f"Primary chunk download error ({first_chunk_url[:45]}): {e_dl}")
                downloaded = os.path.exists(temp_video) and os.path.getsize(temp_video) >= 512 * 1024

        # Fallback if primary chunk download failed
        if not downloaded:
            for fb_url in [stream_url, spidy_ddl]:
                if fb_url and fb_url != first_chunk_url:
                    try:
                        downloaded = await asyncio.wait_for(
                            download_media_chunk(fb_url, temp_video, max_bytes=10 * 1024 * 1024, timeout=15.0),
                            timeout=20.0
                        )
                        if downloaded:
                            break
                    except Exception as e_fb:
                        logging.warning(f"Fallback chunk download error: {e_fb}")
                        downloaded = os.path.exists(temp_video) and os.path.getsize(temp_video) >= 512 * 1024
                        if downloaded:
                            break

        archive_entries = []
        if is_archive_filename(filename):
            arch_url = (spidy_ddl if spidy_ddl else stream_url) or stream_url or ""
            if arch_url:
                try:
                    archive_entries = await asyncio.to_thread(get_zip_video_entries_sync, arch_url)
                except Exception as e_ar:
                    logging.debug(f"Failed to fetch zip entries: {e_ar}")

        if downloaded:
            info_json = await extract_mediainfo_from_file(temp_video)
            cleanup_temp_files(temp_video)
            temp_video = None
            if info_json:
                formatted_mi = format_mediainfo_text(
                    info_json,
                    filename=filename,
                    fallback_size=size,
                    total_bytes=real_total_bytes,
                    archive_entries=archive_entries
                )

        # Clean fallback if mediainfo failed
        if not formatted_mi:
            clean_fn = (filename or "Media File").replace("_", ".")
            if is_archive_filename(filename):
                formatted_mi = format_archive_mediainfo_fallback(filename, size, archive_entries=archive_entries)
            else:
                formatted_mi = (
                    f"Filename  : {clean_fn}\n"
                    f"Format    : Matroska\n"
                    f"Size      : {size or 'N/A'}\n"
                    f"Duration  : N/A\n"
                    f"Bitrate   : N/A\n"
                    "VIDEO\n"
                    "Format    : HEVC\n"
                    "Resolution: N/A\n"
                    "Bit depth : 8 bits\n"
                    "FPS       : 24.000 FPS\n"
                    "AUDIO\n"
                    "[Hindi] Dolby Digital Plus"
                )

        # Step 5: Generate 4 Screenshots
        await status_msg.edit_text(
            f"✓ Google Drive file detected\n"
            f"✓ GDFLIX link generated\n"
            f"{poster_status}"
            f"✓ MediaInfo extracted\n"
            f"⏳ <b>Generating 4 screenshots...</b>",
            parse_mode="HTML"
        )

        # Step 5: 4 screenshots via in-process PyAV (no FFmpeg, no download).
        # Archives (.zip): frames come from the embedded video (stored-proxy /
        # small-deflate), same 4-shot output as single/multi video posts.
        _zip_cands = []
        _zid = file_id or gdrive_id
        spidy_url = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={_zid}" if _zid else None
        if _zid:
            _zip_cands = [spidy_url, f"https://drive.google.com/uc?id={_zid}&export=download"]
        src_stream = (spidy_url if is_archive_filename(filename) and spidy_url else stream_url) or spidy_url
        screenshots = await extract_screenshots_smart(filename, src_stream, _zip_cands, temp_dir)
        if not screenshots:
            logging.warning("API shots 0/4 — post bina shots ke jayega")

        # Immediate cleanup of heavy video chunk to free disk space immediately
        cleanup_temp_files(temp_video)
        gc.collect()

        # Step 6: Upload Screenshots
        screenshot_urls = []
        if screenshots:
            await status_msg.edit_text(
                f"✓ Google Drive file detected\n"
                f"✓ GDFLIX link generated\n"
                f"{poster_status}"
                f"✓ MediaInfo extracted\n"
                f"✓ {len(screenshots)} screenshots generated\n"
                f"⏳ <b>Uploading screenshots to Telegram...</b>",
                parse_mode="HTML"
            )
            upload_chat = ADMIN_ID or POST_CHANNEL_ID or CHANNEL_ID
            tasks = [get_telegram_file_id(BOT_TOKEN, upload_chat, s) for s in screenshots]
            screenshot_urls = [f for f in await asyncio.gather(*tasks) if f]

        # Step 7: Publishing to channel
        await status_msg.edit_text(
            f"✓ Google Drive file detected\n"
            f"✓ GDFLIX link generated\n"
            f"{poster_status}"
            f"✓ MediaInfo extracted\n"
            f"✓ {len(screenshot_urls)} screenshots uploaded\n"
            f"⏳ <b>Publishing to channel...</b>",
            parse_mode="HTML"
        )

        target_channel = POST_CHANNEL_ID or CHANNEL_ID
        is_group = update.effective_chat.type in ("group", "supergroup") or (update.effective_chat.id and update.effective_chat.id < 0)
        is_user_admin = await is_admin(user_id)

        post_url = None
        post_error = None

        # Custom backup channel set → post ONLY there, never on owner's channel
        user_backup_channel_id = None
        user_channel_post_url = None
        user_channel_error = None
        try:
            user_backup_channel_id = await db.get_user_backup_channel(user_id)
        except Exception:
            user_backup_channel_id = None

        # 1. Publish to main owner channel ONLY if user is owner/admin and has NO custom backup channel
        if is_user_admin and target_channel and not user_backup_channel_id:
            msg_id, post_url, post_error = await publish_channel_post(
                context=context,
                channel_id=target_channel,
                filename=filename,
                size=size,
                gdflix_link=dl_link,
                poster_url=poster_url,
                formatted_mediainfo=formatted_mi,
                screenshots=screenshots,
                screenshot_urls=screenshot_urls,
                user_id=user_id
            )

        # 1b. Publish to user's own backup channel (if set in /settings)
        if user_backup_channel_id:
            try:
                _, user_channel_post_url, user_channel_error = await publish_channel_post(
                    context=context,
                    channel_id=resolve_channel_id_for_api(user_backup_channel_id),
                    filename=filename,
                    size=size,
                    gdflix_link=dl_link,
                    poster_url=poster_url,
                    formatted_mediainfo=formatted_mi,
                    screenshots=screenshots,
                    screenshot_urls=screenshot_urls,
                    user_id=user_id
                )
                if user_channel_error:
                    logging.warning(f"User backup-channel post failed for {user_id}: {user_channel_error}")
            except Exception as e_ub:
                user_channel_error = str(e_ub)
                logging.warning(f"User backup-channel post exception for {user_id}: {e_ub}")

        # 2. If link was sent in a group: also post the full formatted post directly in the group!
        if is_group:
            if not target_channel or str(target_channel) != str(update.effective_chat.id):
                await publish_channel_post(
                    context=context,
                    channel_id=update.effective_chat.id,
                    filename=filename,
                    size=size,
                    gdflix_link=dl_link,
                    poster_url=poster_url,
                    formatted_mediainfo=formatted_mi,
                    screenshots=screenshots,
                    screenshot_urls=screenshot_urls,
                    user_id=user_id
                )
            try:
                await status_msg.delete()
            except Exception:
                pass
        elif not is_user_admin and not user_backup_channel_id:
            # Regular user in private chat without a backup channel: deliver full formatted post directly in PM!
            await publish_channel_post(
                context=context,
                channel_id=update.effective_chat.id,
                filename=filename,
                size=size,
                gdflix_link=dl_link,
                poster_url=poster_url,
                formatted_mediainfo=formatted_mi,
                screenshots=screenshots,
                screenshot_urls=screenshot_urls,
                user_id=user_id
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            # Private chat response for admin or user with backup channel
            if (post_error or not post_url) and not user_channel_post_url:
                err_extra = ""
                if user_backup_channel_id and user_channel_error:
                    err_extra = f"\n⚠️ Backup channel also failed: {safe_html(user_channel_error)} — make the bot admin in your backup channel (see /settings)."
                await status_msg.edit_text(
                    f"❌ <b>Publishing to channel failed:</b> {safe_html(post_error or user_channel_error or 'Unknown error')}{err_extra}\n\n"
                    f"🔗 <b>GDFLIX Link:</b> {dl_link}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶ GDFLIX", url=dl_link)]])
                )
                return

            # Step 8: Success message to user
            btn_rows = []
            success_lines = ["✓ <b>Posted successfully</b>", ""]
            if post_url:
                btn_rows.append([InlineKeyboardButton("📣 Open Post", url=post_url)])
                success_lines.append(f"Channel: <a href=\"{post_url}\">Open Post</a>")
            if user_backup_channel_id:
                if user_channel_post_url:
                    btn_rows.append([InlineKeyboardButton("📢 Open Backup Post", url=user_channel_post_url)])
                    success_lines.append(f"Backup: <a href=\"{user_channel_post_url}\">Open Backup Post</a>")
                else:
                    success_lines.append("⚠️ <b>Backup channel post failed</b> — make the bot admin in your backup channel (see /settings).")
            success_markup = InlineKeyboardMarkup(btn_rows) if btn_rows else None
            await status_msg.edit_text(
                "\n".join(success_lines),
                parse_mode="HTML",
                reply_markup=success_markup
            )

    except Exception as e:
        logging.exception(f"Error in automatic post generation: {e}")
        try:
            await status_msg.edit_text(f"❌ <b>Error:</b> {safe_html(str(e))}", parse_mode="HTML")
        except Exception:
            pass
    finally:
        cleanup_temp_files(temp_video, *screenshots)
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
        cleanup_all_temp_dirs()


async def _resolve_link_meta(idx: int, lnk: str, user_id: int, user_key: str | None) -> dict:
    """Phase 0 worker: resolve one link's metadata (GDFLIX + history + poster + stream URL).

    Fully I/O-bound — safe to gather across all links. Never raises; errors
    are returned in the ctx dict so one bad link can't kill the batch.
    """
    ctx: dict = {"idx": idx, "lnk": lnk, "shots": [], "shot_urls": [],
                 "temp_dir": None, "temp_video": None, "dl_ok": False,
                 "formatted_mi": "", "error": None}
    try:
        gdrive_id = extract_file_id(lnk)
        normalized_lnk = normalize_gdrive_link(lnk)
        fn, sz, dl, err, f_id = await gdflix_convert(normalized_lnk, user_key)
        if not fn or not dl:
            ctx["error"] = err or "GDFLIX convert failed"
            logging.warning(f"Batch link {lnk} failed: {ctx['error']}")
            return ctx
        await save_history(user_id, fn, sz, dl)
        clean_title, p_url = await get_clean_title_and_poster(fn)
        stream_url = await get_media_stream_url(dl, f_id or gdrive_id)
        ctx.update({"filename": fn, "size": sz, "gdflix_link": dl,
                    "gdrive_id": gdrive_id, "f_id": f_id,
                    "clean_title": clean_title, "poster_url": p_url,
                    "stream_url": stream_url})
    except Exception as e:
        ctx["error"] = str(e)
        logging.warning(f"Batch resolve error for {lnk}: {e}")
    return ctx


async def _download_link_chunk(ctx: dict, chunk_bytes: int = 10 * 1024 * 1024) -> dict:
    """Phase 1 worker: download one link's 10MB chunk to a UNIQUE temp path.

    Each link gets its own mkdtemp dir + indexed filename so concurrent
    tasks never overwrite each other. Never raises.
    """
    if ctx.get("error"):
        return ctx
    try:
        temp_dir = tempfile.mkdtemp(prefix=f"gdflix_batch_{ctx['idx']}_")
        temp_video = os.path.join(temp_dir, f"sample_{ctx['idx']}.mkv")
        ctx["temp_dir"] = temp_dir
        ctx["temp_video"] = temp_video
        f_id, gdrive_id = ctx.get("f_id"), ctx.get("gdrive_id")
        stream_url = ctx.get("stream_url")

        spidy_ddl = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={f_id or gdrive_id}" if (f_id or gdrive_id) else None
        dl_ok = False
        first_url = (spidy_ddl if is_archive_filename(ctx.get("filename")) and spidy_ddl else stream_url) or spidy_ddl
        if first_url:
            try:
                dl_ok = await download_media_chunk(first_url, temp_video, max_bytes=chunk_bytes, timeout=15.0)
            except Exception as e_dl:
                logging.warning(f"Batch chunk download error: {e_dl}")
                dl_ok = os.path.exists(temp_video) and os.path.getsize(temp_video) >= 512 * 1024
        if not dl_ok:
            for fb in [stream_url, spidy_ddl, f"https://drive.google.com/uc?id={f_id or gdrive_id}&export=download" if (f_id or gdrive_id) else None]:
                if fb and fb != first_url:
                    try:
                        dl_ok = await download_media_chunk(fb, temp_video, max_bytes=chunk_bytes, timeout=15.0)
                        if dl_ok:
                            break
                    except Exception:
                        dl_ok = os.path.exists(temp_video) and os.path.getsize(temp_video) >= 512 * 1024
                        if dl_ok:
                            break
        ctx["dl_ok"] = dl_ok
    except Exception as e:
        ctx["error"] = str(e)
        logging.warning(f"Batch download worker error: {e}")
    return ctx


def extract_episode_identifier(filename: str | None) -> str | None:
    """Extract episode token like E01, E02 from a filename."""
    if not filename:
        return None
    fn = str(filename).strip()
    # 1. S01E02, S1E2, s01e02
    m = re.search(r'[._\s\[\(]S\d{1,2}[._\s-]*E(\d{1,3})[._\s\]\)]', fn, re.IGNORECASE)
    if m:
        return f"E{int(m.group(1)):02d}"
    # 2. 1x02, 01x02
    m = re.search(r'[._\s\[\(]\d{1,2}x(\d{1,3})[._\s\]\)]', fn, re.IGNORECASE)
    if m:
        return f"E{int(m.group(1)):02d}"
    # 3. EP01, EP.01, Episode 01, Ep 1
    m = re.search(r'[._\s\[\(](?:EP?|Episode)[._\s-]*(\d{1,3})[._\s\]\)]', fn, re.IGNORECASE)
    if m:
        return f"E{int(m.group(1)):02d}"
    # 4. E01 at start or delimited
    m = re.search(r'(?:^|[._\s\[\(-])E(\d{1,3})(?:[._\s\]\)-]|$)', fn, re.IGNORECASE)
    if m:
        return f"E{int(m.group(1)):02d}"
    return None


def is_series_batch(ctxs: list[dict]) -> bool:
    """True if batch contains at least 2 distinct episodes (TV Series), False for Movie qualities."""
    ep_set = set()
    for c in ctxs:
        fn = c.get("filename") or ""
        ep = extract_episode_identifier(fn)
        if ep:
            ep_set.add(ep)
    return len(ep_set) >= 2


async def _heavy_link_work(ctx: dict, sem: asyncio.Semaphore, cached_screenshots: list[str], is_series: bool = False) -> dict:
    """Phase 2 worker: MediaInfo (local chunk) + Screenshots, then chunk cleanup.

    Runs under the shared Semaphore(2).
    - Movie mode: 4 HD master frames are simply copied into this link's temp_dir.
    - Series mode: each episode extracts its own unique 4 HD screenshots via MrSpidy DDL!
    The 10MB chunk is deleted BEFORE this returns. Never raises.
    """
    if ctx.get("error") or not ctx.get("dl_ok"):
        return ctx
    try:
        async with sem:
            temp_video = ctx["temp_video"]
            temp_dir = ctx["temp_dir"]

            def _parse_sz(s):
                try:
                    s = s.strip().upper().replace(",", ".")
                    if "GB" in s: return int(float(s.replace("GB", "").strip()) * 1024**3)
                    elif "MB" in s: return int(float(s.replace("MB", "").strip()) * 1024**2)
                    elif "KB" in s: return int(float(s.replace("KB", "").strip()) * 1024)
                except Exception: pass
                return 0
            real_sz_bytes = _parse_sz(ctx.get("size") or "")

            archive_entries = []
            if is_archive_filename(ctx.get("filename")):
                _bzid = ctx.get("f_id") or ctx.get("gdrive_id")
                spidy_url = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={_bzid}" if _bzid else ""
                arch_url = spidy_url or ctx.get("stream_url") or ""
                if arch_url:
                    try:
                        archive_entries = await asyncio.to_thread(get_zip_video_entries_sync, arch_url)
                    except Exception as e_ar:
                        logging.debug(f"Batch zip entries error: {e_ar}")
            ctx["archive_entries"] = archive_entries

            try:
                info_json = await extract_mediainfo_from_file(temp_video)
                if info_json:
                    ctx["formatted_mi"] = format_mediainfo_text(
                        info_json,
                        filename=ctx["filename"],
                        fallback_size=ctx["size"],
                        total_bytes=real_sz_bytes,
                        archive_entries=archive_entries
                    )
            except Exception as e_mi:
                logging.warning(f"Batch mediainfo error for {ctx.get('filename')}: {e_mi}")

            # HD shot handling:
            if cached_screenshots:
                # Movie mode: reuse the 4 master frames across all links
                try:
                    copied = []
                    for k, src_shot in enumerate(cached_screenshots[:4], 1):
                        dst = os.path.join(temp_dir, f"screenshot_{k}.jpg")
                        shutil.copyfile(src_shot, dst)
                        copied.append(dst)
                    ctx["shots"] = copied
                except Exception as e_cp:
                    logging.warning(f"Batch shot copy error for {ctx.get('filename')}: {e_cp}")
                    ctx["shots"] = []
            elif is_series:
                # Series mode: extract unique screenshots for this episode!
                _bzid = ctx.get("f_id") or ctx.get("gdrive_id")
                spidy_url = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={_bzid}" if _bzid else None
                src_stream = (spidy_url if is_archive_filename(ctx.get("filename")) and spidy_url else ctx.get("stream_url")) or spidy_url
                if src_stream:
                    try:
                        _zcands = [spidy_url, f"https://drive.google.com/uc?id={_bzid}&export=download"] if _bzid else []
                        shots = await extract_screenshots_smart(ctx.get("filename") or "", src_stream, _zcands, temp_dir)
                        ctx["shots"] = shots or []
                    except Exception as e_ep_shot:
                        logging.warning(f"Episode shot extraction failed for {ctx.get('filename')}: {e_ep_shot}")
                        ctx["shots"] = []

            # Aggressive cleanup: chunk dies HERE, before uploads + posting.
            cleanup_temp_files(temp_video)
            ctx["temp_video"] = None
            gc.collect()
    except Exception as e:
        ctx["error"] = ctx.get("error") or str(e)
        logging.warning(f"Batch heavy worker error: {e}")
        try:
            if ctx.get("temp_video"):
                cleanup_temp_files(ctx["temp_video"])
                ctx["temp_video"] = None
        except Exception:
            pass
    return ctx


async def process_and_send_links(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    links: list
):
    """Parallel batch pipeline with HD screenshot caching.

    - Phase 0: metadata for ALL links via one asyncio.gather (I/O-bound).
    - Phase 1: 10MB chunks for ALL links via one asyncio.gather (unique paths).
    - Phase 1.5: extract 4 HD shots ONCE (first link, fallback to next) via
      generate_local_screenshots_pyav into master_temp_dir.
    - Phase 2: MediaInfo + HD shot COPY via gather under Semaphore(2); each
      chunk is deleted immediately after its own work finishes.
    - Phase 3: ALL screenshots uploaded simultaneously via one gather.
    - Phase 4: group by title and publish sequentially (rate-limit safe).
    - Finally: master_temp_dir deleted ONLY after all posts are done.
    """
    user_id = update.effective_user.id

    expanded_links = []
    for lnk in links:
        folder_id = extract_folder_id(lnk)
        if folder_id:
            folder_files = await get_files_from_folder(folder_id)
            if folder_files:
                expanded_links.extend(folder_files)
        else:
            expanded_links.append(lnk)

    links = expanded_links

    if not links:
        await update.message.reply_text("❌ Folder mein koi file nahi mili ya Drive link invalid hai.")
        return

    # If single link, process directly
    if len(links) == 1:
        await process_single_drive_link(update, context, links[0], user_id)
        return

    # Multiple links: fully parallel batch pipeline
    status_msg = await update.message.reply_text(
        f"⏳ <b>Processing {len(links)} files for channel post...</b>",
        parse_mode="HTML"
    )

    processed_items = []
    all_batch_temp_dirs = []
    all_batch_temp_files = []
    heavy_sem = asyncio.Semaphore(2)  # 512MB RAM: max 2 heavy workers at once

    try:
        user_key = await get_user_key(user_id)

        # ── Phase 0: resolve metadata for ALL links in parallel ──
        await status_msg.edit_text(
            f"⏳ <b>Resolving {len(links)} links...</b>",
            parse_mode="HTML"
        )
        ctxs = await asyncio.gather(
            *[_resolve_link_meta(idx, lnk, user_id, user_key) for idx, lnk in enumerate(links)]
        )
        ctxs = [c for c in ctxs if not c.get("error")]
        if not ctxs:
            await status_msg.edit_text("❌ <b>Koi bhi file process nahi ho payi.</b>", parse_mode="HTML")
            return

        # ── Phase 1: download ALL 10MB chunks in parallel (unique paths) ──
        await status_msg.edit_text(
            f"⏳ <b>Downloading {len(ctxs)} chunks in parallel...</b>",
            parse_mode="HTML"
        )
        ctxs = await asyncio.gather(*[_download_link_chunk(c) for c in ctxs])
        for c in ctxs:
            if c.get("temp_dir"):
                all_batch_temp_dirs.append(c["temp_dir"])
            if c.get("temp_video"):
                all_batch_temp_files.append(c["temp_video"])
        ctxs = [c for c in ctxs if c.get("dl_ok")]
        if not ctxs:
            await status_msg.edit_text("❌ <b>Koi bhi file process nahi ho payi.</b>", parse_mode="HTML")
            return

        series_mode = is_series_batch(ctxs)
        master_temp_dir = tempfile.mkdtemp(prefix="gdflix_master_")
        all_batch_temp_dirs.append(master_temp_dir)
        cached_screenshots: list[str] = []

        if not series_mode:
            # ── MOVIE MODE: Extract 4 HD shots ONCE (first link wins, next links copy) ──
            await status_msg.edit_text(
                f"⏳ <b>Extracting HD screenshots for Movie (once)...</b>",
                parse_mode="HTML"
            )
            best_try: list[str] = []
            best_try_dir: str | None = None
            for c in ctxs:
                if cached_screenshots:
                    break
                _bzid = c.get("f_id") or c.get("gdrive_id")
                spidy_url = f"https://ddl-by.mrspidy.eu.org/direct.aspx?id={_bzid}" if _bzid else None
                # For archives/ZIP, prioritize spidy_url directly; for videos use stream_url or spidy_url
                src_stream = (spidy_url if is_archive_filename(c.get("filename")) and spidy_url else c.get("stream_url")) or spidy_url
                if not src_stream:
                    continue
                attempt_dir = tempfile.mkdtemp(prefix="gdflix_master_try_")
                try:
                    # Archives (.zip): frames come from the embedded video, same
                    # 4-shot output as video posts.
                    _zcands = [spidy_url, f"https://drive.google.com/uc?id={_bzid}&export=download"] if _bzid else []
                    shots = await extract_screenshots_smart(c.get("filename") or "", src_stream, _zcands, attempt_dir)
                except Exception as e_m:
                    logging.warning(f"Master shot attempt failed: {e_m}")
                    shots = []
                if len(shots) >= 4:
                    # Promote: move the 4 HD frames into master_temp_dir
                    cached_screenshots = []
                    for k, s in enumerate(shots[:4], 1):
                        dst = os.path.join(master_temp_dir, f"master_{k}.jpg")
                        try:
                            os.replace(s, dst)
                            cached_screenshots.append(dst)
                        except Exception:
                            if os.path.exists(s):
                                cached_screenshots.append(s)
                    if best_try_dir:
                        shutil.rmtree(best_try_dir, ignore_errors=True)
                        best_try, best_try_dir = [], None
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                else:
                    if len(shots) > len(best_try):
                        if best_try_dir:
                            shutil.rmtree(best_try_dir, ignore_errors=True)
                        best_try, best_try_dir = list(shots), attempt_dir
                    else:
                        shutil.rmtree(attempt_dir, ignore_errors=True)
            if not cached_screenshots and best_try:
                # No link yielded 4 — reuse the best partial attempt as-is
                logging.warning(f"Master HD extraction partial ({len(best_try)}/4), reusing best attempt")
                cached_screenshots = []
                for k, s in enumerate(best_try, 1):
                    dst = os.path.join(master_temp_dir, f"master_{k}.jpg")
                    try:
                        os.replace(s, dst)
                        cached_screenshots.append(dst)
                    except Exception:
                        if os.path.exists(s):
                            cached_screenshots.append(s)
                if best_try_dir:
                    shutil.rmtree(best_try_dir, ignore_errors=True)
            if not cached_screenshots:
                logging.warning("Master HD extraction yielded 0 frames — batch posts go without shots")
        else:
            # ── SERIES MODE: Each episode gets its own distinct screenshots! ──
            await status_msg.edit_text(
                f"⏳ <b>Series detected: Extracting individual screenshots for {len(ctxs)} episodes...</b>",
                parse_mode="HTML"
            )

        # ── Phase 2: MediaInfo + Screenshots in parallel under Semaphore(2) ──
        # Each worker deletes its own chunk before returning.
        await status_msg.edit_text(
            f"⏳ <b>Processing MediaInfo & Screenshots for {len(ctxs)} files...</b>",
            parse_mode="HTML"
        )
        ctxs = await asyncio.gather(*[_heavy_link_work(c, heavy_sem, cached_screenshots, is_series=series_mode) for c in ctxs])
        for c in ctxs:
            for s in c.get("shots", []):
                all_batch_temp_files.append(s)

        # ── Phase 3: upload ALL screenshots simultaneously ──
        await status_msg.edit_text(
            f"⏳ <b>Uploading screenshots...</b>",
            parse_mode="HTML"
        )
        upload_chat = ADMIN_ID or POST_CHANNEL_ID or CHANNEL_ID
        flat_shots: list[tuple[int, str]] = []
        for ci, c in enumerate(ctxs):
            for s in c.get("shots", []):
                flat_shots.append((ci, s))
        if flat_shots:
            up_results = await asyncio.gather(
                *[get_telegram_file_id(BOT_TOKEN, upload_chat, s) for _, s in flat_shots],
                return_exceptions=True
            )
            for (ci, _), fid in zip(flat_shots, up_results):
                if isinstance(fid, str) and fid:
                    ctxs[ci]["shot_urls"].append(fid)

        # ── Phase 4: build items (with MediaInfo fallback) + group + publish ──
        for c in ctxs:
            formatted_mi = c.get("formatted_mi") or ""
            if not formatted_mi:
                if is_archive_filename(c.get("filename")):
                    formatted_mi = format_archive_mediainfo_fallback(c.get("filename"), c.get("size"), archive_entries=c.get("archive_entries"))
                else:
                    clean_fn = (c.get("filename") or "Media File").replace("_", ".")
                    formatted_mi = (
                        f"Filename  : {clean_fn}\n"
                        f"Format    : Matroska\n"
                        f"Size      : {c.get('size') or 'N/A'}\n"
                        f"Duration  : N/A\n"
                        f"Bitrate   : N/A\n\n"
                        "VIDEO\n"
                        "Format    : HEVC\n"
                        "Resolution: N/A\n"
                        "Bit depth : 8 bits\n"
                        "FPS       : 24.000 FPS\n\n"
                        "AUDIO\n"
                        "[Hindi] Dolby Digital Plus"
                    )
            processed_items.append({
                "filename": c["filename"],
                "size": c["size"],
                "gdflix_link": c["gdflix_link"],
                "formatted_mediainfo": formatted_mi,
                "screenshots": c.get("shots", []),
                "screenshot_urls": c.get("shot_urls", []),
                "poster_url": c.get("poster_url"),
                "title_group": (c.get("clean_title") or c["filename"])
            })

        if not processed_items:
            await status_msg.edit_text("❌ <b>Koi bhi file process nahi ho payi.</b>", parse_mode="HTML")
            return

        # Group items by title_group
        from collections import defaultdict
        groups = defaultdict(list)
        for itm in processed_items:
            groups[itm["title_group"]].append(itm)

        target_channel = POST_CHANNEL_ID or CHANNEL_ID
        is_user_admin = await is_admin(user_id)
        published_links = []
        backup_published_links = []
        backup_channel_error = None
        try:
            user_backup_channel_raw = await db.get_user_backup_channel(user_id)
        except Exception:
            user_backup_channel_raw = None
        user_backup_channel = resolve_channel_id_for_api(user_backup_channel_raw) if user_backup_channel_raw else None
        is_group = update.effective_chat.type in ("group", "supergroup") or (update.effective_chat.id and update.effective_chat.id < 0)

        # Telegram character/block limits: max 4 items per post ensures 10, 16, or more links never crash
        MAX_ITEMS_PER_POST = 4

        should_post_to_main = is_user_admin and target_channel and not user_backup_channel

        for grp_title, group_items in groups.items():
            best_poster = next((x["poster_url"] for x in group_items if x.get("poster_url")), None)
            chunks = [group_items[i:i + MAX_ITEMS_PER_POST] for i in range(0, len(group_items), MAX_ITEMS_PER_POST)]

            for chunk_items in chunks:
                # 1. Publish to main owner channel ONLY if user is owner/admin and has NO custom backup channel
                if should_post_to_main:
                    msg_id, post_url, err = await publish_channel_post(
                        context=context,
                        channel_id=target_channel,
                        poster_url=best_poster,
                        items=chunk_items,
                        user_id=user_id
                    )
                    if post_url:
                        published_links.append(post_url)
                    await asyncio.sleep(1.0)

                # 1b. Publish to user's own backup channel (if set in /settings)
                if user_backup_channel:
                    try:
                        _, ub_url, ub_err = await publish_channel_post(
                            context=context,
                            channel_id=user_backup_channel,
                            poster_url=best_poster,
                            items=chunk_items,
                            user_id=user_id
                        )
                        if ub_url:
                            backup_published_links.append(ub_url)
                        elif ub_err:
                            backup_channel_error = ub_err
                    except Exception as e_ub:
                        backup_channel_error = str(e_ub)
                        logging.warning(f"User backup-channel batch post failed for {user_id}: {e_ub}")
                    await asyncio.sleep(1.0)

                # 2. If link was sent in a group: also post directly in the group!
                if is_group and (not target_channel or str(target_channel) != str(update.effective_chat.id)):
                    await publish_channel_post(
                        context=context,
                        channel_id=update.effective_chat.id,
                        poster_url=best_poster,
                        items=chunk_items,
                        user_id=user_id
                    )
                    await asyncio.sleep(1.0)
                elif not is_group and not is_user_admin and not user_backup_channel:
                    # Regular user in PM without custom backup channel: send directly to user's PM!
                    await publish_channel_post(
                        context=context,
                        channel_id=update.effective_chat.id,
                        poster_url=best_poster,
                        items=chunk_items,
                        user_id=user_id
                    )
                    await asyncio.sleep(1.0)

        if is_group or (not is_user_admin and not user_backup_channel):
            try:
                await status_msg.delete()
            except Exception:
                pass
        elif published_links or backup_published_links:
            kbs = []
            row = []
            for i, u in enumerate(published_links):
                row.append(InlineKeyboardButton(f"📣 Post #{i+1}", url=u))
                if len(row) == 2:
                    kbs.append(row)
                    row = []
            if row:
                kbs.append(row)
            brow = []
            for i, u in enumerate(backup_published_links):
                brow.append(InlineKeyboardButton(f"📢 Backup #{i+1}", url=u))
                if len(brow) == 2:
                    kbs.append(brow)
                    brow = []
            if brow:
                kbs.append(brow)
            done_text = f"✓ <b>Posted {len(processed_items)} files successfully across {len(published_links) + len(backup_published_links)} posts!</b>"
            if user_backup_channel:
                if backup_published_links:
                    done_text += f"\n📢 <b>Your channel:</b> {len(backup_published_links)} posts done (owner channel skipped)."
                else:
                    done_text += f"\n⚠️ <b>Backup channel post failed</b> — make the bot admin in your backup channel (see /settings)."
            await status_msg.edit_text(
                done_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kbs) if kbs else None
            )
        else:
            await status_msg.edit_text("❌ <b>Channel publish failed. Check bot channel admin permissions.</b>", parse_mode="HTML")

    except Exception as e:
        logging.exception(f"Error in batch link processing: {e}")
        try:
            await status_msg.edit_text(f"❌ <b>Error:</b> {safe_html(str(e))}", parse_mode="HTML")
        except Exception:
            pass
    finally:
        for f in all_batch_temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        for d in all_batch_temp_dirs:
            try:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        cleanup_all_temp_dirs()


async def channel_post_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle interactive inline buttons (ℹ MediaInfo, 🖼 Screenshots) on channel posts"""
    query = update.callback_query
    data = query.data or ""

    if data.startswith("cmi:"):
        key = data.split(":", 1)[1]
        item = channel_post_cache.get(key)
        if item and item.get("mediainfo"):
            mi_text = item["mediainfo"]
            fn = item.get("filename", "Media")
            await query.answer()
            try:
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=f"ℹ <b>MediaInfo for {safe_html(fn)}:</b>\n\n<pre>{mi_text}</pre>",
                    parse_mode="HTML"
                )
            except Exception:
                await query.answer(mi_text[:190], show_alert=True)
        else:
            await query.answer("MediaInfo not found or expired.", show_alert=True)

    elif data.startswith("css:"):
        key = data.split(":", 1)[1]
        item = channel_post_cache.get(key)
        if item and item.get("screenshots"):
            await query.answer("Sending screenshots...")
            try:
                shots = item["screenshots"]
                media_group = [
                    InputMediaPhoto(media=b, caption=f"🖼 Screenshot #{idx+1}" if idx == 0 else None)
                    for idx, b in enumerate(shots)
                ]
                await context.bot.send_media_group(chat_id=query.from_user.id, media=media_group)
            except Exception:
                await query.answer("Please start the bot in private chat to receive screenshots.", show_alert=True)
        else:
            await query.answer("Screenshots not found or expired.", show_alert=True)


async def gdflix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_allowed(user_id, chat_id):
        await update.message.reply_text("Access denied.")
        return

    full_text = update.message.text or ""
    parts = full_text.split(None, 1)
    after_cmd = parts[1] if len(parts) > 1 else ""
    links = extract_links(after_cmd)

    if not links:
        msg = "Usage:\n\nSingle:\n/gdflix https://drive.google.com/...\n\nMultiple:\n/gdflix\nhttps://drive.google.com/...\nhttps://drive.google.com/..."
        await update.message.reply_text(msg)
        return

    await process_and_send_links(update, context, links)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_allowed(user_id, chat_id):
        return

    text = update.message.text or update.message.caption or ""
    # If user tapped "Set Backup Channel" in /settings, treat next message as channel ID
    if context.user_data.get("awaiting_backup_channel"):
        if extract_links(text):
            # User sent a Drive link instead — cancel channel setup and process link normally
            context.user_data.pop("awaiting_backup_channel", None)
        else:
            await save_backup_channel_from_input(update, context, text.strip())
            return

    # If user tapped "Set Backup Tag" in /settings, treat next message as custom tag/link
    if context.user_data.get("awaiting_backup_tag"):
        if extract_links(text) and "drive.google.com" in text:
            # User sent a Drive link instead — cancel tag setup and process link normally
            context.user_data.pop("awaiting_backup_tag", None)
        else:
            await save_backup_tag_from_input(update, context, text.strip())
            return
    links = extract_links(text)
    if not links:
        if getattr(update.message, "forward_origin", None):
            await update.message.reply_text("📥 Forwarded post received. Please send/paste a Google Drive link to generate an automatic channel post!")
        return

    await process_and_send_links(update, context, links)


# ── Other handlers (unchanged) ───────────────────────────────────────────────

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_allowed(user_id, chat_id):
        await update.message.reply_text("Access denied.")
        return
    history = await db.get_history(user_id)
    if not history:
        await update.message.reply_text("No history found.")
        return
    lines = []
    for i, item in enumerate(history, 1):
        lines.append(str(i) + ". " + safe_html(item["filename"]) + " - [" + item["size"] + "]\n" + item["link"] + "\n" + item["time"])
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_allowed(user_id, chat_id):
        await update.message.reply_text("🚫 Access denied.")
        return
    stats = await db.get_stats()
    total = stats.get("total", 0) if stats else 0
    user_count = len(stats.get("users", {})) if stats else 0
    my_count = stats.get("users", {}).get(str(user_id), 0) if stats else 0

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>GDFlix Stats</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 <b>Total Conversions:</b> <code>{total}</code>\n"
        f"👥 <b>Total Users:</b> <code>{user_count}</code>\n"
        f"⚡ <b>Your Conversions:</b> <code>{my_count}</code>\n"
    )
    if await is_admin(user_id):
        text += (
            f"\n👑 <b>Admin Panel:</b>\n"
            f"└ 🤖 Bot is admin-only mode\n"
        )
    text += f"\n━━━━━━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(text, parse_mode="HTML")

async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("Usage: /setkey YOUR_GDFLIX_API_KEY")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()
    if not query_text or "drive.google.com" not in query_text:
        return
    user_id = update.inline_query.from_user.id
    filename, size, link, error, gdrive_id = await gdflix_convert(query_text, await get_user_key(user_id))
    if filename:
        result_text = format_batch_response([format_response(filename, size, link)])
        backup = await get_backup(user_id)
        if backup:
            result_text += f"\n\n<tg-emoji emoji-id=\"{CROWN_EMOJI_ID}\">👑</tg-emoji> <b>Back-Up:</b> <b>{backup}</b> <tg-emoji emoji-id=\"{SPARKLE_EMOJI_ID}\">✨</tg-emoji>"
        results = [InlineQueryResultArticle(id="1", title=filename, description=size, input_message_content=InputTextMessageContent(result_text, parse_mode="HTML"))]
    else:
        results = [InlineQueryResultArticle(id="1", title="Error", description=error, input_message_content=InputTextMessageContent("Error: " + error))]
    await update.inline_query.answer(results, cache_time=10)

async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("Usage: /setkey YOUR_GDFLIX_API_KEY")
        return
    await set_user_key(user_id, context.args[0].strip())
    await update.message.reply_text("Key saved! To remove: /removekey")

async def mykey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    k = await get_user_key(user_id)
    if k != key:
        masked = k[:4] + "****" + k[-4:] if len(k) > 8 else "****"
        await update.message.reply_text(f"Saved key: {masked}\nTo remove: /removekey")
    else:
        await update.message.reply_text("No personal key saved. Default is being used.")

async def removekey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    await remove_user_key(user_id)
    await update.message.reply_text("Key removed.")


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only the owner can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin USER_ID")
        return
    try:
        new_uid = int(context.args[0])
        await db.add_admin(new_uid)
        await update.message.reply_text(f"✅ Admin added: <code>{new_uid}</code>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Invalid User ID.")

async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only the owner can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin USER_ID")
        return
    try:
        rem_uid = int(context.args[0])
        await db.remove_admin(rem_uid)
        await update.message.reply_text(f"✅ Admin removed: <code>{rem_uid}</code>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Invalid User ID.")

async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only the owner can use this command.")
        return
    admins = await db.get_admins()
    if admins:
        text = "👑 <b>Admins List:</b>\n\n"
        text += f"🏠 Owner: <code>{ADMIN_ID}</code>\n"
        for a in admins:
            text += f"👤 Admin: <code>{a}</code>\n"
    else:
        text = "👑 <b>Admins List:</b>\n\n"
        text += f"🏠 Owner: <code>{ADMIN_ID}</code>\n"
        text += "\n_Koi admin nahi added._"
    await update.message.reply_text(text, parse_mode="HTML")


async def defaultbackup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id): return
    current = await db.is_default_backup_enabled()
    if not context.args:
        status = "ON" if current else "OFF"
        await update.message.reply_text(f"Default Backup: {status}\nUsage: /defaultbackup on/off")
        return
    arg = context.args[0].lower()
    if arg == "on":
        await db.config.update_one({"_id": "settings"}, {"$set": {"default_backup_enabled": True}})
        await update.message.reply_text("Default backup ON!")
    elif arg == "off":
        await db.config.update_one({"_id": "settings"}, {"$set": {"default_backup_enabled": False}})
        await update.message.reply_text("Default backup OFF!")

async def poster_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("🔍 Usage: /poster <movie or show name>\n\nExample: /poster Avengers")
        return
    query = " ".join(context.args).strip()
    searching = await update.message.reply_text(f"⏳ Searching poster for: <b>{query}</b>...", parse_mode="HTML")
    posters = await get_multiple_posters(query)
    await searching.delete()
    if not posters:
        await update.message.reply_text(f"❌ No posters found for: <b>{query}</b>", parse_mode="HTML")
        return
    context.user_data["poster_results"] = posters
    context.user_data["final_msg"] = None
    context.user_data["raw_filename"] = query
    current = posters[0]
    api_ott = current.get("ott", "")
    detected_ott = api_ott or detect_ott_from_filename(query)
    card_lines = [f"🎬 <b>{safe_html(current['title'])}</b>", ""]
    card_lines.append(f"✦ 📅 <b>Year :</b> {current.get('year') or 'N/A'}")
    card_lines.append(f"✦ 📺 <b>Platform :</b> {detected_ott or '🌐 Web'}")
    card_lines.append(f"✦ 🖼 <b>Type :</b> Landscape")
    card_lines.append(f"\n⚡ <i>Powered by @HrXRiPS</i>")
    caption = "\n".join(card_lines)
    await update.message.reply_photo(
        photo=current["poster"],
        caption=caption,
        parse_mode="HTML",
        reply_markup=build_poster_keyboard(
            user_id=user_id,
            index=0,
            total=len(posters),
            poster_url=current["poster"]
        )
    )

async def setbackup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    if not context.args:
        current_backup = await db.get_user_backup(user_id)
        curr_text = f"<code>{current_backup}</code>" if current_backup else "<i>Default backup (HrXRiPS.t.me)</i>"
        await update.message.reply_text(
            f"📌 <b>Backup Channel Settings:</b>\n"
            f"Current Backup: {curr_text}\n\n"
            f"<b>Usage:</b> <code>/setbackup yourchannel.t.me</code>\n"
            f"<b>Or:</b> <code>/setbackup https://t.me/yourchannel</code>\n"
            f"<b>Or:</b> <code>/setbackup @yourchannel</code>\n\n"
            f"Yeh link aapke har post ke niche <b>✦ Back-Up: <u>channel.t.me</u> ✦</b> ban kar aayega.",
            parse_mode="HTML"
        )
        return
    backup = " ".join(context.args).strip()
    await db.set_user_backup(user_id, backup)
    disp, url = format_backup_link(backup)
    await update.message.reply_text(
        f"✅ <b>Backup link successfully updated!</b>\n\n"
        f"<b>Post Preview:</b>\n"
        f"✦ Back-Up: <a href=\"{url}\"><b><u>{disp}</u></b></a> ✦\n\n"
        f"Ab aapke saare channel posts ke niche yeh backup link aayega.",
        parse_mode="HTML"
    )

async def removebackup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id): return
    await db.remove_user_backup(user_id)
    await update.message.reply_text("✅ Custom backup removed. Default backup will be used if enabled.")


# ── User Backup-Channel + Backup-Tag Settings (/settings) ────────────────────

async def build_settings_text_and_markup(user_id: int):
    """User-friendly settings text + status + action buttons."""
    try:
        tag_enabled = await db.is_backup_tag_enabled(user_id)
    except Exception:
        tag_enabled = True
    try:
        backup_channel = await db.get_user_backup_channel(user_id)
    except Exception:
        backup_channel = None
    try:
        backup_tag = await db.get_user_backup(user_id)
    except Exception:
        backup_tag = None

    if backup_tag:
        disp, _ = format_backup_link(backup_tag)
        tag_text_value = disp
    else:
        try:
            if await is_admin(user_id) and await db.is_default_backup_enabled():
                tag_text_value = f"{DEFAULT_BACKUP} (default)"
            else:
                tag_text_value = "Not set"
        except Exception:
            tag_text_value = "Not set"

    channel_value = f"<code>{safe_html(str(backup_channel))}</code>" if backup_channel else "<i>Not set</i>"

    text = (
        "⚙️ <b>Post Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Control what happens with your posts:\n\n"
        "1️⃣ <b>Backup Tag (Footer Link)</b>\n"
        "A clickable link shown at the bottom of your posts:\n"
        "<code>✦ Back-Up: yourchannel.t.me ✦</code>\n"
        "Tap <b>✏️ Set Tag</b> to set your own channel or invite link.\n\n"
        "2️⃣ <b>Custom Backup Channel (Auto-Post)</b>\n"
        "Every post will go <b>only</b> to your own private/public channel (owner channel is completely skipped!).\n"
        "Steps: Add this bot as <b>admin</b> in your channel, "
        "then tap <b>Set Backup Channel</b> and send the channel ID "
        "(example: <code>-1001234567890</code> or <code>@yourchannel</code>).\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>Backup Tag:</b> {'✅ ON' if tag_enabled else '❌ OFF'} (<code>{safe_html(tag_text_value)}</code>)\n"
        f"📢 <b>Backup Channel:</b> {channel_value}\n"
    )

    tag_btn_text = f"🏷 Tag: {'✅ ON' if tag_enabled else '❌ OFF'}"
    channel_btn_text = "📢 Backup Channel: ✅ Set" if backup_channel else "📢 Set Backup Channel"

    keyboard = [
        [
            InlineKeyboardButton(tag_btn_text, callback_data="sett_tag_toggle"),
            InlineKeyboardButton("✏️ Set Tag", callback_data="sett_setbackuptag")
        ]
    ]
    if backup_tag:
        keyboard.append([InlineKeyboardButton("🗑 Remove Custom Tag", callback_data="sett_removebackuptag")])
    keyboard.append([InlineKeyboardButton(channel_btn_text, callback_data="sett_setchannel")])
    if backup_channel:
        keyboard.append([InlineKeyboardButton("🗑 Remove Backup Channel", callback_data="sett_removechannel")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="sett_close")])

    markup = InlineKeyboardMarkup(keyboard)
    return text, markup


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_allowed(user_id, update.effective_chat.id):
        await update.message.reply_text("Access denied.")
        return
    context.user_data.pop("awaiting_backup_channel", None)
    context.user_data.pop("awaiting_backup_tag", None)
    text, markup = await build_settings_text_and_markup(user_id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user_id = update.effective_user.id

    if data == "sett_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    if data == "sett_tag_toggle":
        try:
            current = await db.is_backup_tag_enabled(user_id)
        except Exception:
            current = True
        await db.set_backup_tag_enabled(user_id, not current)
        await query.answer(f"Backup tag {'OFF' if current else 'ON'}")
        try:
            text, markup = await build_settings_text_and_markup(user_id)
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "sett_setbackuptag":
        context.user_data["awaiting_backup_tag"] = True
        context.user_data.pop("awaiting_backup_channel", None)
        await query.answer()
        try:
            await query.message.reply_text(
                "✏️ <b>Send your custom Backup Tag / Link</b>\n\n"
                "Examples:\n"
                "• <code>yourchannel.t.me</code>\n"
                "• <code>https://t.me/yourchannel</code>\n"
                "• <code>@yourchannel</code>\n\n"
                "Yeh text/link aapke posts ke footer mein <b>✦ Back-Up: ... ✦</b> ban kar aayega.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data == "sett_removebackuptag":
        await db.remove_user_backup(user_id)
        context.user_data.pop("awaiting_backup_tag", None)
        await query.answer("Custom backup tag removed")
        try:
            text, markup = await build_settings_text_and_markup(user_id)
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "sett_setchannel":
        context.user_data["awaiting_backup_channel"] = True
        context.user_data.pop("awaiting_backup_tag", None)
        await query.answer()
        try:
            await query.message.reply_text(
                "📢 <b>Send your backup channel ID</b>\n\n"
                "Example: <code>-1001234567890</code> or <code>@yourchannel</code>\n\n"
                "⚠️ First add this bot as <b>admin</b> in that channel, "
                "otherwise auto-post will fail.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data == "sett_removechannel":
        await db.remove_user_backup_channel(user_id)
        context.user_data.pop("awaiting_backup_channel", None)
        await query.answer("Backup channel removed")
        try:
            text, markup = await build_settings_text_and_markup(user_id)
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    await query.answer()


async def save_backup_tag_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
    """Validate & save backup tag text sent after tapping 'Set Tag'. Returns True if handled."""
    user_id = update.effective_user.id
    tag = raw_text.strip()
    if not tag:
        await update.message.reply_text("❌ Empty backup tag. Please send e.g. <code>channel.t.me</code> or <code>@yourchannel</code>.", parse_mode="HTML")
        return True

    await db.set_user_backup(user_id, tag)
    await db.set_backup_tag_enabled(user_id, True)
    context.user_data.pop("awaiting_backup_tag", None)

    disp, url = format_backup_link(tag)
    await update.message.reply_text(
        f"✅ <b>Backup tag successfully updated!</b>\n\n"
        f"<b>Post Preview:</b>\n"
        f"✦ Back-Up: <a href=\"{url}\"><b><u>{disp}</u></b></a> ✦\n\n"
        f"Ab aapke saare posts ke niche yeh backup link aayega.\n"
        f"Use /settings to change or remove it anytime.",
        parse_mode="HTML"
    )
    return True


async def save_backup_channel_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
    """Validate channel ID sent after tapping 'Set Backup Channel'. Returns True if handled."""
    user_id = update.effective_user.id
    normalized = normalize_backup_channel_input(raw_text)
    if not normalized:
        await update.message.reply_text("❌ Empty channel ID. Please send like <code>-1001234567890</code> or <code>@yourchannel</code>.", parse_mode="HTML")
        return True
    if "/+" in normalized or "joinchat" in normalized:
        await update.message.reply_text(
            "❌ Private invite links (<code>t.me/+...</code>) can't be used directly.\n"
            "Add the bot as admin, then send the numeric channel ID (<code>-100...</code>). "
            "You can get it by forwarding a channel message to @userinfobot.",
            parse_mode="HTML"
        )
        return True
    api_id = resolve_channel_id_for_api(normalized)
    # Verify bot can see the channel
    try:
        chat = await context.bot.get_chat(api_id)
    except Exception as e:
        logging.warning(f"Backup channel verify failed for {user_id} ({normalized}): {e}")
        await update.message.reply_text(
            "❌ <b>Channel not found / bot has no access.</b>\n\n"
            "1. Add this bot to your channel as <b>admin</b>.\n"
            "2. Then send the ID again (example: <code>-1001234567890</code> or <code>@yourchannel</code>).",
            parse_mode="HTML"
        )
        return True
    # Warn if bot is not admin (still save so user can fix without re-typing)
    admin_warning = ""
    try:
        bot_me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, bot_me.id)
        if getattr(member, "status", "") not in ("administrator", "creator"):
            admin_warning = "\n\n⚠️ Bot is <b>not admin</b> in that channel yet — auto-post will fail until you promote it."
    except Exception:
        admin_warning = "\n\n⚠️ Could not verify admin rights — please make sure the bot is admin."
    try:
        await db.set_user_backup_channel(user_id, str(chat.id))
    except Exception as e:
        await update.message.reply_text(f"❌ Save failed: {safe_html(str(e))}")
        return True
    context.user_data.pop("awaiting_backup_channel", None)
    await update.message.reply_text(
        f"✅ <b>Backup channel saved:</b> <code>{chat.id}</code>"
        f"{' (' + safe_html(chat.title) + ')' if getattr(chat, 'title', None) else ''}"
        f"\n\nEvery new post will now go <b>only</b> to your channel (owner channel skipped).{admin_warning}"
        f"\n\nUse /settings to change it anytime.",
        parse_mode="HTML"
    )
    return True


# ── Koyeb Health Check Server ────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress health check logs


def start_health_server():
    port = int(os.environ.get("PORT", 8000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logging.info(f"Health server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logging.warning(f"Health server could not start on port {port} (safe to ignore on local): {e}")



async def clearcache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    folder_cache.clear()
    gdflix_cache.clear()
    await update.message.reply_text("✅ Cache cleared! Fresh data will be fetched now.")

async def secrets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ADMIN_ID only: dump Koyeb secret KEYS (no values) as a paste-ready block.

    Keys are parsed live from config.py's environ.get("KEY") calls so the
    list never drifts out of sync. Values are intentionally left blank —
    live secrets must never sit in chat history.
    """
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg_src = f.read()
        keys = re.findall(r'environ\.get\(\s*"([A-Z0-9_]+)"', cfg_src)
        ordered: list[str] = []
        for k in keys:
            if k not in ordered:
                ordered.append(k)
        if "PORT" not in ordered:
            ordered.append("PORT")  # Koyeb health server (bot.py), not in config.py
        lines = [f"{k}=" for k in ordered]
        await update.message.reply_text(
            "<b>Koyeb Secrets (fill values, paste into dashboard):</b>\n"
            f"<pre>{html.escape(chr(10).join(lines))}</pre>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to read secrets: {safe_html(str(e))}")

def main():
    # Koyeb health check ke liye background mein HTTP server chalaao
    threading.Thread(target=start_health_server, daemon=True).start()

    # Clear residual temporary files on startup
    cleanup_all_temp_dirs()

    app = Application.builder().token(BOT_TOKEN).build()

    async def post_init(app):
        await db.init_db()
        # Clear old webhook/session — Koyeb rolling deploy conflict fix
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.warning(f"Webhook delete failed (ignoreable): {e}")
        await app.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("help", "How to use"),
            BotCommand("gdflix", "Convert Drive links"),
            BotCommand("poster", "Search poster by name"),
            BotCommand("history", "Last 10 links"),
            BotCommand("stats", "Usage stats"),
            BotCommand("settings", "Backup tag + backup channel"),
            BotCommand("setkey", "Save GDFlix API key"),
            BotCommand("mykey", "Check saved key"),
            BotCommand("removekey", "Remove key"),
            BotCommand("setbackup", "Set backup link"),
            BotCommand("removebackup", "Remove backup link"),
            BotCommand("addadmin", "Add admin (owner only)"),
            BotCommand("removeadmin", "Remove admin (owner only)"),
            BotCommand("listadmins", "List all admins (owner only)"),
            BotCommand("clearcache", "Clear cache - fresh data"),
            BotCommand("secrets", "Koyeb secrets list (owner only)"),
        ])

        # Start pyrogram client — for colored buttons
        # Yeh background mein update receive karega aur entities cache karega
        try:
            tc = await get_pyro_client()
            if tc:
                logging.info("Pyrogram colored button client ready!")
        except Exception as e:
            logging.warning(f"Pyrogram client init failed: {e}")

    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("gdflix", gdflix_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("setkey", setkey_command))
    app.add_handler(CommandHandler("mykey", mykey_command))
    app.add_handler(CommandHandler("poster", poster_command))
    app.add_handler(CommandHandler("removekey", removekey_command))
    app.add_handler(CommandHandler("setbackup", setbackup_command))
    app.add_handler(CommandHandler("removebackup", removebackup_command))
    app.add_handler(CommandHandler(["settings", "setting"], settings_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("defaultbackup", defaultbackup_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("clearcache", clearcache_command))
    app.add_handler(CommandHandler("secrets", secrets_command))

    app.add_handler(CallbackQueryHandler(poster_callback_handler, pattern=r"^poster_"))
    app.add_handler(CallbackQueryHandler(channel_post_callback_handler, pattern=r"^c(mi|ss):"))
    app.add_handler(CallbackQueryHandler(settings_callback_handler, pattern=r"^sett_"))

    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.FORWARDED) & ~filters.COMMAND, handle_link))

    async def _ignore_conflict(update, context):
        err = getattr(context, "error", None)
        # Conflict = purana + naya Koyeb instance overlap, self-heal hota hai. Spam mat karo.
        if err and "Conflict" in type(err).__name__:
            return
        logging.warning(f"Update error: {err}")

    app.add_error_handler(_ignore_conflict)

    print("GDFlix Bot is running...")
    # Retry on conflict — Koyeb rolling deploy may have both instances running
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "inline_query"],
    )

if __name__ == "__main__":
    main()
