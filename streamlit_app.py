import os
import sys
import time
import asyncio
import logging
import threading
from datetime import datetime
from io import StringIO

try:
    import streamlit as st
except ImportError:
    st = None

# ── Streamlit UI Setup ───────────────────────────────────────────────────────
if st:
    st.set_page_config(
        page_title="GDFlix 2.0 • 4K UHD Bot & Dashboard",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Custom styling — dark modern glassmorphic look
    st.markdown("""
    <style>
        .main-header {
            font-size: 2.2rem;
            font-weight: 800;
            background: linear-gradient(135deg, #00F2FE 0%, #4FACFE 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.2rem;
        }
        .sub-header {
            color: #94A3B8;
            font-size: 1.05rem;
            margin-bottom: 1.5rem;
        }
        .stat-card {
            background: rgba(30, 41, 59, 0.7);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 16px 20px;
            backdrop-filter: blur(10px);
            margin-bottom: 12px;
        }
        .stat-title {
            color: #94A3B8;
            font-size: 0.85rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-value {
            color: #F8FAFC;
            font-size: 1.6rem;
            font-weight: 700;
            margin-top: 4px;
        }
        .badge-4k {
            background: linear-gradient(135deg, #FF416C, #FF4B2B);
            color: white;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: bold;
            display: inline-block;
        }
        .badge-online {
            background: rgba(16, 185, 129, 0.2);
            border: 1px solid #10B981;
            color: #10B981;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
        }
    </style>
    """, unsafe_allow_html=True)


# ── In-Memory Log Capture ────────────────────────────────────────────────────
class InMemoryLogBuffer:
    def __init__(self, max_lines=200):
        self.max_lines = max_lines
        self.logs = []
        self.lock = threading.Lock()

    def write(self, message):
        if not message or not str(message).strip():
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message.strip()}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > self.max_lines:
                self.logs.pop(0)

    def get_logs(self):
        with self.lock:
            return list(self.logs)


LOG_BUFFER = InMemoryLogBuffer(max_lines=300)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.write(msg)
        except Exception:
            pass


# Hook into root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
root_logger.addHandler(buffer_handler)


# ── Background Telegram Bot Thread (Streamlit Singleton) ─────────────────────
def _run_bot_safely():
    """Target function for bot worker thread."""
    try:
        from bot import main as start_telegram_bot
        LOG_BUFFER.write("Starting GDFlix 2.0 Telegram Bot polling...")
        start_telegram_bot()
    except Exception as e:
        LOG_BUFFER.write(f"Telegram Bot error: {e}")
        logging.exception(f"Telegram bot crashed in thread: {e}")


def init_bot_service():
    """Initialize bot thread once using streamlit cache_resource or global lock."""
    if st:
        @st.cache_resource
        def _get_cached_bot():
            t = threading.Thread(target=_run_bot_safely, name="TelegramBotWorker", daemon=True)
            t.start()
            return {"thread": t, "start_time": datetime.now()}
        return _get_cached_bot()
    else:
        t = threading.Thread(target=_run_bot_safely, name="TelegramBotWorker", daemon=True)
        t.start()
        return {"thread": t, "start_time": datetime.now()}


# ── 24/7 Keep-Alive Pinger Service ──────────────────────────────────────────
class PingerService:
    def __init__(self, interval_seconds=120):
        self.interval = interval_seconds
        self.ping_url = os.environ.get("APP_URL", os.environ.get("PING_URL", ""))
        try:
            import streamlit as st
            if hasattr(st, "secrets"):
                if "APP_URL" in st.secrets:
                    self.ping_url = str(st.secrets["APP_URL"]).strip()
                elif "PING_URL" in st.secrets:
                    self.ping_url = str(st.secrets["PING_URL"]).strip()
        except Exception:
            pass
        self.total_pings = 0
        self.successful_pings = 0
        self.last_ping_time = None
        self.last_status = "Waiting for first ping..."
        self.last_latency_ms = 0
        self.history = []  # list of dicts: {"time": str, "target": str, "status": str, "latency_ms": int, "ok": bool}
        self.running = False
        self.thread = None
        self.lock = threading.Lock()

    def set_url(self, url: str):
        with self.lock:
            self.ping_url = url.strip()

    def set_interval(self, secs: int):
        with self.lock:
            self.interval = max(30, int(secs))

    def _record_result(self, target: str, status: str, latency: int, ok: bool):
        with self.lock:
            self.total_pings += 1
            if ok:
                self.successful_pings += 1
            self.last_ping_time = datetime.now()
            self.last_status = status
            self.last_latency_ms = latency
            self.history.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "target": target,
                "status": status,
                "latency_ms": latency,
                "ok": ok
            })
            if len(self.history) > 30:
                self.history.pop(0)

    def ping_target(self, client, target: str) -> tuple[bool, str, int]:
        t0 = time.time()
        try:
            r = client.get(target, timeout=12.0)
            latency = int((time.time() - t0) * 1000)
            status_str = f"HTTP {r.status_code} OK"
            return True, status_str, latency
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return False, f"Err: {e}", latency

    def ping_once(self) -> tuple[bool, str]:
        import httpx
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            # 1. Target URL or local health
            if self.ping_url:
                target = self.ping_url.rstrip("/")
                health_target = f"{target}/_stcore/health"
                ok, st_msg, lat = self.ping_target(client, health_target)
                if not ok:
                    ok, st_msg, lat = self.ping_target(client, target)
            else:
                target = "http://localhost:8501/_stcore/health"
                ok, st_msg, lat = self.ping_target(client, target)

            self._record_result(target, st_msg, lat, ok)
            if ok:
                LOG_BUFFER.write(f"🟢 [24/7 Pinger] Alive -> {target} [{st_msg}, {lat}ms]")
            else:
                LOG_BUFFER.write(f"⚠️ [24/7 Pinger] Notice for {target}: {st_msg}")

            # 2. Telegram Bot Keepalive
            try:
                from config import BOT_TOKEN
                if BOT_TOKEN:
                    ok_tg, msg_tg, lat_tg = self.ping_target(client, f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
                    self._record_result("Telegram Bot API", msg_tg, lat_tg, ok_tg)
            except Exception:
                pass

            return ok, f"{st_msg} ({lat}ms)"

    def _loop(self):
        time.sleep(10)  # Wait for startup
        while self.running:
            try:
                self.ping_once()
            except Exception as e:
                LOG_BUFFER.write(f"❌ [24/7 Pinger] Error in loop: {e}")
            time.sleep(self.interval)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="KeepAlivePingerWorker", daemon=True)
        self.thread.start()
        LOG_BUFFER.write(f"🚀 [24/7 Pinger] Started automatic keepalive thread (interval: {self.interval}s)")


def init_pinger_service():
    """Singleton pinger service cached across Streamlit reruns."""
    if st:
        @st.cache_resource
        def _get_cached_pinger():
            p = PingerService(interval_seconds=120)
            p.start()
            return p
        return _get_cached_pinger()
    else:
        p = PingerService(interval_seconds=120)
        p.start()
        return p


# ── Async Helper ─────────────────────────────────────────────────────────────
def run_coro(coro):
    """Run an async coroutine inside synchronous Streamlit context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Main Streamlit Dashboard ─────────────────────────────────────────────────
def main():
    if not st:
        print("Streamlit not found. Running bot directly...")
        _run_bot_safely()
        return

    # Start bot in background (only once)
    bot_info = init_bot_service()
    bot_thread = bot_info["thread"]
    start_time = bot_info["start_time"]
    uptime = datetime.now() - start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    # Start 24/7 pinger in background (only once)
    pinger = init_pinger_service()

    # Header
    col_t1, col_t2 = st.columns([3, 1])
    with col_t1:
        st.markdown('<div class="main-header">⚡ GDFlix 2.0 • 4K UHD Bot</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sub-header">Automated Google Drive to GDFLIX MediaInfo & 4K Screenshot Publisher on Streamlit Cloud</div>',
            unsafe_allow_html=True
        )
    with col_t2:
        st.write("")
        if bot_thread.is_alive():
            st.markdown('<div class="badge-online">🟢 BOT ONLINE (4K Active)</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div style="color:#EF4444; font-weight:bold;">🔴 BOT STOPPED</div>', unsafe_allow_html=True)
        st.caption(f"Uptime: {uptime_str}")

    # Top Metrics Row
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown("""
        <div class="stat-card">
            <div class="stat-title">Platform</div>
            <div class="stat-value">Streamlit.app</div>
            <span class="badge-4k">1GB+ RAM Free</span>
        </div>
        """, unsafe_allow_html=True)
    with m2:
        st.markdown("""
        <div class="stat-card">
            <div class="stat-title">Screenshot Quality</div>
            <div class="stat-value">4K UHD</div>
            <span style="color:#10B981; font-size:0.8rem; font-weight:600;">3840×2160 Native</span>
        </div>
        """, unsafe_allow_html=True)
    with m3:
        st.markdown("""
        <div class="stat-card">
            <div class="stat-title">Extractor Engine</div>
            <div class="stat-value">PyAV Fast</div>
            <span style="color:#60A5FA; font-size:0.8rem; font-weight:600;">0.2s I-Frame Grab</span>
        </div>
        """, unsafe_allow_html=True)
    with m4:
        p_last = pinger.last_ping_time.strftime("%H:%M:%S") if pinger.last_ping_time else "Starting..."
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-title">24/7 Keep-Alive</div>
            <div class="stat-value">{pinger.total_pings} Pings</div>
            <span style="color:#10B981; font-size:0.8rem; font-weight:600;">🟢 Active ({p_last})</span>
        </div>
        """, unsafe_allow_html=True)

    st.divider()

    # Sidebar: Configurations & Controls
    with st.sidebar:
        st.markdown("### 📡 24/7 Keep-Alive Pinger")
        app_url_val = pinger.ping_url or ""
        new_url = st.text_input(
            "Your Streamlit App URL:",
            value=app_url_val,
            placeholder="https://your-bot.streamlit.app",
            help="Enter your public Streamlit URL to keep the app awake 24/7."
        )
        if new_url and new_url != pinger.ping_url:
            pinger.set_url(new_url)
            st.success("Ping URL saved!")

        cp1, cp2 = st.columns(2)
        with cp1:
            if st.button("🚀 Ping Now", use_container_width=True):
                ok, res = pinger.ping_once()
                if ok:
                    st.toast(f"Ping OK: {res}", icon="🟢")
                else:
                    st.toast(f"Ping result: {res}", icon="ℹ️")
        with cp2:
            st.caption(f"Status: `{pinger.last_status}`")

        st.divider()
        st.markdown("### ⚙️ Bot Settings")
        from config import ADMIN_ID, POST_CHANNEL_ID, GDFLIX_HOST, DEFAULT_BACKUP
        st.markdown(f"**Bot Owner ID:** `{ADMIN_ID}`")
        st.markdown(f"**Main Channel:** `{POST_CHANNEL_ID}`")
        st.markdown(f"**GDFlix Host:** `{GDFLIX_HOST}`")
        st.markdown(f"**Default Backup:** `{DEFAULT_BACKUP}`")

        st.divider()
        st.markdown("### 📱 Telegram Commands")
        st.markdown("""
        - `/start` - Start bot
        - `/settings` - ✏️ Set Backup Tag & Channel
        - `/poster <name>` - Search custom poster
        - `/gdflix <link>` - Convert Drive links
        - `/stats` - View conversion statistics
        - `/mykey` / `/setkey` - Personal API Key
        """)

        st.divider()
        if st.button("🔄 Refresh Dashboard", use_container_width=True):
            st.rerun()

    # Tabs: [1. Web Converter & Preview, 2. Live Logs, 3. 24/7 Pinger, 4. Database Stats]
    tab_convert, tab_logs, tab_pinger, tab_stats = st.tabs([
        "🎬 Web Converter & 4K Preview",
        "📋 Live Bot Logs",
        "📡 24/7 Keep-Alive Pinger",
        "📊 Database & User Stats"
    ])

    with tab_convert:
        st.markdown("#### Test or Convert Google Drive Link via Web Dashboard")
        st.caption("You can paste any Drive link or folder URL here to extract MediaInfo & generate 4K screenshots directly on Streamlit!")

        with st.form("web_converter_form"):
            gdrive_input = st.text_input(
                "Google Drive URL (File or Folder):",
                placeholder="https://drive.google.com/file/d/.../view or folder link"
            )
            custom_key = st.text_input(
                "Optional Custom GDFlix API Key (Leave empty to use bot default):",
                type="password"
            )
            submitted = st.form_submit_button("⚡ Convert & Extract 4K MediaInfo", use_container_width=True)

        if submitted and gdrive_input.strip():
            from bot import gdflix_convert, get_folder_details, extract_folder_id
            from media_utils import (
                extract_mediainfo_from_file,
                format_mediainfo_text,
                download_media_chunk,
                generate_local_screenshots_pyav,
                cleanup_temp_files,
                SPIDY_DDL_API
            )
            import tempfile

            folder_id = extract_folder_id(gdrive_input.strip())
            with st.spinner("⏳ Contacting GDFlix & Extracting 4K Media..."):
                try:
                    if folder_id:
                        folder_name, folder_size, f_links, _ = run_coro(get_folder_details(folder_id))
                        st.success(f"📂 Folder Detected: **{folder_name}** ({len(f_links)} files, Total {folder_size})")
                        target_link = f_links[0] if f_links else None
                    else:
                        target_link = gdrive_input.strip()

                    if target_link:
                        fn, sz, dl, err, gid = run_coro(gdflix_convert(target_link, custom_key or None))
                        if err or not fn:
                            st.error(f"❌ Error from GDFlix: {err or 'Unknown error'}")
                        else:
                            st.markdown(f"### 🎬 {fn}")
                            c1, c2 = st.columns([1, 1])
                            with c1:
                                st.info(f"**Size:** `{sz}`")
                            with c2:
                                st.link_button("▶ Open GDFLIX Direct Link", dl, use_container_width=True)

                            # MediaInfo & 4K Screenshots
                            spidy_url = f"{SPIDY_DDL_API}{gid}"
                            temp_dir = tempfile.mkdtemp(prefix="st_4k_")
                            sample_file = os.path.join(temp_dir, "sample.mkv")

                            with st.spinner("Extracting 10MB chunk for MediaInfo..."):
                                ok = run_coro(download_media_chunk(spidy_url, sample_file, max_bytes=10*1024*1024, timeout=15.0))
                                if ok and os.path.exists(sample_file):
                                    info_json = run_coro(extract_mediainfo_from_file(sample_file))
                                    cleanup_temp_files(sample_file)
                                    if info_json:
                                        mi_txt = format_mediainfo_text(info_json, filename=fn, fallback_size=sz)
                                        with st.expander("📄 View Formatted MediaInfo", expanded=True):
                                            st.code(mi_txt, language="yaml")

                            with st.spinner("Capturing 4K Native Screenshots via PyAV..."):
                                shots = run_coro(generate_local_screenshots_pyav(spidy_url, temp_dir))
                                if shots:
                                    st.markdown(f"#### 🖼 4K Screenshots ({len(shots)})")
                                    sc_cols = st.columns(min(len(shots), 4))
                                    for idx, s_path in enumerate(shots):
                                        with sc_cols[idx % 4]:
                                            st.image(s_path, caption=f"4K Frame #{idx+1}", use_container_width=True)
                                else:
                                    st.warning("Screenshots could not be grabbed from this stream.")
                except Exception as e_web:
                    st.error(f"Processing error: {e_web}")

    with tab_logs:
        st.markdown("#### 📋 Real-Time Bot Logs")
        st.caption("Live streaming logs from your Telegram bot instance on Streamlit Cloud:")
        log_entries = LOG_BUFFER.get_logs()
        if log_entries:
            st.code("\n".join(log_entries[-100:]), language="log")
        else:
            st.info("No logs captured yet.")
        if st.button("🔄 Refresh Logs"):
            st.rerun()

    with tab_pinger:
        st.markdown("#### 📡 24/7 Keep-Alive Pinger Status & Control")
        st.caption("Ensures your Telegram bot and Streamlit instance stay awake 24/7 without cold shutdowns.")

        pk1, pk2, pk3, pk4 = st.columns(4)
        with pk1:
            st.metric("Total Keep-Alive Pings", pinger.total_pings)
        with pk2:
            st.metric("Successful Pings", pinger.successful_pings)
        with pk3:
            st.metric("Last Latency", f"{pinger.last_latency_ms} ms")
        with pk4:
            last_t = pinger.last_ping_time.strftime("%H:%M:%S") if pinger.last_ping_time else "Never"
            st.metric("Last Pulse Time", last_t)

        st.markdown("---")

        col_cfg1, col_cfg2 = st.columns([2, 1])
        with col_cfg1:
            st.markdown("##### 🎯 Pinger Target Configuration")
            cur_url = pinger.ping_url or ""
            input_url = st.text_input(
                "Public Streamlit App URL:",
                value=cur_url,
                placeholder="https://your-bot.streamlit.app",
                key="tab_pinger_url_input"
            )
            if input_url != pinger.ping_url:
                pinger.set_url(input_url)
                st.success("Target URL updated!")

            st.caption("When configured, the keepalive pulse pings both your public Streamlit URL (`/_stcore/health`) and the Telegram Bot API (`getMe`) automatically every 2 minutes.")

        with col_cfg2:
            st.markdown("##### ⚡ Manual Pulse Test")
            st.write("")
            if st.button("🚀 Trigger Instant Ping", key="tab_ping_now_btn", use_container_width=True):
                with st.spinner("Pinging target endpoints..."):
                    ok, res = pinger.ping_once()
                    if ok:
                        st.success(f"Success: {res}")
                    else:
                        st.warning(f"Result: {res}")
            st.caption(f"Current Status: `{pinger.last_status}`")

        st.markdown("---")
        st.markdown("##### 📋 Recent Keepalive Pulses")
        if pinger.history:
            try:
                import pandas as pd
                df_history = pd.DataFrame(pinger.history[::-1])
                st.dataframe(df_history, use_container_width=True, hide_index=True)
            except Exception:
                for h in pinger.history[-10:]:
                    st.write(f"`[{h['time']}]` **{h['target']}** ➔ `{h['status']}` ({h['latency_ms']}ms)")
        else:
            st.info("Pinger starting up... First pulse will appear shortly.")

        with st.expander("💡 100% Free 24/7 Guarantee Tips (External Monitors)", expanded=True):
            st.markdown("""
            While this built-in pinger keeps the internal thread and outbound Telegram connection alive, 
            Streamlit Community Cloud also monitors **incoming external HTTP traffic**.
            
            To guarantee 100% continuous uptime 365 days a year for free:
            1. **Cron-job.org / UptimeRobot (Recommended & 100% Free)**:
               - Create a free account at [cron-job.org](https://cron-job.org) or [uptimerobot.com](https://uptimerobot.com).
               - Add an HTTP monitor targeting: `https://your-bot.streamlit.app/_stcore/health`
               - Set interval to **every 5 minutes**.
            2. **GitHub Actions Automated Cron (`.github/workflows/keepalive.yml`)**:
               - We've included `.github/workflows/keepalive.yml` in this repository.
               - In GitHub, add repo secret `APP_URL` with your Streamlit URL, and GitHub will automatically ping it every 10 minutes!
            3. **Local / VPS CLI (`pinger.py`)**:
               - Run `python pinger.py --url https://your-bot.streamlit.app` from any machine or terminal.
            """)

    with tab_stats:
        st.markdown("#### 📊 Database & Stats Overview")
        try:
            from database import db
            stats = run_coro(db.get_stats())
            if stats:
                st.json(stats)
            else:
                st.info("MongoDB connected. Stats collection is initialized.")
        except Exception as e_db:
            st.warning(f"Could not load stats: {e_db}")


if __name__ == "__main__":
    main()
