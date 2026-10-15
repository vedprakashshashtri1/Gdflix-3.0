import os
import re
import gc
import json
import socket
import struct
import zlib
import asyncio
import logging
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import httpx
from langcodes import Language, find
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup
from config import SPIDY_DDL_API

# Lazy OpenCV to keep baseline RAM low on 512MB instances (saves ~70MB on startup)
cv2 = None
np = None
FACE_CASCADE = None

LOGGER = logging.getLogger(__name__)

# ── Ultra-fast / OOM-safe tuning (Koyeb 512MB, 4K/HEVC over HTTP) ─────────────
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MEDIAINFO_CHUNK_BYTES = 10 * 1024 * 1024  # 10MB HTTP Range chunk
PYAV_TIMEOUT_OPT = "15000000"  # 15s libavformat timeout (microseconds)
PYAV_OVERALL_TIMEOUT = 90.0  # whole 4-frame job, incl. slow CDN seeks
PYAV_FRACS = (0.20, 0.40, 0.60, 0.80)  # 4 seek points as duration fractions
PYAV_MAX_WIDTH = 3840  # Full 4K Ultra-HD output (Streamlit Cloud 1GB+ RAM)
PYAV_JPEG_QUALITY = 95  # high-quality JPEG output

CUSTOM_LANG_MAP = {
    "hi": "Hindi", "hin": "Hindi",
    "en": "English", "eng": "English",
    "ta": "Tamil", "tam": "Tamil",
    "te": "Telugu", "tel": "Telugu",
    "ml": "Malayalam", "mal": "Malayalam",
    "kn": "Kannada", "kan": "Kannada",
    "bn": "Bengali", "ben": "Bengali",
    "mr": "Marathi", "mar": "Marathi",
    "pa": "Punjabi", "pan": "Punjabi", "pun": "Punjabi",
    "gu": "Gujarati", "guj": "Gujarati",
    "ur": "Urdu", "urd": "Urdu",
    "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fre": "French", "fra": "French",
    "de": "German", "ger": "German", "deu": "German",
    "ja": "Japanese", "jpn": "Japanese", "jap": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese",
    "ru": "Russian", "rus": "Russian",
    "ar": "Arabic", "ara": "Arabic",
    "pt": "Portuguese", "por": "Portuguese",
    "it": "Italian", "ita": "Italian",
    "ass": "Assamese", "npl": "Nepali",
}


def get_clean_language(lang_code: str) -> str:
    """Resolve ISO/language string into 'Language (Language)' format"""
    if not lang_code:
        return "Unknown"
    cleaned = str(lang_code).lower().strip()
    # Check custom map
    if cleaned in CUSTOM_LANG_MAP:
        name = CUSTOM_LANG_MAP[cleaned]
        return f"{name} ({name})"
    # Check langcodes library
    try:
        name = Language.get(cleaned).display_name()
        if not name.startswith("Unknown") and name != "Ipulo":
            return f"{name} ({name})"
    except Exception:
        pass
    try:
        name = find(cleaned).display_name()
        if name:
            return f"{name} ({name})"
    except Exception:
        pass
    if 2 <= len(cleaned) <= 25:
        name = cleaned.title()
        return f"{name} ({name})"
    return lang_code.title()


def get_clean_language_short(lang_code: str) -> str:
    """Resolve ISO/language string into short clean format, e.g. 'English', 'Hindi'"""
    if not lang_code:
        return "English"
    cleaned = str(lang_code).lower().strip()
    if cleaned in CUSTOM_LANG_MAP:
        return CUSTOM_LANG_MAP[cleaned]
    try:
        name = Language.get(cleaned).display_name()
        if not name.startswith("Unknown") and name != "Ipulo":
            return name
    except Exception:
        pass
    try:
        name = find(cleaned).display_name()
        if name:
            return name
    except Exception:
        pass
    if 2 <= len(cleaned) <= 25:
        return cleaned.title()
    return lang_code.title()


def format_pad(label: str, value: str, width: int = 10) -> str:
    """Pad label with spaces so colons align vertically, e.g.:
    Filename  : Formula.1.mkv
    Format    : Matroska
    Resolution: 3840x2160
    """
    if len(label) >= width:
        return f"{label}: {value}"
    spaces = " " * (width - len(label))
    return f"{label}{spaces}: {value}"


def format_human_duration(seconds: float | str | None) -> str:
    """Format duration into human readable format matching e.g. '4 h 0 min', '2 h 15 min'"""
    if not seconds:
        return "N/A"
    try:
        sec = float(seconds)
        hours = int(sec // 3600)
        minutes = int((sec % 3600) // 60)
        secs = int(sec % 60)
        if hours > 0:
            return f"{hours} h {minutes} min"
        elif minutes > 0:
            return f"{minutes} min" if secs == 0 else f"{minutes} min {secs} s"
        else:
            return f"{secs} s"
    except Exception:
        return str(seconds)


def format_bitrate_str(bitrate_val, bitrate_str: str = "") -> str:
    """Format bitrate into '12.0 Mb/s', '1 356 kb/s', '192 kb/s'"""
    if bitrate_str:
        s = str(bitrate_str).strip()
        s = s.replace("Mbps", "Mb/s").replace("kbps", "kb/s").replace("Gbps", "Gb/s")
        return s
    if bitrate_val:
        try:
            bps = float(bitrate_val)
            if bps >= 1_000_000:
                return f"{bps / 1_000_000:.1f} Mb/s"
            elif bps >= 1_000:
                kbps = int(bps // 1000)
                return f"{kbps:,} kb/s".replace(",", " ")
            else:
                return f"{int(bps)} b/s"
        except Exception:
            pass
    return "N/A"


def format_aspect(vid: dict) -> str:
    ar_str = vid.get("DisplayAspectRatio_String")
    if ar_str:
        return ar_str
    w = vid.get("Width")
    h = vid.get("Height")
    if w and h:
        try:
            wf, hf = float(w), float(h)
            ratio = wf / hf
            if abs(ratio - 16 / 9) < 0.05:
                return "16:9"
            elif abs(ratio - 4 / 3) < 0.05:
                return "4:3"
            elif abs(ratio - 2.40) < 0.08 or abs(ratio - 2.39) < 0.08:
                return "2.40:1"
            elif abs(ratio - 1.85) < 0.05:
                return "1.85:1"
            return f"{ratio:.2f}:1"
        except Exception:
            pass
    return vid.get("DisplayAspectRatio", "16:9")


def format_duration(seconds: float | str | None) -> str:
    """Format duration into HH:MM:SS.mmm (legacy helper)"""
    if not seconds:
        return "N/A"
    try:
        sec = float(seconds)
        hours = int(sec // 3600)
        minutes = int((sec % 3600) // 60)
        rem_secs = sec % 60
        return f"{hours:02d}:{minutes:02d}:{rem_secs:06.3f}"
    except Exception:
        return str(seconds)


def extract_stream_url_sync(gdflix_url: str) -> str | None:
    """Scrape direct stream URL from GDFLIX file page using curl_cffi Chrome impersonation"""
    try:
        r = cf_requests.get(gdflix_url, impersonate="chrome", timeout=15.0)
        if r.status_code != 200:
            LOGGER.warning(f"GDFLIX page returned status {r.status_code} for {gdflix_url}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Priority 1: DIRECT SERVER [MGT] (fastest direct server)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "indexserver" in href:
                return href
                
        # Priority 2: R2 cloud direct download
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "r2.dev" in href or "r2." in href:
                return href
                
        # Priority 3: Instant DL / busycdn
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "busycdn" in href or "instant" in href:
                return href

        # Priority 4: Direct media link with video extension
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r'\.(mkv|mp4|avi|mov|ts)(\?.*)?$', href, re.IGNORECASE):
                return href
    except Exception as e:
        LOGGER.warning(f"Failed to scrape stream URL from GDFLIX page: {e}")
    return None


def resolve_final_video_url_sync(url: str) -> str:
    """Follow redirects or extract embedded video URLs (e.g. busycdn -> fastdl-one.pages.dev?url=...)"""
    if not url:
        return url

    # Already a direct stream URL from googleusercontent or indexserver
    if "googleusercontent.com" in url or "indexserver" in url:
        return url
    
    # 1. Check if URL itself has ?url=http... or &url=http...
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "url" in qs and qs["url"][0].startswith("http"):
            LOGGER.info(f"Unpacked real video URL directly from query param: {qs['url'][0][:60]}...")
            return qs["url"][0]
    except Exception:
        pass

    # 2. Follow redirect using HEAD request (NEVER GET to avoid downloading multi-GB files!)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        with httpx.Client(follow_redirects=False, timeout=6.0, headers=headers) as client:
            resp = client.head(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if loc:
                    parsed_loc = urllib.parse.urlparse(loc)
                    loc_qs = urllib.parse.parse_qs(parsed_loc.query)
                    if "url" in loc_qs and loc_qs["url"][0].startswith("http"):
                        real_url = loc_qs["url"][0]
                        LOGGER.info(f"Unpacked real video URL from redirect Location: {real_url[:60]}...")
                        return real_url
                    return loc
    except Exception as e:
        LOGGER.debug(f"Redirect HEAD check failed for {url[:50]}: {e}")

    return url


def is_spidy_url(url: str | None) -> bool:
    """Spidy DDL links screenshots ke liye banned hai."""
    if not url:
        return False
    u = url.lower()
    return ("mrspidy" in u) or ("ddl-by" in u) or ("direct.aspx" in u)


async def get_screenshot_stream_url(gdflix_url: str = "") -> str | None:
    """Screenshots ke liye NON-Spidy source: indexserver > r2 > busycdn. Spidy kabhi nahi."""
    if not gdflix_url:
        return None
    loop = asyncio.get_running_loop()
    stream_url = await loop.run_in_executor(None, extract_stream_url_sync, gdflix_url)
    if stream_url:
        if is_spidy_url(stream_url):
            LOGGER.info("Skipping Spidy URL for screenshots")
            return None
        resolved_url = await loop.run_in_executor(None, resolve_final_video_url_sync, stream_url)
        if is_spidy_url(resolved_url):
            LOGGER.info("Skipping resolved Spidy URL for screenshots")
            return None
        LOGGER.info(f"Screenshot source (non-Spidy): {resolved_url[:60]}...")
        return resolved_url
    return None


async def get_media_stream_url(gdflix_url: str = "", gdrive_id: str | None = None) -> str | None:
    """Resolve high-speed direct media stream URL from Spidy DDL or GDFLIX"""
    loop = asyncio.get_running_loop()

    # 1. Priority #1: Spidy DDL API (Direct high-speed Google Drive stream)
    if gdrive_id and SPIDY_DDL_API:
        ddl_url = f"{SPIDY_DDL_API}{gdrive_id}"
        resolved_ddl = await loop.run_in_executor(None, resolve_final_video_url_sync, ddl_url)
        LOGGER.info(f"Using Spidy DDL API: {resolved_ddl[:60]}...")
        return resolved_ddl

    # 2. Try GDFLIX direct links as fallback
    if gdflix_url:
        stream_url = await loop.run_in_executor(None, extract_stream_url_sync, gdflix_url)
        if stream_url:
            resolved_url = await loop.run_in_executor(None, resolve_final_video_url_sync, stream_url)
            LOGGER.info(f"Resolved direct stream URL from GDFLIX: {resolved_url[:60]}...")
            return resolved_url

    # 3. Direct Google Drive download link fallback
    if gdrive_id:
        return f"https://drive.google.com/uc?id={gdrive_id}&export=download"

    return None


async def download_media_chunk(
    stream_url: str,
    target_path: str,
    max_bytes: int = MEDIAINFO_CHUNK_BYTES,
    timeout: float = 20.0
) -> bool:
    """Download only the first ``max_bytes`` (default 10MB) via HTTP Range.

    Uses ``Range: bytes=0-<max_bytes-1>`` so multi-GB MKVs are never
    fully fetched. Returns True if >=512KB landed on disk.
    """
    loop = asyncio.get_running_loop()
    stream_url = await loop.run_in_executor(None, resolve_final_video_url_sync, stream_url)
    headers = {
        "User-Agent": CHROME_UA,
        "Range": f"bytes=0-{max_bytes - 1}",
        "Accept-Encoding": "identity",
    }
    try:
        to = httpx.Timeout(timeout=timeout, connect=6.0, read=12.0)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=to,
            headers=headers
        ) as client:
            async with client.stream("GET", stream_url) as response:
                if response.status_code not in (200, 206):
                    LOGGER.warning(f"Stream returned status {response.status_code}")
                    return False

                content_type = response.headers.get("content-type", "").lower()
                if "html" in content_type:
                    # Check if response URL or any redirect history contained the embedded url query
                    for r in response.history + [response]:
                        p = urllib.parse.urlparse(str(r.url))
                        q = urllib.parse.parse_qs(p.query)
                        if "url" in q and q["url"][0].startswith("http"):
                            real_url = q["url"][0]
                            LOGGER.info(f"Retrying download with unpacked URL from redirect: {real_url[:60]}...")
                            return await download_media_chunk(real_url, target_path, max_bytes, timeout)
                    LOGGER.warning(f"Stream returned HTML instead of video: {content_type}")
                    return False

                total_downloaded = 0
                os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
                with open(target_path, "wb") as f:
                    try:
                        async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                            f.write(chunk)
                            total_downloaded += len(chunk)
                            if total_downloaded >= max_bytes:
                                break
                    except Exception as e_stream:
                        LOGGER.warning(f"Stream interrupted after {total_downloaded} bytes: {e_stream}")

                LOGGER.info(f"Downloaded {total_downloaded} bytes to {target_path}")
                return total_downloaded >= 512 * 1024
    except Exception as e:
        LOGGER.warning(f"Error downloading media chunk: {e}")
        if os.path.exists(target_path) and os.path.getsize(target_path) >= 512 * 1024:
            return True
        return False


async def download_chunk_for_mediainfo(
    primary_url: str,
    target_path: str,
    fallback_url: str | None = None,
    chunk_bytes: int = MEDIAINFO_CHUNK_BYTES,
    timeout: float = 20.0,
) -> bool:
    """Spec helper: fetch first 10MB via ``Range: bytes=0-10485760``.

    Tries ``primary_url`` first, then ``fallback_url`` (e.g. Spidy DDL)
    without raising — returns False only if both fail.
    """
    for url in [u for u in (primary_url, fallback_url) if u]:
        try:
            ok = await asyncio.wait_for(
                download_media_chunk(url, target_path, max_bytes=chunk_bytes, timeout=timeout),
                timeout=timeout + 5.0,
            )
            if ok:
                return True
        except Exception as e:
            LOGGER.warning(f"MediaInfo chunk fetch failed for {url[:60]}: {e}")
    return False


def detect_real_video_bytes(file_path: str | None = None, info_json: dict | None = None) -> int:
    """Detect real video stream/container byte size even from a 10MB chunk or ZIP header.

    Prevents archive/season-pack size (e.g. 9GB) from inflating single episode bitrate.
    Returns 0 if unknown.
    """
    # 1. From ZIP local file header (if chunk is the start of a stored zip entry)
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                hdr = f.read(1024)
            if len(hdr) >= 30 and hdr[:4] == b"PK\x03\x04":
                fn_len = struct.unpack_from("<H", hdr, 26)[0]
                extra_len = struct.unpack_from("<H", hdr, 28)[0]
                uncomp_sz = struct.unpack_from("<I", hdr, 22)[0]
                if uncomp_sz == 0xFFFFFFFF and len(hdr) >= 30 + fn_len + extra_len:
                    extra = hdr[30 + fn_len : 30 + fn_len + extra_len]
                    p = 0
                    while p + 4 <= len(extra):
                        eid, esz = struct.unpack_from("<HH", extra, p)
                        if eid == 1 and esz >= 8:
                            uncomp_sz = struct.unpack_from("<Q", extra, p + 4)[0]
                            break
                        p += 4 + esz
                if uncomp_sz > 0 and uncomp_sz != 0xFFFFFFFF:
                    return uncomp_sz
        except Exception:
            pass

    # 2. From Matroska EBML Segment size
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                data = f.read(512 * 1024)
            pos = data.find(b"\x18\x53\x80\x67")
            if pos != -1 and pos + 12 <= len(data):
                idx = pos + 4
                first_b = data[idx]
                mask = 0x80
                length = 1
                while length <= 8 and not (first_b & mask):
                    mask >>= 1
                    length += 1
                if length <= 8 and idx + length <= len(data):
                    val = first_b & (mask - 1)
                    for i in range(1, length):
                        val = (val << 8) | data[idx + i]
                    if 0 < val < 0x00FFFFFFFFFFFFFF:
                        return idx + length + val
        except Exception:
            pass

    # 3. From MediaInfo extra / ConformanceErrors ('expected >=1502834319')
    if info_json:
        try:
            tracks = info_json.get("media", {}).get("track", [])
            for t in tracks:
                extra_str = str(t.get("extra", ""))
                m = re.search(r"expected\s*>=\s*(\d+)", extra_str)
                if m:
                    exp_bytes = int(m.group(1))
                    if exp_bytes > 10 * 1024 * 1024:
                        return exp_bytes
        except Exception:
            pass
    return 0


async def extract_mediainfo_from_file(file_path: str) -> dict:
    """Run mediainfo CLI on file (10MB Range chunk) and return parsed JSON."""
    try:
        process = await asyncio.create_subprocess_exec(
            "mediainfo",
            "--Output=JSON",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            LOGGER.warning("MediaInfo CLI timed out")
            return {}

        if stdout:
            raw = stdout.decode("utf-8", errors="ignore").strip()
            if raw:
                data = json.loads(raw)
                try:
                    real_b = detect_real_video_bytes(file_path, data)
                    if real_b > 0:
                        tracks = data.get("media", {}).get("track", [])
                        for t in tracks:
                            if t.get("@type") == "General":
                                t["_real_video_bytes"] = real_b
                                break
                except Exception:
                    pass
                return data
    except Exception as e:
        LOGGER.warning(f"MediaInfo extraction failed: {e}")
    return {}


async def get_mediainfo_fast(
    primary_url: str,
    output_path: str | None = None,
    fallback_url: str | None = None,
    filename: str = "",
    fallback_size: str = "",
    total_bytes: int = 0,
    chunk_bytes: int = MEDIAINFO_CHUNK_BYTES,
) -> tuple[dict, str]:
    """One-call MediaInfo via HTTP Range (no full download).

    Downloads only the first 10MB to a temp file, runs ``mediainfo`` CLI,
    formats with :func:`format_mediainfo_text`, then aggressively deletes
    the temp chunk via ``os.remove``. Returns ``(info_json, formatted_text)``.
    """
    import shutil as _shutil

    tmp_path = output_path
    _own_tmp = False
    if not tmp_path:
        fd, tmp_path = tempfile.mkstemp(prefix="mi_chunk_", suffix=".mkv")
        os.close(fd)
        _own_tmp = True
    try:
        ok = await download_chunk_for_mediainfo(
            primary_url, tmp_path, fallback_url, chunk_bytes=chunk_bytes
        )
        if not ok:
            return {}, ""
        info = await extract_mediainfo_from_file(tmp_path)
        if not info:
            return {}, ""
        text = format_mediainfo_text(
            info, filename=filename,
            fallback_size=fallback_size, total_bytes=total_bytes,
        )
        return info, text
    finally:
        # Immediate cleanup — prevent disk exhaustion on Koyeb
        try:
            if tmp_path and os.path.exists(tmp_path) and (_own_tmp or True):
                # Only auto-delete temp chunks we own or that live in tmpdir
                if _own_tmp or tmp_path.startswith(tempfile.gettempdir()):
                    os.remove(tmp_path)
                elif output_path is None:
                    os.remove(tmp_path)
        except Exception:
            try:
                if tmp_path and os.path.exists(tmp_path):
                    _shutil.rmtree(tmp_path, ignore_errors=True) if os.path.isdir(tmp_path) else os.remove(tmp_path)
            except Exception:
                pass


def format_mediainfo_text(info_json: dict, filename: str = "", fallback_size: str = "", total_bytes: int = 0, archive_entries: list[dict] | None = None) -> str:
    """
    Format extracted MediaInfo JSON to match user template:
    Filename  : Formula.1.2026.Round15...mkv
    Format    : Matroska
    Size      : 20.2 GiB
    Duration  : 4 h 0 min
    Bitrate   : 12.0 Mb/s
    VIDEO
    Format    : HEVC
    Profile   : Main 10@L5.1@Main
    Resolution: 3840x2160
    Aspect    : 16:9
    FPS       : 59.940 (60000/1001) FPS
    Bit depth : 10 bits
    HDR       : Dolby Vision, Version 1.0, dvhe.08.09, BL+RPU, HDR10 compatible / SMPTE ST 2094 App 4, Version 1, HDR10+ Profile B compatible
    Color     : BT.2020
    Bitrate   : 11.8 Mb/s
    AUDIO
    [English] AAC LC 2 channels @ 192 kb/s
    SUBTITLES
    [English] UTF-8 (English [CC])
    """
    media = info_json.get("media", {})
    tracks = media.get("track", [])
    
    general = next((t for t in tracks if t.get("@type") == "General"), {})
    videos = [t for t in tracks if t.get("@type") == "Video"]
    audios = [t for t in tracks if t.get("@type") == "Audio"]
    subtitles = [t for t in tracks if t.get("@type") in ("Text", "Subtitle")]
    
    lines = []
    
    # ── GENERAL ──────────────────────────────────────────────
    fn = filename or general.get("FileNameExtension") or general.get("FileName") or general.get("Title") or "Media File"
    fn = fn.replace("_", ".")
    lines.append(format_pad("Filename", fn))
    
    container = general.get("Format", "Matroska")
    lines.append(format_pad("Format", container))
    
    size_str = fallback_size or general.get("FileSize_String")
    if not size_str:
        raw_bytes = general.get("FileSize")
        if raw_bytes:
            try:
                b = int(raw_bytes)
                if b >= 1024**3:
                    size_str = f"{b / 1024**3:.1f} GiB"
                elif b >= 1024**2:
                    size_str = f"{b / 1024**2:.1f} MiB"
                else:
                    size_str = f"{b // 1024} KiB"
            except Exception:
                pass
    if not size_str:
        size_str = "N/A"
    lines.append(format_pad("Size", size_str))
    
    dur_sec = general.get("Duration")
    lines.append(format_pad("Duration", format_human_duration(dur_sec)))
    
    try:
        total_duration_sec = float(dur_sec) if dur_sec else 0.0
    except Exception:
        total_duration_sec = 0.0

    # Detect true video byte size if it was inside an archive or parsed from a chunk
    real_vid_bytes = general.get("_real_video_bytes", 0)
    if not real_vid_bytes and archive_entries:
        real_vid_bytes = archive_entries[0].get("uncomp", 0)
    if not real_vid_bytes:
        real_vid_bytes = detect_real_video_bytes(info_json=info_json)

    if is_archive_filename(filename):
        # In an archive (e.g. .zip season pack), total_bytes is the whole archive size (e.g. 9.13 GB),
        # but duration is for ONE episode (e.g. 37m 24s).
        # We MUST NOT divide archive size by single episode duration!
        effective_bytes = real_vid_bytes if real_vid_bytes > 0 else 0
    else:
        effective_bytes = real_vid_bytes if real_vid_bytes > 0 else total_bytes

    # Overall Bitrate
    ov_br = format_bitrate_str(general.get("OverallBitRate"), general.get("OverallBitRate_String"))
    if effective_bytes > 0 and total_duration_sec > 0:
        calc_bps = int((effective_bytes * 8) / total_duration_sec)
        # If overall bitrate was from a small chunk (<100 kbps) or missing, use calculated
        if ov_br == "N/A" or ("kb/s" in ov_br and calc_bps >= 800_000):
            ov_br = format_bitrate_str(calc_bps)
    elif is_archive_filename(filename) and "kb/s" in ov_br:
        ov_br = "N/A"
    lines.append(format_pad("Bitrate", ov_br))
    
    # ── VIDEO ────────────────────────────────────────────────
    if videos:
        lines.append("")
        for idx, vid in enumerate(videos):
            v_hdr = f"VIDEO #{idx+1}" if len(videos) > 1 else "VIDEO"
            lines.append(v_hdr)
            
            codec = vid.get("Format", "HEVC")
            lines.append(format_pad("Format", codec))
            
            profile = vid.get("Format_Profile", "")
            level = vid.get("Format_Level", "")
            tier = vid.get("Format_Tier", "")
            if "@" in profile:
                prof_str = profile
            else:
                prof_str = profile
                if level:
                    prof_str += f"@L{level}"
                if tier:
                    prof_str += f"@{tier}"
            if prof_str:
                lines.append(format_pad("Profile", prof_str))
                
            w = vid.get("Width")
            h = vid.get("Height")
            res_str = f"{w}x{h}" if w and h else "N/A"
            lines.append(format_pad("Resolution", res_str))
            
            aspect = format_aspect(vid)
            lines.append(format_pad("Aspect", aspect))
            
            # FPS
            fps = vid.get("FrameRate_String")
            if not fps:
                num = vid.get("FrameRate_Num")
                den = vid.get("FrameRate_Den")
                fr_val = vid.get("FrameRate")
                if fr_val:
                    try:
                        fv = float(fr_val)
                        if num and den and int(den) > 1:
                            fps = f"{fv:.3f} ({num}/{den}) FPS"
                        else:
                            fps = f"{fv:.3f} FPS"
                    except Exception:
                        fps = f"{fr_val} FPS"
                else:
                    fps = "24.000 FPS"
            if not fps.upper().endswith("FPS") and not fps.lower().endswith("fps"):
                fps += " FPS"
            lines.append(format_pad("FPS", fps))
            
            depth = vid.get("BitDepth_String") or (f"{vid.get('BitDepth')} bits" if vid.get("BitDepth") else "8 bits")
            lines.append(format_pad("Bit depth", depth))
            
            # HDR
            hdr_str = (
                vid.get("HDR_Format_String")
                or vid.get("HDR_Format_Commercial")
                or vid.get("HDR_Format")
                or vid.get("HDR_Format_Compatibility")
            )
            colour_primaries = str(vid.get("colour_primaries", ""))
            transfer = str(vid.get("transfer_characteristics", ""))
            if hdr_str:
                lines.append(format_pad("HDR", hdr_str))
            elif "2020" in colour_primaries or "PQ" in transfer or "HLG" in transfer:
                lines.append(format_pad("HDR", "HDR10"))
                
            # Color
            color = vid.get("colour_primaries") or vid.get("matrix_coefficients")
            if color:
                lines.append(format_pad("Color", color))
                
            # Video Bitrate
            v_br = format_bitrate_str(vid.get("BitRate"), vid.get("BitRate_String"))
            if effective_bytes > 0 and total_duration_sec > 0:
                calc_total_bps = int((effective_bytes * 8) / total_duration_sec)
                aud_bps = 0
                for a in audios:
                    if a.get("BitRate"):
                        try:
                            aud_bps += int(a["BitRate"])
                        except Exception:
                            aud_bps += 224000
                    else:
                        aud_bps += 224000
                calc_vid_bps = max(calc_total_bps - aud_bps, int(calc_total_bps * 0.82))
                if v_br == "N/A" or ("kb/s" in v_br and calc_total_bps >= 500_000):
                    v_br = format_bitrate_str(calc_vid_bps)
            elif is_archive_filename(filename) and "kb/s" in v_br:
                v_br = "N/A"
            lines.append(format_pad("Bitrate", v_br))
            
    # ── AUDIO ────────────────────────────────────────────────
    if audios:
        lines.append("")
        lines.append("AUDIO")
        for aud in audios:
            lang = get_clean_language_short(aud.get("Language_String") or aud.get("Language") or "English")
            fmt = aud.get("Format_Commercial_IfAny") or aud.get("Format") or "AAC"
            if aud.get("Format") == "AAC" and aud.get("Format_Profile") == "LC" and "LC" not in fmt:
                fmt = "AAC LC"
            ch = aud.get("Channels_String") or (f"{aud.get('Channels')} channels" if aud.get("Channels") else "2 channels")
            br = format_bitrate_str(aud.get("BitRate"), aud.get("BitRate_String"))
            
            title = aud.get("Title")
            title_str = ""
            if title and title.strip().lower() not in ("audio", "audio track", "", lang.lower()):
                title_str = f" ({title.strip()})"
                
            if br and br != "N/A":
                lines.append(f"[{lang}] {fmt} {ch} @ {br}{title_str}")
            else:
                lines.append(f"[{lang}] {fmt} {ch}{title_str}")
                
    # ── SUBTITLES ────────────────────────────────────────────
    if subtitles:
        lines.append("")
        lines.append("SUBTITLES")
        for sub in subtitles:
            lang = get_clean_language_short(sub.get("Language_String") or sub.get("Language") or "English")
            fmt = sub.get("Format") or sub.get("Format_Commercial_IfAny") or "UTF-8"
            title = sub.get("Title")
            if title and title.strip().lower() not in ("subtitle", "subtitles", "", lang.lower()):
                lines.append(f"[{lang}] {fmt} ({title.strip()})")
            else:
                lines.append(f"[{lang}] {fmt}")
                
    # ── EPISODES (if archive has multiple files) ──────────────
    entries_to_show = archive_entries or general.get("_archive_entries")
    if entries_to_show and len(entries_to_show) > 1:
        lines.extend(format_archive_episodes_list(entries_to_show))

    return "\n".join(lines)


def _extract_pyav_sync(video_url: str, temp_dir: str) -> list[str]:
    """Synchronous PyAV extractor — runs ONLY inside ``asyncio.to_thread``.

    Opens the REMOTE url (no full download), seeks to 20/40/60/80% of
    duration with keyframe snap (``backward=True`` — no intermediate decode),
    converts one frame per point to PIL, resizes to max 1280px width
    (LANCZOS), saves high-quality JPEG (q95) ``screenshot_1..4.jpg``.
    ``container.close() + gc.collect()`` in ``finally`` protects 512MB RAM.
    """
    try:
        import av
    except Exception as e:
        LOGGER.warning(f"PyAV not installed, skipping screenshots: {e}")
        return []
    from PIL import Image
    from PIL.Image import Resampling

    os.makedirs(temp_dir, exist_ok=True)
    container = av.open(
        video_url,
        options={
            "timeout": PYAV_TIMEOUT_OPT,
            "reconnect": "1",
            "reconnect_streamed": "1",
            "reconnect_delay_max": "5",
            "max_buffer_size": "2097152",
            "probesize": "5000000",
            "analyzeduration": "5000000",
            "user_agent": CHROME_UA,
        },
    )
    try:
        vs = next((s for s in container.streams if s.type == "video"), None)
        if vs is None:
            LOGGER.warning("PyAV: no video stream found")
            return []
        try:
            # Single thread decoder on 512MB box prevents multi-frame memory allocation
            vs.thread_count = 1
        except Exception:
            pass
        if container.duration and container.duration > 0:
            duration = container.duration / 1_000_000  # AV_TIME_BASE us -> s
        elif vs.duration:
            duration = float(vs.duration * vs.time_base)
        else:
            LOGGER.warning("PyAV: unknown duration, cannot seek")
            return []

        tb = float(vs.time_base)
        saved: list[str] = []
        for i, frac in enumerate(PYAV_FRACS, 1):
            fp = os.path.join(temp_dir, f"screenshot_{i}.jpg")
            try:
                target_pts = int((frac * duration) / tb)
                # backward=True snaps directly to nearest keyframe (I-frame)
                container.seek(target_pts, stream=vs, backward=True, any_frame=False)
                for frame in container.decode(vs):
                    # Take the very first keyframe immediately — avoids decoding hundreds of intermediate P/B frames!
                    img = frame.to_image()
                    w, h = img.size
                    if w > PYAV_MAX_WIDTH:
                        resized = img.resize(
                            (PYAV_MAX_WIDTH, int(h * PYAV_MAX_WIDTH / w)),
                            Resampling.BILINEAR,
                        )
                        del img
                        img = resized
                    img.save(fp, format="JPEG", quality=PYAV_JPEG_QUALITY, optimize=True)
                    del img, frame
                    saved.append(fp)
                    break
            except Exception as e:
                LOGGER.debug(f"PyAV grab failed at {frac:.0%}: {e}")
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
            finally:
                gc.collect()
        return saved
    finally:
        try:
            container.close()
        except Exception:
            pass
        gc.collect()


async def generate_local_screenshots_pyav(video_url: str, temp_dir: str) -> list[str]:
    """Async wrapper for the Telegram bot — PyAV blocks, so run it in a thread.

    Call as ``shots = await generate_local_screenshots_pyav(url, temp_dir)``.
    Never blocks the event loop; 90s overall guard; never raises.
    """
    if os.environ.get("DISABLE_SCREENSHOTS", "0") == "1" or os.environ.get("SKIP_SCREENSHOTS", "0") == "1":
        LOGGER.info("Screenshots disabled via env — skipping")
        return []
    if not video_url or not video_url.startswith(("http://", "https://")):
        return []
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(_extract_pyav_sync, video_url, temp_dir),
            timeout=PYAV_OVERALL_TIMEOUT,
        )
        LOGGER.info(f"PyAV screenshots: {len(res)}/4 for {video_url[:40]}...")
        return res
    except asyncio.TimeoutError:
        LOGGER.warning("PyAV extraction timed out")
    except Exception as e:
        LOGGER.warning(f"PyAV extraction failed: {e}")
    return []


# ── Archive (.zip) screenshots ───────────────────────────────────────────────
# .zip/.rar links PyAV directly nahi khol pata (FFmpeg ko raw video bytes chahiye,
# archive container nahi). ZIP ke liye central-directory tail parse karke andar ka
# video dhoondhte hain:
#   - STORED entries: bytes contiguous hote hain → localhost range-proxy FFmpeg ko
#     full seeking deta hai bina poora archive download kiye (multi-GB safe).
#   - Chhoti DEFLATED entries (<=64MB): download + inflate + local PyAV.
# .rar/.7z bina full download ke sambhav nahi (512MB RAM cap) → gracefully [].

ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab", ".iso")
ARCHIVE_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mts", ".mov",
                      ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".m4v")
ZIP_TAIL_START = 256 * 1024       # first tail fetch
ZIP_TAIL_MAX = 8 * 1024 * 1024    # max EOCD search window
ZIP_CD_MAX = 32 * 1024 * 1024     # max central-directory fetch
ZIP_DEFLATE_MAX_BYTES = 64 * 1024 * 1024  # full-download cap for compressed entries
ARCHIVE_ATTEMPT_TIMEOUT = 300.0   # whole archive attempt guard (parse is fast; proxy stages self-bound)
ZIP_PROXY_STAGE_TIMEOUT = 75.0    # one STORED-proxy + PyAV run (slow hosts fail fast → next URL tried)
_PROXY_CHUNK = 1024 * 1024        # 1MB pieces for proxy/entry fetch


def is_archive_filename(filename: str | None) -> bool:
    if not filename:
        return False
    fn = str(filename).strip().lower()
    return fn.endswith(ARCHIVE_EXTS)


def format_archive_episodes_list(entries: list[dict], max_items: int = 25) -> list[str]:
    """Format a clean list of episodes inside an archive with their individual sizes."""
    lines = []
    if not entries or len(entries) <= 1:
        return lines
    lines.append("")
    lines.append(f"EPISODES ({len(entries)}):")
    for idx, e in enumerate(entries[:max_items], 1):
        name = e.get("name", "")
        uncomp = e.get("uncomp", 0)
        if uncomp >= 1024**3:
            sz_str = f"{uncomp / 1024**3:.2f} GB"
        elif uncomp >= 1024**2:
            sz_str = f"{uncomp / 1024**2:.1f} MB"
        else:
            sz_str = f"{uncomp // 1024} KB"

        base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        m = re.search(
            r"(?:S\d+)?(E\d+)(?:[._\s]+(.*?))?(?:[._\s]+(?:1080p|720p|2160p|480p|HD|WEB|AMZN|NF|Hotstar|Jio|Zee5|Sony|BluRay|HDTV|DDP|AAC|x264|x265|H\.?264|HEVC))",
            base,
            re.IGNORECASE,
        )
        if m:
            ep = m.group(1).upper()
            title = (m.group(2) or "").replace(".", " ").strip(" -_")
            ep_label = f"{ep} - {title}" if title else ep
        else:
            clean = os.path.splitext(base)[0].replace(".", " ")
            ep_label = clean[:40]

        lines.append(f"• {ep_label} [{sz_str}]")
    if len(entries) > max_items:
        lines.append(f"• ... and {len(entries) - max_items} more episodes")
    return lines


def format_archive_mediainfo_fallback(filename: str | None, size: str | None,
                                      archive_entries: list[dict] | None = None) -> str:
    """Honest MediaInfo fallback for archives (never fake Matroska/HEVC)."""
    clean_fn = (filename or "Media File").replace("_", ".")
    low = clean_fn.lower()
    if low.endswith(".zip"):
        kind = "ZIP Archive"
    elif low.endswith(".rar"):
        kind = "RAR Archive"
    elif low.endswith(".7z"):
        kind = "7-Zip Archive"
    else:
        kind = "Archive"
    res = [
        f"Filename  : {clean_fn}",
        f"Format    : {kind}",
        f"Size      : {size or 'N/A'}",
        "Video inside archive — screenshots extracted from embedded video where possible"
    ]
    if archive_entries and len(archive_entries) > 1:
        res.extend(format_archive_episodes_list(archive_entries))
    return "\n".join(res)


def _zip_u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _zip_u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _zip_u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def _http_range_bytes(client: "httpx.Client", url: str, first: int, last: int) -> bytes | None:
    """Fetch exactly [first, last] via HTTP Range. Returns None when unsupported."""
    if first < 0 or last < first:
        return None
    want = last - first + 1
    try:
        with client.stream("GET", url, headers={"Range": f"bytes={first}-{last}"}) as r:
            if r.status_code == 206:
                buf = bytearray()
                for piece in r.iter_bytes(chunk_size=256 * 1024):
                    buf.extend(piece)
                    if len(buf) >= want:
                        break
                if len(buf) < want:
                    return None
                return bytes(buf[:want])
            if r.status_code == 200 and first == 0:
                # Server ignored Range — only usable from offset 0; read capped.
                buf = bytearray()
                for piece in r.iter_bytes(chunk_size=256 * 1024):
                    buf.extend(piece)
                    if len(buf) >= want:
                        break
                return bytes(buf[:want])
    except Exception as e:
        LOGGER.debug(f"Range fetch [{first}-{last}] failed: {e}")
    return None


def _http_total_size(client: "httpx.Client", url: str) -> int | None:
    """Total file size via 0-0 Range (Content-Range) or HEAD. None when unknown."""
    try:
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
            if r.status_code == 206:
                cr = r.headers.get("content-range", "")
                m = re.search(r"/(\d+)\s*$", cr)
                if m:
                    return int(m.group(1))
            elif r.status_code == 200:
                cl = r.headers.get("content-length")
                if cl and cl.isdigit():
                    return int(cl)
    except Exception as e:
        LOGGER.debug(f"Total-size probe failed: {e}")
    try:
        r = client.head(url)
        cl = r.headers.get("content-length")
        if cl and cl.isdigit():
            return int(cl)
    except Exception:
        pass
    return None


def _zip_parse_eocd(tail: bytes) -> dict | None:
    """Parse End-of-Central-Directory from tail bytes. Handles ZIP64 locator."""
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0 or len(tail) - idx < 22:
        return None
    disk_no = _zip_u16(tail, idx + 4)
    cd_disk = _zip_u16(tail, idx + 6)
    total_entries = _zip_u16(tail, idx + 10)
    cd_size = _zip_u32(tail, idx + 12)
    cd_offset = _zip_u32(tail, idx + 16)
    if total_entries == 0 or disk_no != 0 or cd_disk != 0:
        return None  # empty or multi-disk span — unsupported
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or total_entries == 0xFFFF:
        # ZIP64 — locator (20 bytes) sits right before EOCD
        if idx < 20 or tail[idx - 20:idx - 16] != b"PK\x06\x07":
            return None
        eocd64_off = _zip_u64(tail, idx - 12)
        return {"zip64": True, "eocd64_off": eocd64_off}
    return {"zip64": False, "total_entries": total_entries,
            "cd_size": cd_size, "cd_offset": cd_offset}


def _zip_parse_eocd64(blob: bytes) -> dict | None:
    if len(blob) < 56 or blob[0:4] != b"PK\x06\x06":
        return None
    return {"zip64": False,
            "total_entries": _zip_u64(blob, 32),
            "cd_size": _zip_u64(blob, 40),
            "cd_offset": _zip_u64(blob, 48)}


def _zip_parse_cd(blob: bytes, count: int) -> list[dict]:
    """Parse central-directory entries. Returns [{name, method, comp, uncomp, loff}]."""
    entries: list[dict] = []
    o = 0
    for _ in range(count):
        if o + 46 > len(blob) or blob[o:o + 4] != b"PK\x01\x02":
            break
        flag = _zip_u16(blob, o + 8)
        method = _zip_u16(blob, o + 10)
        comp = _zip_u32(blob, o + 20)
        uncomp = _zip_u32(blob, o + 24)
        nl = _zip_u16(blob, o + 28)
        el = _zip_u16(blob, o + 30)
        cl = _zip_u16(blob, o + 32)
        loff = _zip_u32(blob, o + 42)
        name_end = o + 46 + nl
        if name_end > len(blob):
            break
        try:
            name = blob[o + 46:name_end].decode("utf-8", errors="replace")
        except Exception:
            name = ""
        # ZIP64 extra field (0x0001): oversized values in order uncomp/comp/loff
        if comp == 0xFFFFFFFF or uncomp == 0xFFFFFFFF or loff == 0xFFFFFFFF:
            eo = name_end
            eend = eo + el
            while eo + 4 <= eend <= len(blob):
                eid = _zip_u16(blob, eo)
                esz = _zip_u16(blob, eo + 2)
                if eo + 4 + esz > len(blob):
                    break
                if eid == 0x0001:
                    p = eo + 4
                    if uncomp == 0xFFFFFFFF and p + 8 <= eo + 4 + esz:
                        uncomp = _zip_u64(blob, p); p += 8
                    if comp == 0xFFFFFFFF and p + 8 <= eo + 4 + esz:
                        comp = _zip_u64(blob, p); p += 8
                    if loff == 0xFFFFFFFF and p + 8 <= eo + 4 + esz:
                        loff = _zip_u64(blob, p); p += 8
                    break
                eo += 4 + esz
        entries.append({"name": name, "method": method, "comp": comp,
                        "uncomp": uncomp, "loff": loff,
                        "encrypted": bool(flag & 0x01)})
        o = name_end + el + cl
    return entries


def _zip_pick_video(entries: list[dict]) -> dict | None:
    """Largest non-encrypted video file inside the archive."""
    best = None
    for e in entries:
        name = (e.get("name") or "")
        if not name or name.endswith("/") or e.get("encrypted"):
            continue
        if not name.lower().endswith(ARCHIVE_VIDEO_EXTS):
            continue
        if not e.get("uncomp"):
            continue
        if best is None or e["uncomp"] > best["uncomp"]:
            best = e
    return best


def get_zip_video_entries_sync(source_url: str) -> list[dict]:
    """Parse ZIP central directory and return list of all video files with name and size."""
    if not source_url or not source_url.startswith(("http://", "https://")):
        return []
    try:
        with httpx.Client(follow_redirects=True,
                          timeout=httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=10.0),
                          headers={"User-Agent": CHROME_UA, "Accept-Encoding": "identity"}) as client:
            total = _http_total_size(client, source_url)
            if not total or total < 22:
                return []
            tail_size = ZIP_TAIL_START
            eocd = None
            while tail_size <= max(ZIP_TAIL_MAX, total):
                start = max(0, total - tail_size)
                tail = _http_range_bytes(client, source_url, start, total - 1)
                if tail is None:
                    return []
                eocd = _zip_parse_eocd(tail)
                if eocd is not None:
                    break
                if start == 0:
                    return []
                tail_size *= 2
            if not eocd:
                return []
            if eocd.get("zip64"):
                eoff = eocd["eocd64_off"]
                blob = _http_range_bytes(client, source_url, eoff, eoff + 56)
                if blob:
                    eocd = _zip_parse_eocd64(blob) or eocd
            cd_off, cd_size = eocd["cd_offset"], eocd["cd_size"]
            if cd_size > ZIP_CD_MAX or not eocd.get("total_entries"):
                return []
            cd_blob = _http_range_bytes(client, source_url, cd_off, cd_off + cd_size - 1)
            if not cd_blob:
                return []
            entries = _zip_parse_cd(cd_blob, eocd["total_entries"])
            vids = []
            for e in entries:
                name = e.get("name") or ""
                if not name or name.endswith("/") or e.get("encrypted"):
                    continue
                if not name.lower().endswith(ARCHIVE_VIDEO_EXTS):
                    continue
                if not e.get("uncomp"):
                    continue
                vids.append(e)
            return vids
    except Exception as e:
        LOGGER.debug(f"Failed to fetch zip entries: {e}")
        return []


def _zip_data_start(client: "httpx.Client", url: str, local_offset: int) -> int | None:
    """Local file header → embedded data start offset."""
    hdr = _http_range_bytes(client, url, local_offset, local_offset + 64)
    if not hdr or len(hdr) < 30 or hdr[0:4] != b"PK\x03\x04":
        return None
    nl = _zip_u16(hdr, 26)
    el = _zip_u16(hdr, 28)
    return local_offset + 30 + nl + el


class _ZipEntryProxyHandler(BaseHTTPRequestHandler):
    """Maps entry-relative bytes → archive-absolute ranged fetches (STORED only)."""
    server_version = "ZipProxy/1.0"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        try:
            lock = getattr(self.server, "_conn_lock", None)
            if lock:
                with lock:
                    self.server._conns.add(self.connection)  # type: ignore[attr-defined]
            else:
                self.server._conns.add(self.connection)  # type: ignore[attr-defined]
        except Exception:
            pass

    def finish(self):
        try:
            lock = getattr(self.server, "_conn_lock", None)
            if lock:
                with lock:
                    self.server._conns.discard(self.connection)  # type: ignore[attr-defined]
            else:
                self.server._conns.discard(self.connection)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            super().finish()
        except Exception:
            pass

    def do_HEAD(self):
        total = getattr(self.server, "_entry_size", 0)
        mime = getattr(self.server, "_mime", "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(total))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        total = getattr(self.server, "_entry_size", 0)
        base = getattr(self.server, "_data_start", 0)
        client = getattr(self.server, "_proxy_client", None)
        source_url = getattr(self.server, "_source_url", "")
        mime = getattr(self.server, "_mime", "application/octet-stream")

        if not client or not source_url or total <= 0:
            self.send_error(500, "Proxy not configured")
            return

        rng = (self.headers.get("Range") or "").strip()
        try:
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng)
                if not m:
                    self.send_error(416, "Bad Range")
                    return
                a, b = m.group(1), m.group(2)
                if a == "" and b == "":
                    self.send_error(416, "Bad Range")
                    return
                if a == "":  # suffix: last N bytes
                    n = int(b)
                    first, last = max(0, total - n), total - 1
                else:
                    first = int(a)
                    last = int(b) if b != "" else total - 1
                if first >= total or last < first:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                last = min(last, total - 1)
                is_range = True
            else:
                first = 0
                last = total - 1
                is_range = False

            body_len = last - first + 1
            src_first = base + first
            src_last = base + last

            req_headers = {"Range": f"bytes={src_first}-{src_last}"}
            with client.stream("GET", source_url, headers=req_headers) as r:
                if r.status_code == 416:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                if r.status_code not in (200, 206):
                    self.send_error(502, f"Upstream error {r.status_code}")
                    return

                if r.status_code == 200 and src_first > 0:
                    self.send_error(502, "Upstream does not support Range")
                    return

                status_code = 206 if is_range else 200
                self.send_response(status_code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(body_len))
                self.send_header("Accept-Ranges", "bytes")
                if is_range:
                    self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                # Stream in 64KB chunks directly into the socket: peak RAM is < 0.1MB!
                remaining = body_len
                for chunk in r.iter_bytes(chunk_size=64 * 1024):
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            LOGGER.debug(f"Zip proxy GET failed: {e}")


_MIME_BY_EXT = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
                ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".wmv": "video/x-ms-wmv",
                ".flv": "video/x-flv", ".webm": "video/webm", ".ts": "video/mp2t",
                ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".mpg": "video/mpeg",
                ".mpeg": "video/mpeg", ".vob": "video/mpeg"}


def _zip_shots_stored_proxy(client: "httpx.Client", source_url: str, entry: dict,
                            temp_dir: str,
                            timeout: float = ZIP_PROXY_STAGE_TIMEOUT) -> list[str]:
    """Serve a STORED entry over localhost with full seeking → PyAV frames.

    Streams in 64KB chunks directly over ThreadingHTTPServer so memory usage is <1MB
    even for multi-gigabyte video files inside ZIP archives. Safe for 512MB RAM boxes.
    """
    import time as _time
    ext = os.path.splitext(entry["name"])[1].lower() or ".mkv"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ZipEntryProxyHandler)
    server.daemon_threads = True
    server._source_url = source_url  # type: ignore[attr-defined]
    server._data_start = entry["_data_start"]  # type: ignore[attr-defined]
    server._entry_size = entry["uncomp"]  # type: ignore[attr-defined]
    server._mime = _MIME_BY_EXT.get(ext, "application/octet-stream")  # type: ignore[attr-defined]
    server._proxy_client = client  # type: ignore[attr-defined]
    server._conns = set()  # type: ignore[attr-defined]
    server._conn_lock = threading.Lock()  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    proxy_url = f"http://127.0.0.1:{server.server_address[1]}/video{ext}"
    LOGGER.info(f"Zip proxy up for {entry['name']} ({entry['uncomp'] // (1024*1024)}MB)")
    result: dict = {}

    def _run():
        try:
            result["shots"] = _extract_pyav_sync(proxy_url, temp_dir)
        except Exception as e:
            result["error"] = e

    def _unblock_server():
        try:
            lock = getattr(server, "_conn_lock", None)
            if lock:
                with lock:
                    conns = list(getattr(server, "_conns", ()))
            else:
                conns = list(getattr(server, "_conns", ()))
            for c in conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    jt = threading.Thread(target=_run, daemon=True)
    jt.start()
    t0 = _time.monotonic()
    jt.join(timeout)
    elapsed = _time.monotonic() - t0
    if jt.is_alive():
        LOGGER.warning(f"Zip proxy stage timed out after {elapsed:.0f}s — force-closing proxy")
    _unblock_server()
    t.join(timeout=5)
    jt.join(timeout=10)
    shots = result.get("shots") or []
    if shots:
        LOGGER.info(f"Zip proxy shots: {len(shots)}/4 in {elapsed:.0f}s")
    return shots


def _zip_shots_deflated_small(client: "httpx.Client", source_url: str, entry: dict,
                              temp_dir: str) -> list[str]:
    """Download (capped) + raw-inflate a small DEFLATED entry → local PyAV."""
    comp = entry["comp"]
    if not comp or comp > ZIP_DEFLATE_MAX_BYTES:
        LOGGER.info(f"Zip entry too big for inflate ({comp} bytes) — skipping shots")
        return []
    raw = bytearray()
    pos = entry["_data_start"]
    end = pos + comp
    while pos < end:
        piece = _http_range_bytes(client, source_url, pos, min(pos + _PROXY_CHUNK - 1, end - 1))
        if not piece:
            return []
        raw.extend(piece)
        pos += len(piece)
    try:
        data = zlib.decompressobj(-15).decompress(bytes(raw))
    except Exception as e:
        LOGGER.warning(f"Zip inflate failed: {e}")
        return []
    ext = os.path.splitext(entry["name"])[1].lower() or ".mkv"
    out_path = os.path.join(temp_dir, f"zip_video{ext}")
    try:
        with open(out_path, "wb") as f:
            f.write(data)
    finally:
        del raw, data
        gc.collect()
    try:
        return _extract_pyav_sync(out_path, temp_dir)
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass


def _zip_shots_from_url_sync(source_url: str, temp_dir: str) -> list[str]:
    """Full ZIP attempt for one URL. Never raises — [] on any failure."""
    import time as _time
    t0 = _time.monotonic()
    try:
        with httpx.Client(follow_redirects=True,
                          timeout=httpx.Timeout(connect=8.0, read=25.0, write=10.0, pool=10.0),
                          headers={"User-Agent": CHROME_UA, "Accept-Encoding": "identity"}) as client:
            total = _http_total_size(client, source_url)
            if not total or total < 22:
                return []
            # 1. EOCD via growing tail window
            tail_size = ZIP_TAIL_START
            eocd = None
            while tail_size <= max(ZIP_TAIL_MAX, total):
                start = max(0, total - tail_size)
                tail = _http_range_bytes(client, source_url, start, total - 1)
                if tail is None:
                    return []
                eocd = _zip_parse_eocd(tail)
                if eocd is not None:
                    break
                if start == 0:
                    return []  # whole file scanned, no EOCD → not a zip
                tail_size *= 2
            if not eocd:
                return []
            # 2. ZIP64 → resolve real CD location first
            if eocd.get("zip64"):
                eoff = eocd["eocd64_off"]
                blob = _http_range_bytes(client, source_url, eoff, eoff + 56)
                if blob is None:
                    return []
                eocd = _zip_parse_eocd64(blob)
                if not eocd:
                    return []
            cd_off, cd_size = eocd["cd_offset"], eocd["cd_size"]
            if cd_size > ZIP_CD_MAX or not eocd.get("total_entries"):
                return []
            # 3. Central directory must lie inside the file
            if cd_off + cd_size > total:
                return []
            cd_blob = _http_range_bytes(client, source_url, cd_off, cd_off + cd_size - 1)
            if cd_blob is None:
                return []
            entries = _zip_parse_cd(cd_blob, eocd["total_entries"])
            entry = _zip_pick_video(entries)
            if entry is None:
                LOGGER.info("Zip has no video file inside — skipping shots")
                return []
            LOGGER.info(f"Zip video entry: {entry['name']} "
                        f"({entry['uncomp'] // (1024*1024)}MB, "
                        f"{'stored' if entry['method'] == 0 else 'deflated'}) "
                        f"[parse {_time.monotonic()-t0:.1f}s]")
            # 4. Local header → data start
            if entry["loff"] + 30 > total:
                return []
            data_start = _zip_data_start(client, source_url, entry["loff"])
            if data_start is None or data_start >= total:
                return []
            entry["_data_start"] = data_start
            if entry["method"] == 0:  # STORED — contiguous bytes
                if data_start + entry["uncomp"] > total:
                    return []
                return _zip_shots_stored_proxy(client, source_url, entry, temp_dir)
            if entry["method"] == 8:  # DEFLATED
                return _zip_shots_deflated_small(client, source_url, entry, temp_dir)
            LOGGER.info(f"Zip method {entry['method']} unsupported — skipping shots")
            return []
    except Exception as e:
        LOGGER.warning(f"Zip screenshot attempt failed: {e}")
    return []


def _attempt_zip_screenshots_sync(urls: list[str], temp_dir: str) -> list[str]:
    import time as _time
    for url in urls:
        t0 = _time.monotonic()
        try:
            final = resolve_final_video_url_sync(url)
            shots = _zip_shots_from_url_sync(final, temp_dir)
            if shots:
                return shots
        except Exception as e:
            LOGGER.debug(f"Zip URL attempt failed ({str(url)[:50]}): {e}")
        LOGGER.info(f"Zip URL exhausted in {_time.monotonic()-t0:.0f}s: {str(url)[:60]}")
    return []


async def extract_archive_screenshots(filename: str | None,
                                      candidate_urls: list[str] | None,
                                      temp_dir: str) -> list[str]:
    """Screenshots for archive links. ZIP supported; .rar/.7z gracefully [] (never raises).

    Gated by ENABLE_ZIP_SHOTS=1: remote frame extraction (localhost range-proxy
    + FFmpeg over multi-GB entries) needs headroom that 512MB instances don't
    have (OOM exit 9 observed). Default OFF = posts publish safely without
    archive shots; enable only on instances with >=1GB RAM.
    """
    if not is_archive_filename(filename):
        return []
    if os.environ.get("ENABLE_ZIP_SHOTS", "1") == "0":
        LOGGER.info(f"Archive screenshots disabled (ENABLE_ZIP_SHOTS=0) for {filename}")
        return []
    if str(filename).lower().endswith(".zip"):
        urls = [u for u in (candidate_urls or []) if u and str(u).startswith(("http://", "https://"))]
        if not urls:
            return []
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(_attempt_zip_screenshots_sync, urls, temp_dir),
                timeout=ARCHIVE_ATTEMPT_TIMEOUT,
            )
            LOGGER.info(f"Archive screenshots: {len(res)}/4 for {filename}")
            return res
        except asyncio.TimeoutError:
            LOGGER.warning("Archive screenshot attempt timed out")
        except Exception as e:
            LOGGER.warning(f"Archive screenshot attempt failed: {e}")
        return []
    LOGGER.info(f"Archive screenshots: {filename} — only .zip supported, skipping shots")
    return []


async def extract_screenshots_smart(filename: str | None,
                                    stream_url: str | None,
                                    fallback_urls: list[str] | None,
                                    temp_dir: str) -> list[str]:
    """Single entry-point for posts: archives → embedded-video extraction,
    normal videos → existing PyAV path. Never raises."""
    try:
        if is_archive_filename(filename):
            cands = []
            # Priority 1: MrSpidy DDL direct URLs (fastest Range request support)
            for u in list(fallback_urls or []):
                if u and ("mrspidy" in str(u) or "ddl-by" in str(u)) and u not in cands:
                    cands.append(u)
            if stream_url and ("mrspidy" in str(stream_url) or "ddl-by" in str(stream_url)) and stream_url not in cands:
                cands.append(stream_url)
            # Priority 2: Other stream URLs & fallbacks
            if stream_url and stream_url not in cands:
                cands.append(stream_url)
            for u in list(fallback_urls or []):
                if u and u not in cands:
                    cands.append(u)
            return await extract_archive_screenshots(filename, cands, temp_dir)
        if stream_url:
            return await generate_local_screenshots_pyav(stream_url, temp_dir)
    except Exception as e:
        LOGGER.warning(f"Smart screenshot extraction failed: {e}")
    return []


def is_black_frame(image_path: str, mean_thresh: float = 12.0, black_ratio_thresh: float = 0.85) -> bool:
    """Pitch-black check in <10ms: grayscale + 160x90 downscale + mean test.

    Returns True if the frame should be discarded (black/empty/corrupt).
    No-op (False) when OpenCV is unavailable — caller keeps the frame.
    """
    try:
        if cv2 is None or np is None:
            return False
        if not image_path or not os.path.exists(image_path):
            return True
        if os.path.getsize(image_path) < 2000:
            return True
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return True
        # Downscale first: 640px JPEG -> 160x90 => mean/std in ~2-5ms
        try:
            small = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA)
        except Exception:
            small = img
        mean_v = float(np.mean(small))
        black_ratio = float(np.sum(small < 25) / small.size)
        return bool(mean_v < mean_thresh or black_ratio > black_ratio_thresh)
    except Exception:
        return False


def evaluate_frame_fast(image_path: str, ts: float = 99.0) -> float:
    """Light check + censor/logo reject. ~5ms. ts chahiye censor pakadne ke liye."""
    try:
        if not image_path or not os.path.exists(image_path):
            return -1000.0
        sz = os.path.getsize(image_path)
        if sz < 5000:
            return -1000.0
        # Spec fast path: pitch-black discard in <10ms via is_black_frame()
        if is_black_frame(image_path):
            return -1000.0
        if cv2 is None or np is None:
            return float(min(sz / 1000.0, 100.0))
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return -1000.0
        if max(img.shape) > 320:
            small = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA)
        else:
            small = img
        mean_v = float(np.mean(small))
        std_v = float(np.std(small))
        black_ratio = float(np.sum(small < 25) / small.size)
        del img, small
        if mean_v < 20.0 or black_ratio > 0.65 or std_v < 12.0:
            return -1000.0
        # CBFC censor card: beige paper (150-215), dense text (std>30), early ts
        if ts < 60.0 and 150.0 < mean_v < 215.0 and std_v > 30.0:
            return -1000.0
        # Early logo/title on dark bg, no content
        if ts < 15.0 and black_ratio > 0.45:
            return -1000.0
        return min(std_v * 1.5 + sz / 20000.0, 200.0)
    except Exception:
        return 50.0


def evaluate_frame_quality(image_path: str, ts: float) -> tuple[float, int]:
    """
    Score a candidate screenshot frame using OpenCV:
    - Filters out black / near-black frames (mean < 25 or black_ratio > 0.65)
    - Filters out flat / blank / solid color frames (std < 15)
    - Filters out intro logos, title cards & censor certificates (ts < 12s with no faces)
    - Detects human character faces using Haar Cascade (+200 score per face)
    - Rewards contrast, brightness, visual detail, and scene variety
    Returns (score, face_count).
    """
    if not image_path or not os.path.exists(image_path) or os.path.getsize(image_path) < 2000:
        return -1000.0, 0

    if cv2 is None or np is None:
        return 100.0, 0

    try:
        img = cv2.imread(image_path)
        if img is None:
            return -1000.0, 0

        # Downscale for ultra-fast processing and zero memory impact (<3MB RAM)
        h, w = img.shape[:2]
        if max(w, h) > 640:
            scale = 640.0 / max(w, h)
            small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            small = img

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mean_val = float(np.mean(gray))
        std_val = float(np.std(gray))
        black_ratio = float(np.sum(gray < 25) / gray.size)

        # 1. Reject black or near-black frames ("black ye nhi")
        if mean_val < 25.0 or black_ratio > 0.65:
            return -1000.0, 0

        # 2. Reject flat / uniform / solid color frames
        if std_val < 15.0:
            return -1000.0, 0

        # 3. Detect human faces using Haar Cascade ("face wala")
        face_count = 0
        if FACE_CASCADE is not None:
            faces = FACE_CASCADE.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=4,
                minSize=(25, 25)
            )
            face_count = len(faces)

        # 4. Anti-logo & anti-censor filter ("logo ye nhi"):
        # In movies/shows, the first 12s almost always contain censor certificate,
        # studio logos (Pen Studios, Yash Raj, Warner Bros) or statutory warnings.
        # If timestamp is early and no human face is detected, penalize heavily.
        if ts < 12.0 and face_count == 0:
            return -500.0, 0

        # Also detect logo/title text on dark/solid background (e.g. guest artist Helen, credits)
        if face_count == 0 and black_ratio > 0.45:
            return -400.0, 0

        # Censor certificate / text-card filter (e.g. CBFC card at film start):
        # bright beige paper, dense text texture, no faces, early timestamp.
        if ts < 45.0 and face_count == 0 and black_ratio < 0.30 and 150.0 < mean_val < 215.0 and std_val > 35.0:
            return -350.0, 0

        # 5. Composite score:
        # - Faces: massive bonus (+200 per face up to 3 faces)
        # - Contrast/texture: min(std_val, 80) * 1.5
        # - Ideal movie brightness (around 80-140): bonus up to 50
        # - Temporal bonus for deeper timestamps (actual movie content): up to 40
        face_bonus = min(face_count, 3) * 200.0
        contrast_score = min(std_val, 80.0) * 1.5
        brightness_score = max(0.0, 50.0 - abs(mean_val - 100.0) * 0.5)
        temporal_bonus = min(ts * 1.5, 40.0)

        total_score = face_bonus + contrast_score + brightness_score + temporal_bonus
        return total_score, face_count

    except Exception as e:
        LOGGER.debug(f"Error evaluating frame quality for {image_path}: {e}")
        return 50.0, 0


def cleanup_temp_files(*file_paths):
    """Safely remove list of temporary files"""
    for fp in file_paths:
        if fp and isinstance(fp, str) and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception as e:
                LOGGER.warning(f"Could not delete temp file {fp}: {e}")
        elif fp and isinstance(fp, (list, tuple)):
            for sub in fp:
                if sub and isinstance(sub, str) and os.path.exists(sub):
                    try:
                        os.remove(sub)
                    except Exception as e:
                        LOGGER.warning(f"Could not delete temp file {sub}: {e}")


def cleanup_all_temp_dirs():
    """Purge any remaining gdflix temp directories and loose media files to prevent disk accumulation on Koyeb/cloud"""
    try:
        import shutil
        tmp_base = tempfile.gettempdir()
        for entry in os.listdir(tmp_base):
            if (
                entry.startswith("gdflix_post_")
                or entry.startswith("gdflix_batch_")
                or entry.startswith("screenshot_")
                or entry.startswith("cand_")
                or entry.startswith("legacy_audio_")
                or (entry.startswith("sample") and entry.endswith(".mkv"))
            ):
                full_p = os.path.join(tmp_base, entry)
                if os.path.isdir(full_p):
                    shutil.rmtree(full_p, ignore_errors=True)
                elif os.path.isfile(full_p):
                    try:
                        os.remove(full_p)
                    except Exception:
                        pass
    except Exception as e:
        LOGGER.debug(f"Temp cleanup notice: {e}")


async def upload_image_to_catbox(file_path: str, timeout: float = 20.0) -> str | None:
    """Upload an image file to Catbox and return the direct permanent URL"""
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(file_path, "rb") as f:
                content = f.read()
            resp = await client.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (os.path.basename(file_path), content, "image/jpeg")}
            )
            if resp.status_code == 200 and resp.text.strip().startswith("http"):
                return resp.text.strip()
    except Exception as e:
        LOGGER.warning(f"Catbox upload failed for {file_path}: {e}")
    return None


async def upload_screenshots(screenshot_paths: list[str]) -> list[str]:
    """Upload multiple screenshots in parallel to Catbox and return direct URLs"""
    if not screenshot_paths:
        return []
    tasks = [upload_image_to_catbox(p) for p in screenshot_paths]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    urls = []
    for r in results:
        if isinstance(r, str) and r.startswith("http"):
            urls.append(r)
    LOGGER.info(f"Successfully uploaded {len(urls)}/{len(screenshot_paths)} screenshots to Catbox")
    return urls


async def ensure_fast_poster_url(poster_url: str) -> str:
    """Ensure poster URL is on a fast CDN that Telegram servers can access without blocks"""
    if not poster_url:
        return ""
    if "catbox.moe" in poster_url:
        return poster_url
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(poster_url)
            if resp.status_code == 200 and len(resp.content) > 1000:
                cat_resp = await client.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": ("poster.jpg", resp.content, "image/jpeg")}
                )
                if cat_resp.status_code == 200 and cat_resp.text.strip().startswith("http"):
                    return cat_resp.text.strip()
    except Exception as e:
        LOGGER.warning(f"Could not re-host poster on fast CDN: {e}")
    return poster_url


# Legacy helper maintained for backward compatibility
async def extract_audio_languages_from_url(ddl_url: str) -> str:
    try:
        temp_dir = tempfile.gettempdir()
        temp_file = os.path.join(temp_dir, f"legacy_audio_{os.getpid()}.mkv")
        downloaded = await download_media_chunk(ddl_url, temp_file, max_bytes=8 * 1024 * 1024, timeout=20.0)
        if not downloaded:
            return "Unknown"
        info = await extract_mediainfo_from_file(temp_file)
        cleanup_temp_files(temp_file)
        
        audios = [t for t in info.get("media", {}).get("track", []) if t.get("@type") == "Audio"]
        langs = []
        for a in audios:
            lang = a.get("Language_String") or a.get("Language")
            if lang:
                clean = get_clean_language(lang).split(" ")[0]
                if clean not in langs:
                    langs.append(clean)
        return ", ".join(langs) if langs else "Unknown"
    except Exception as e:
        LOGGER.warning(f"Legacy audio extraction failed: {e}")
        return "Unknown"

