# ⚡ GDFlix Bot v2.0

<p align="center">
  <b>A modern, high-performance Telegram Bot that converts Google Drive links into high-speed GDFlix direct stream links with automated Channel Posts, Expandable MediaInfo, Face-Scored Screenshots, and OTT Poster detection.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" />
  <img src="https://img.shields.io/badge/OpenCV-Face%20Detection-5C3EE8?style=for-the-badge&logo=opencv&logoColor=white" />
  <img src="https://img.shields.io/badge/FFmpeg-Fast%20Seek-007808?style=for-the-badge&logo=ffmpeg&logoColor=white" />
  <img src="https://img.shields.io/badge/MongoDB-Async%20Motor-47A248?style=for-the-badge&logo=mongodb&logoColor=white" />
  <img src="https://img.shields.io/badge/Deploy-Koyeb%20%7C%20Heroku%20%7C%20Docker-blueviolet?style=for-the-badge" />
</p>

---

## 🌟 Key Highlights & Features

### 🎬 1. Automated Channel Post Generation
* **Single & Multi-Link Batching**: Convert individual Google Drive links or bulk paste multiple links/folders at once. The bot automatically groups files by movie/show title and generates unified channel posts.
* **Expandable Blockquotes (`<blockquote expandable>`)**: Keeps channel posts neat and tidy. Users can tap to expand full MediaInfo and Screenshot links.
* **Pastyx / HrX-Style MediaInfo**: Clean visual layout with blank line spacing between sections, accurate video bitrate calculations, and audio title tags (e.g. `[Hindi] Dolby Digital Plus 2 channels @ 224 kb/s (Hindi - -HrX)`).

### 📸 2. Face & Character-Aware Screenshots (OpenCV)
* Automatically extracts **4 high-quality screenshots** from the video stream without downloading the full multi-gigabyte file.
* Integrates **OpenCV Haar Cascade Face Detection** (`haarcascade_frontalface_default.xml`) to score frames and skip blank screens, distributor logos, or static title cards.

### 🧹 3. Zero-Footprint Cloud Disk Cleanup (Koyeb Optimized)
* **Instant Chunk Purge**: The 80 MB video chunk is immediately deleted from disk the moment MediaInfo and screenshots are extracted.
* **Instant Screenshot Purge**: As soon as screenshots are uploaded to Telegram servers, local `.jpg` files are deleted from the container.
* **Automated `/tmp` Sanitizer**: `cleanup_all_temp_dirs()` wipes any residual temporary directories and loose files after each post and upon bot boot.

### ⚡ 4. High-Speed Direct Stream & DDL Fallback
* Scrapes high-speed CDN and R2 direct streams from GDFLIX.
* Features automatic **Spidy DDL API fallback** (`https://ddl-by.mrspidy.eu.org/direct.aspx?id=...`) if GDFLIX serves an HTML page (`fastdl-one.pages.dev`).

### 🎨 5. Multi-Source Poster & OTT Detection
* Multi-source poster matching using **TMDB**, **JustWatch**, and **Spidey API**.
* Intelligently selects 16:9 landscape backdrops/posters with fallback to official title banners.
* Detects OTT release tags (Amazon Prime, Netflix, Hotstar, Zee5, SonyLIV, etc.).

### 💬 6. Group Auto-Posting
* When added to Telegram groups, pasting a Google Drive link automatically generates the full rich post with poster and buttons right in the group or mirrors it to your designated channel.

---

## 📋 Bot Commands

### User Commands
| Command | Description |
|---|---|
| `/start` | Launch the bot and verify authorization |
| `/help` | Detailed guide on using the bot |
| `/gdflix <link>` | Convert a single Google Drive URL |
| `/poster <name>` | Manually search and select posters |
| `/history` | View your last 10 converted links |
| `/setkey <key>` | Save your personal GDFlix API key |
| `/mykey` | Display your active GDFlix API key |
| `/removekey` | Remove your custom GDFlix API key |
| `/setbackup <link>` | Set custom backup channel link |
| `/removebackup` | Reset backup channel link |
| `/stats` | View bot system stats and link conversion count |

### Owner / Admin Commands
| Command | Description |
|---|---|
| `/addadmin <id>` | Grant admin privileges to a user ID *(Owner only)* |
| `/removeadmin <id>` | Revoke admin privileges *(Owner only)* |
| `/listadmins` | List all current bot administrators *(Owner only)* |
| `/defaultbackup on/off`| Toggle global default backup channel watermark |
| `/clearcache` | Flush poster, stream, and memory caches |

---

## 🛠️ Environment Variables Configuration

| Variable | Description | Required | Default |
|---|---|:---:|---|
| `BOT_TOKEN` | Telegram Bot Token from [@BotFather](https://t.me/BotFather) | **Yes** | — |
| `ADMIN_ID` | Telegram User ID of the bot owner | **Yes** | — |
| `MONGODB_URI` | MongoDB Connection URI (Atlas recommended) | **Yes** | — |
| `GDFLIX_KEY` | Primary GDFlix API Key | **Yes** | — |
| `GDRIVE_API_KEY` | Google Drive API Key (for folder traversal) | **Yes** | — |
| `CHANNEL_ID` | Telegram Channel ID / username for auto-posting | **Yes** | — |
| `POST_CHANNEL_ID`| Target channel ID for automated channel posts | No | `CHANNEL_ID` |
| `TMDB_API_KEY` | TheMovieDatabase API Key for metadata & backdrops | No | Bundled Key |
| `SPIDY_API_KEY` | Spidey API Key for poster & DDL resolution | No | Bundled Key |
| `PORT` | Health check HTTP server port (for Koyeb web services) | No | `8000` |
| `AUDIO_EMOJI_ID` | Telegram Premium Custom Emoji ID for Audio icon | No | Default |
| `CROWN_EMOJI_ID` | Telegram Premium Custom Emoji ID for VIP/Backup icon | No | Default |
| `SPARKLE_EMOJI_ID`| Telegram Premium Custom Emoji ID for Sparkle icon | No | Default |

---

## 🚀 Deployment Guide

### Deploying on Koyeb (Recommended)

1. **Fork or Push** this repository to your GitHub account.
2. Log into [Koyeb.com](https://app.koyeb.com/).
3. Click **Create Service** ➔ **GitHub**.
4. Select this repository and branch (`main`).
5. Under **Builder**, select **Dockerfile** (this ensures `ffmpeg`, `mediainfo`, and Python packages are installed).
6. Under **Environment variables**, input all required keys from the table above.
7. Click **Deploy**. The built-in HTTP health server on port `8000` ensures your service stays healthy and continuously active.

### Deploying with Docker Locally

```bash
# Clone the repository
git clone https://github.com/your-username/Gdflix-2.0.git
cd Gdflix-2.0

# Build the Docker image
docker build -t gdflix-bot .

# Run the container with your environment file
docker run -d --name gdflix-bot --env-file .env gdflix-bot
```

### Local Development Setup

```bash
# Ensure Python 3.11+, ffmpeg, and mediainfo CLI are installed on your OS
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python bot.py
```

---

## 📂 Project Architecture

```
Gdflix-2.0/
├── bot.py              # Main bot runner, command handlers & post orchestrator
├── config.py           # Centralized environment loader & runtime constants
├── database.py         # Async MongoDB motor driver (users, admins, keys, history)
├── media_utils.py      # Stream scraping, MediaInfo formatting & OpenCV screenshot engine
├── poster_utils.py     # Multi-provider landscape poster & OTT detection engine
├── Aptfile             # System packages (mediainfo, ffmpeg)
├── Dockerfile          # Multi-stage production container build
├── Procfile            # Worker process definition (Heroku)
├── requirements.txt    # Python library dependencies
└── README.md           # Documentation
```

---

## 🔒 Security & Privacy

* **Zero Persistent Media Storage**: Video chunks and screenshots are temporary and wiped immediately after Telegram delivery.
* **Role-Based Access Control**: Sensitive administrative commands are strictly restricted to `ADMIN_ID` and verified admins.
* **Sensitive Credentials**: API tokens and database URIs are loaded exclusively from environment variables.

---

## 📜 License

Distributed under the MIT License. Developed for educational purposes and community file management.


## Private configuration
Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` for local use, or enter its settings in Streamlit Community Cloud Secrets. Credentials are intentionally omitted from this repository.
