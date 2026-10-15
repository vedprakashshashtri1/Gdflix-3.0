"""
GDFlix 2.0 • 24/7 Keep-Alive Pinger Service
============================================
Keeps your Streamlit App and Telegram Bot alive 24/7 continuously.

Usage:
    python pinger.py --url https://your-app.streamlit.app --interval 120
    python pinger.py (reads APP_URL / PING_URL from secrets or environment)
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime

try:
    import httpx
except ImportError:
    import requests as httpx  # fallback if httpx is missing

# Setup colorful & clear logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("Pinger")


def get_configured_url():
    """Attempt to find app url from env or secrets.toml."""
    # 1. Check environment variables
    for key in ("APP_URL", "PING_URL", "STREAMLIT_URL", "RENDER_EXTERNAL_URL"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()

    # 2. Check .streamlit/secrets.toml
    secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("APP_URL") or line.startswith("PING_URL"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            if val and not val.startswith("your-"):
                                return val
        except Exception:
            pass

    return ""


def get_telegram_token():
    """Attempt to retrieve Telegram bot token for API keepalive."""
    token = os.environ.get("BOT_TOKEN")
    if token:
        return token.strip()
    try:
        from config import BOT_TOKEN
        if BOT_TOKEN:
            return BOT_TOKEN
    except Exception:
        pass
    return ""


def ping_endpoint(client, url, label=""):
    """Ping a specific URL and return (success, status_or_error)."""
    try:
        r = client.get(url, timeout=12.0)
        status = getattr(r, "status_code", 200)
        if status < 400:
            logger.info(f"🟢 [{label}] Ping SUCCESS -> {url} [HTTP {status}]")
            return True, f"HTTP {status}"
        else:
            logger.warning(f"⚠️ [{label}] Ping returned status {status} -> {url}")
            return False, f"HTTP {status}"
    except Exception as e:
        logger.error(f"❌ [{label}] Ping FAILED -> {url} ({e})")
        return False, str(e)


def run_pinger(target_url: str, interval: int = 120, ping_tg: bool = True):
    """Main keep-alive loop running every `interval` seconds."""
    logger.info("=" * 60)
    logger.info("⚡ GDFlix 2.0 • 24/7 Keep-Alive Pinger Initialized")
    logger.info(f"🎯 Target URL : {target_url or 'http://localhost:8501 (Local Mode)'}")
    logger.info(f"⏱️ Interval   : {interval} seconds")
    logger.info("=" * 60)

    tg_token = get_telegram_token() if ping_tg else ""
    ping_count = 0

    with httpx.Client(follow_redirects=True) if hasattr(httpx, "Client") else None as client:
        # If httpx fallback is used:
        http_agent = client if client is not None else httpx

        while True:
            ping_count += 1
            logger.info(f"--- Keep-Alive Pulse #{ping_count} ---")

            # 1. Ping the main target URL
            if target_url:
                clean_url = target_url.rstrip("/")
                # Ping health endpoint if Streamlit
                if "streamlit.app" in clean_url or "localhost" in clean_url:
                    health_url = f"{clean_url}/_stcore/health"
                    ping_endpoint(http_agent, health_url, label="App Health")
                ping_endpoint(http_agent, clean_url, label="Main Web")
            else:
                # Local fallback
                ping_endpoint(http_agent, "http://localhost:8501/_stcore/health", label="Local Health")

            # 2. Ping Telegram Bot API getMe (keeps outbound connection warm)
            if tg_token:
                tg_url = f"https://api.telegram.org/bot{tg_token}/getMe"
                ping_endpoint(http_agent, tg_url, label="Telegram Bot API")

            logger.info(f"⏳ Sleeping for {interval}s until next keepalive pulse...")
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="GDFlix 2.0 24/7 Keep-Alive Pinger")
    parser.add_argument("--url", type=str, default="", help="Your public Streamlit or Web App URL")
    parser.add_argument("--interval", type=int, default=120, help="Ping interval in seconds (default: 120s)")
    parser.add_argument("--no-tg", action="store_true", help="Disable Telegram API keepalive")
    args = parser.parse_args()

    target_url = args.url or get_configured_url()
    run_pinger(target_url, interval=args.interval, ping_tg=not args.no_tg)


if __name__ == "__main__":
    main()
