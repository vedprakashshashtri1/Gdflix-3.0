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

# ── Base Variables ─────────────
GDFLIX_HOST = get_cfg("GDFLIX_HOST", "gdflix.dev")
DATA_FILE = "data.json"

# ── Telegram Bot Credentials ─────────────
BOT_TOKEN = get_cfg("BOT_TOKEN", "8869321920:AAGYnU_y3WmYczaM9YI1oglWhu2w-8dUhoc")
ADMIN_ID = int(get_cfg("ADMIN_ID", "7383334104"))
CHANNEL_ID = get_cfg("CHANNEL_ID", "-1003294369676")
POST_CHANNEL_ID = get_cfg("POST_CHANNEL_ID", "-1003294369676")
DEFAULT_BACKUP = get_cfg("DEFAULT_BACKUP", "HrXRiPS.t.me")

# ── API Keys ──────────────────────────────
GDFLIX_KEY = get_cfg("GDFLIX_KEY", "dbca760c94dd6409f722d866688b8f19")
SPIDY_DDL_API = "https://ddl-by.mrspidy.eu.org/direct.aspx?id="
SPIDY_API_KEY = get_cfg("SPIDY_API_KEY", "spidy_27uwrd4n6ob")
POSTER_API_URL = get_cfg("POSTER_API_URL", "https://poster-api.ispidy.com")
GDRIVE_API_KEY = get_cfg("GDRIVE_API_KEY", "AIzaSyCduQptn1zUQY-0X-Lvyv0dnU9nMaI6ISw")
MONGODB_URI = get_cfg("MONGODB_URI", "mongodb+srv://rama69787:rama69787@cluster0.dgspwp1.mongodb.net/?appName=Cluster0")
TMDB_API_KEY = get_cfg("TMDB_API_KEY", "2dca580c2a14b55200e784d157207b4d")

# ── Pyrogram Session (Optional) ───────────
API_ID = int(get_cfg("API_ID", "22092321"))
API_HASH = get_cfg("API_HASH", "41422d801af88573fbee711e2c08f744")
SESSION_STRING = get_cfg("SESSION_STRING", "")

# ── Premium Custom Emojis ─────────────────
AUDIO_EMOJI_ID = get_cfg("AUDIO_EMOJI_ID", "5388632425314140043")
CROWN_EMOJI_ID = get_cfg("CROWN_EMOJI_ID", "5217822164362739968")
SPARKLE_EMOJI_ID = get_cfg("SPARKLE_EMOJI_ID", "5325547803936572038")
SEASON_STICKER_ID = get_cfg("SEASON_STICKER_ID", "5217822164362739968")

# ── 24/7 Keep-Alive Pinger URL ────────────
APP_URL = get_cfg("APP_URL", "https://gdflix-30-6lkzgpuwpdoxf5gczft4kv.streamlit.app/")
