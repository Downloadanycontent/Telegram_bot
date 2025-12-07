#!/usr/bin/env python3
# downloadanycontent_bot.py
# Legal PUBLIC downloader bot.
# IMPORTANT: Use only for public content you have right to distribute.
# Do NOT use for private/paid/DRM/copyrighted content.

import os
import tempfile
import shutil
import asyncio
import logging
from urllib.parse import urlparse, parse_qs
import subprocess
import sys
import time

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- CONFIG ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8241430357:AAFUdZ5RioIMAFTbKiWuI8UYYKXiOQzql6M")

# Hosts allowed for direct download (public only)
WHITELIST = {
    "drive.google.com",
    "docs.google.com",
    "dl.dropboxusercontent.com",
    "dropbox.com",
    "mediafire.com",
    "mega.nz",
    "transfer.sh",
    "file.io",
    "anonfiles.com",
    "pixeldrain.com",
    "send.cm",
    "example.com",

    # Added hosts
    "instagram.com",     
    "googlevideo.com" 
}

# Limits & behavior
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024      # 500 MB max per download
MAX_SIZE_BEFORE_COMPRESS = 25 * 1024 * 1024 # 25 MB compress threshold
REQUEST_TIMEOUT = 60
USER_RATE_LIMIT = 1     # requests per WINDOW
WINDOW = 30             # seconds window for rate limit
CONCURRENT_DOWNLOADS = 2  # semaphore to avoid many parallel downloads

# Optional API_KEY (leave empty for public use)
API_KEY = os.getenv("DOWNLOAD_BOT_KEY", "")

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- runtime structures ----------
_user_requests = {}  # user_id -> list of timestamps
download_semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ---------- helpers ----------
def domain_allowed(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return False
    for w in WHITELIST:
        if host == w or host.endswith("." + w):
            return True
    return False

def drive_direct_link(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    parts = parsed.path.split("/")
    if "file" in parts and "d" in parts:
        try:
            idx = parts.index("d")
            file_id = parts[idx + 1]
            return f"https://drive.google.com/uc?export=download&id={file_id}"
        except Exception:
            pass
    if "id" in qs:
        return f"https://drive.google.com/uc?export=download&id={qs['id'][0]}"
    return url

def get_filename_from_response(resp, url):
    cd = resp.headers.get("content-disposition")
    if cd and "filename=" in cd:
        fname = cd.split("filename=")[-1].strip(' "')
        return fname
    path = urlparse(url).path
    name = os.path.basename(path) or "file"
    return name

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    arr = _user_requests.get(user_id, [])
    arr = [t for t in arr if now - t < WINDOW]
    if len(arr) >= USER_RATE_LIMIT:
        _user_requests[user_id] = arr
        return False
    arr.append(now)
    _user_requests[user_id] = arr
    return True

async def run_command(cmd, cwd=None):
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("Send a PUBLIC link (Google Drive public / Dropbox public / direct file URL / MediaFire / public video link / Instagram public). "
            "Bot will attempt to download and send the file.\n\n"
            "Rules: public files only. Private/paid/DRM/copyrighted content is rejected.")
    if API_KEY:
        text = "API_KEY required. Send /key YOUR_KEY\n\n" + text
    await update.message.reply_text(text)

async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text("This bot is public; no API key required.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /key YOUR_KEY")
        return
    if args[0] == API_KEY:
        await update.message.reply_text("API key accepted. You may send links now.")
    else:
        await update.message.reply_text("Invalid key.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()
    if not text.startswith("http"):
        await update.message.reply_text("Send a valid URL starting with http or https.")
        return

    if API_KEY:
        await update.message.reply_text("This bot requires an API key. Use /key YOUR_KEY.")
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text(f"Rate limit: max {USER_RATE_LIMIT} requests per {WINDOW}s. Try again later.")
        return

    parsed = urlparse(text)
    host = parsed.netloc.lower()
    dl_url = text

    if host.endswith("drive.google.com") or host.endswith("docs.google.com"):
        dl_url = drive_direct_link(text)

    # Detect video sites for yt-dlp
    is_video_site = any(s in host for s in ("youtube.com", "youtu.be", "vimeo.com", "facebook.com", "x.com", "twitter.com", "dailymotion.com", "instagram.com"))
    if is_video_site:
        await update.message.reply_text("Detected video site/public media. Downloading via yt-dlp...")
        await download_video_and_send(update, dl_url)
        return

    if not domain_allowed(dl_url):
        await update.message.reply_text("Domain not allowed or not public. Check whitelist or use public video link.")
        return

    await update.message.reply_text("Accepted link. Downloading...")
    async with download_semaphore:
        await download_direct_and_send(update, dl_url)

async def download_direct_and_send(update: Update, dl_url: str):
    tmpdir = tempfile.mkdtemp()
    try:
        local_path = os.path.join(tmpdir, "downloaded")
        with requests.get(dl_url, stream=True, timeout=REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            content_length = r.headers.get("content-length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                await update.message.reply_text("File exceeds max allowed size. Aborting.")
                return
            filename = get_filename_from_response(r, dl_url)
            local_path = os.path.join(tmpdir, filename)
            total = 0
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            await update.message.reply_text("Download exceeded allowed size. Aborting.")
                            return
        file_size = os.path.getsize(local_path)
        if file_size > MAX_SIZE_BEFORE_COMPRESS:
            zip_base = os.path.join(tmpdir, "out")
            shutil.make_archive(zip_base, 'zip', tmpdir, filename)
            zip_path = zip_base + ".zip"
            await update.message.reply_document(document=open(zip_path, "rb"))
        else:
            await update.message.reply_document(document=open(local_path, "rb"))
    except Exception as e:
        logger.exception("direct download failed")
        await update.message.reply_text(f"Download failed. Reason: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def download_video_and_send(update: Update, url: str):
    tmpdir = tempfile.mkdtemp()
    try:
        out_template = os.path.join(tmpdir, "%(title).100s.%(ext)s")
        cmd = f"yt-dlp -f best -o \"{out_template}\" \"{url}\" --no-playlist --geo-bypass"
        await update.message.reply_text("Running yt-dlp, please wait...")
        code, out, err = await run_command(cmd)
        logger.info("yt-dlp out: %s", out)
        logger.info("yt-dlp err: %s", err)
        if code != 0:
            await update.message.reply_text("yt-dlp failed. Video may be private or blocked.")
            return
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir,f))]
        if not files:
            await update.message.reply_text("No output file found after yt-dlp.")
            return
        files.sort(key=lambda p: os.path.getsize(p), reverse=True)
        video_path = files[0]
        if os.path.getsize(video_path) > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Downloaded file exceeds max allowed size. Aborting.")
            return
        if os.path.getsize(video_path) > MAX_SIZE_BEFORE_COMPRESS:
            zip_path = video_path + ".zip"
            shutil.make_archive(os.path.splitext(zip_path)[0], 'zip', tmpdir, os.path.basename(video_path))
            await update.message.reply_document(document=open(zip_path, "rb"))
        else:
            await update.message.reply_document(document=open(video_path, "rb"))
    except Exception as e:
        logger.exception("video download failed")
        await update.message.reply_text(f"Video download failed. Reason: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    await update.message.reply_text(f"Received Telegram file: {doc.file_name}. Use this to share files to be re-served.")

def main():
    if TOKEN.startswith("PUT_YOUR_TOKEN"):
        print("Set TELEGRAM_BOT_TOKEN environment variable or edit TOKEN in script.")
        sys.exit(1)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("key", key_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()

if __name__ == "__main__":
    main()
