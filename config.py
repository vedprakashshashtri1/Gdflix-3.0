from os import environ

def get_cfg(key_name, default=""):
    val = environ.get(key_name)
    if val:
        return val
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key_name in st.secrets:
            return str(st.secrets[key_name])
    except Exception:
        pass
    return default

GDFLIX_HOST = "gdflix.dev"
key = get_cfg("GDFLIX_KEY", "")
BOT_TOKEN = get_cfg("BOT_TOKEN", "")
ADMIN_ID = int(get_cfg("ADMIN_ID", "7383334104"))
CHANNEL_ID = "-1003294369676"
POST_CHANNEL_ID = get_cfg("POST_CHANNEL_ID", get_cfg("CHANNEL_ID", "-1003294369676"))

DATA_FILE = "data.json"

SPIDY_DDL_API = "https://ddl-by.mrspidy.eu.org/direct.aspx?id="
SPIDY_API_KEY = get_cfg("SPIDY_API_KEY", "")
POSTER_API_URL = get_cfg("POSTER_API_URL", "https://poster-api.ispidy.com")
GDRIVE_API_KEY = get_cfg("GDRIVE_API_KEY", "")
MONGODB_URI = get_cfg("MONGODB_URI", "")

# TMDB API — for OTT/Watch Providers (free, get from tmdb.org)
TMDB_API_KEY = get_cfg("TMDB_API_KEY", "")

# Pyrogram — Premium account session (for premium emojis)
API_ID = int(get_cfg("API_ID", "22092321"))          # Get from my.telegram.org
API_HASH = get_cfg("API_HASH", "")             # Get from my.telegram.org
SESSION_STRING = get_cfg("SESSION_STRING", "") # Pyrogram session string

# Premium Telegram Emoji IDs
AUDIO_EMOJI_ID = get_cfg("AUDIO_EMOJI_ID", "5388632425314140043")       # 🔊 Audio Tracks
CROWN_EMOJI_ID = get_cfg("CROWN_EMOJI_ID", "5217822164362739968")       # 👑 Back-Up
SPARKLE_EMOJI_ID = get_cfg("SPARKLE_EMOJI_ID", "5325547803936572038")   # ✨ Sparkle
SEASON_STICKER_ID = get_cfg("SEASON_STICKER_ID", "5217822164362739968")

# 24/7 Keep-Alive Pinger URL (e.g. https://your-bot.streamlit.app)
APP_URL = get_cfg("APP_URL", "")


