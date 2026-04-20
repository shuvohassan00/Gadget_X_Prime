#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GADGET X-PRIME 2026 ULTRA - upgraded single-file Telegram bot

Highlights
- Better UI, button flow, animated status text
- YouTube-style search list for music/video
- Direct URL download hub for supported social/video platforms
- Audio + video download with metadata, quality choices, file size display
- Shazam recognition from audio/voice/video/document
- Daily bonus, redeem, referral, premium, profile
- Admin panel with stats, admins, coins, premium, ban, broadcast, toggles
- MongoDB-backed user/config/state storage

Important
- Put BOT_TOKEN, MONGO_URL, OWNER_ID in .env
- Do NOT hardcode secrets in source code
- Some third-party sites change often, so no bot can honestly guarantee
  permanent 100% success on every platform/link forever.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import imageio_ffmpeg
import nest_asyncio
import telebot
from dotenv import load_dotenv
from pymongo import MongoClient
from telebot import apihelper
from telebot.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile

load_dotenv()
nest_asyncio.apply()

# ---------------------------
# Environment / global setup
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URL = os.getenv("MONGO_URL", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or 0)
BOT_NAME = os.getenv("BOT_NAME", "GADGET X-PRIME 2026 ULTRA").strip()
TMP_DIR = Path(os.getenv("BOT_TMP_DIR", "./tmp_gadget_ultra")).resolve()
TMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_TELEGRAM_UPLOAD_MB = max(1, int(os.getenv("MAX_TELEGRAM_UPLOAD_MB", "45").strip() or 45))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing in .env")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID missing in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
log = logging.getLogger("GadgetXPrimeUltra")

apihelper.READ_TIMEOUT = 180
apihelper.CONNECT_TIMEOUT = 120

ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] += os.pathsep + os.path.dirname(ffmpeg_path)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)

try:
    import yt_dlp

    YTDLP_OK = True
except Exception as exc:
    yt_dlp = None
    YTDLP_OK = False
    log.warning("yt-dlp import failed: %s", exc)

try:
    from shazamio import Shazam

    shazam = Shazam()
    SHAZAM_OK = True
except Exception as exc:
    shazam = None
    SHAZAM_OK = False
    log.warning("shazamio import failed: %s", exc)

# ---------------------------
# Database
# ---------------------------
client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=12000)
db = client["gadget_x_prime_ultra"]
users_col = db["users"]
config_col = db["config"]
redeem_col = db["redeem_codes"]
transactions_col = db["transactions"]
states_col = db["states"]

users_col.create_index("user_id", unique=True)
redeem_col.create_index("code", unique=True)
states_col.create_index("user_id", unique=True)
transactions_col.create_index("created_at")

DEFAULT_CONFIG: Dict[str, Any] = {
    "_id": "global",
    "bot_name": BOT_NAME,
    "daily_bonus": 50,
    "referral_reward": 100,
    "premium_price": 1200,
    "maintenance_mode": False,
    "force_sub_channel": "",
    "admins": [],
    "download_audio_enabled": True,
    "download_video_enabled": True,
    "downloads_enabled": True,
    "feature_flags": {
        "shazam": True,
        "redeem": True,
        "bonus": True,
        "referral": True,
        "premium": True,
        "downloads": True,
        "broadcast": True,
    },
}
config_col.update_one({"_id": "global"}, {"$setOnInsert": DEFAULT_CONFIG}, upsert=True)


# ---------------------------
# Runtime caches
# ---------------------------
SEARCH_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_LOCK = threading.Lock()
DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dlpool")
ACTIVE_CHAT_JOBS: set[int] = set()
ACTIVE_LOCK = threading.Lock()
URL_RE = re.compile(r"^https?://", re.I)
URL_EXTRACT_RE = re.compile(r"(https?://[^\s<>\"']+)", re.I)
TRAILING_URL_PUNCT_RE = re.compile(r"[)\],.!?:;]+$")


# ---------------------------
# Helpers
# ---------------------------
def now_utc() -> datetime:
    # Keep datetimes naive in UTC so values round-trip cleanly through MongoDB
    # without offset-aware/offset-naive subtraction errors.
    return datetime.utcnow()


def normalize_db_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def resolve_ydl_entry(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not info:
        return {}
    if info.get("_type") == "playlist":
        for entry in info.get("entries") or []:
            if entry:
                return entry
        return {}
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if entry:
                return entry
    return info


def safe_answer_callback(call, text: str, show_alert: bool = False) -> None:
    try:
        bot.answer_callback_query(call.id, text, show_alert=show_alert)
    except Exception:
        pass


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def cfg() -> Dict[str, Any]:
    data = config_col.find_one({"_id": "global"})
    if data:
        return data
    config_col.insert_one(DEFAULT_CONFIG.copy())
    return config_col.find_one({"_id": "global"}) or DEFAULT_CONFIG.copy()


def is_url(text: str) -> bool:
    return bool(URL_RE.match((text or "").strip()))


def extract_first_url(text: str) -> str:
    if not text:
        return ""
    match = URL_EXTRACT_RE.search(text.strip())
    candidate = (match.group(1) if match else "").strip()
    # Common in chats: URL followed by punctuation.
    return TRAILING_URL_PUNCT_RE.sub("", candidate)


def prettify_extractor_name(name: str) -> str:
    raw = (name or "Unknown").strip()
    if not raw:
        return "Unknown"
    return raw.replace("_", " ").replace("IE", "").strip().title()


def normalize_public_url(url: str) -> str:
    """
    Clean up copied social/share links so yt-dlp has a better chance to resolve
    platform redirect/short URLs on the first try.
    """
    cleaned = (url or "").strip().replace("\u200b", "")
    cleaned = cleaned.split()[0] if cleaned else ""
    cleaned = cleaned.strip("<>\"'")

    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower().lstrip("www.")
    path = parsed.path or ""

    # Convert Instagram share links into canonical post/reel links.
    if host.endswith("instagram.com") and "/share/" in path:
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "share":
            media_type = parts[1]
            short_code = parts[2]
            if media_type in ("reel", "p", "tv"):
                cleaned = f"https://instagram.com/{media_type}/{short_code}/"
        elif len(parts) >= 2 and parts[0] == "share":
            # Fallback: keep as reel-style if subtype is not present in copied URL.
            cleaned = f"https://instagram.com/reel/{parts[1]}/"
        parsed = urlparse(cleaned)
        host = (parsed.netloc or "").lower().lstrip("www.")

    # Expand YouTube short links with v parameter when present.
    if host == "youtu.be":
        video_id = path.strip("/")
        if video_id:
            cleaned = f"https://www.youtube.com/watch?v={video_id}"
            parsed = urlparse(cleaned)
            host = (parsed.netloc or "").lower().lstrip("www.")

    # For tiktok short links keep clean path only.
    if host in ("vt.tiktok.com", "vm.tiktok.com"):
        cleaned = f"{parsed.scheme}://{parsed.netloc}{path}"

    # Remove tracking query for Instagram/Facebook style links.
    # Keep query for some shorteners (e.g. YouTube list URLs).
    if "instagram.com/" in cleaned or "facebook.com/" in cleaned or "tiktok.com/" in cleaned:
        cleaned = cleaned.split("?", 1)[0]
    return expand_redirect_url(cleaned)


def expand_redirect_url(url: str) -> str:
    """
    Resolve short/share redirect links to final URL when possible.
    If network resolution fails, keep original URL.
    """
    candidate = (url or "").strip()
    if not candidate:
        return candidate
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().lstrip("www.")
    if host not in ("vt.tiktok.com", "vm.tiktok.com", "facebook.com", "m.facebook.com", "instagram.com"):
        return candidate
    try:
        req = Request(
            candidate,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=12) as resp:
            final_url = resp.geturl() or candidate
        return final_url.split("?", 1)[0]
    except (URLError, TimeoutError, ValueError):
        return candidate
    except Exception:
        return candidate


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_number(n: Optional[int]) -> str:
    if n is None:
        return "Unknown"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}B"


def format_bytes(num: Optional[int]) -> str:
    if num is None:
        return "Unknown"
    size = float(num)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return "Unknown"


def upload_limit_bytes() -> int:
    return int(MAX_TELEGRAM_UPLOAD_MB * 1024 * 1024)


def is_over_upload_limit(size_bytes: Optional[int]) -> bool:
    return bool(size_bytes and int(size_bytes) > upload_limit_bytes())


def upload_limit_text() -> str:
    return f"{MAX_TELEGRAM_UPLOAD_MB} MB"


def short_title(text: str, limit: int = 40) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "file")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "file"


def ensure_user(message) -> None:
    users_col.update_one(
        {"user_id": message.from_user.id},
        {
            "$setOnInsert": {
                "user_id": message.from_user.id,
                "coins": 0,
                "is_premium": False,
                "premium_expire_at": None,
                "is_banned": False,
                "ban_reason": "",
                "joined_at": now_utc(),
                "last_bonus_at": None,
                "daily_streak": 0,
                "referred_by": None,
                "total_referrals": 0,
                "claimed_milestones": [],
            },
            "$set": {
                "username": message.from_user.username or "",
                "first_name": message.from_user.first_name or "User",
                "last_seen_at": now_utc(),
            },
        },
        upsert=True,
    )


def get_user(user_id: int) -> Dict[str, Any]:
    return users_col.find_one({"user_id": user_id}) or {}


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in cfg().get("admins", [])


def is_banned(user_id: int) -> bool:
    return bool(get_user(user_id).get("is_banned"))


def set_state(user_id: int, state: str = "", data: Optional[Dict[str, Any]] = None) -> None:
    states_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "state": state, "data": data or {}, "updated_at": now_utc()}},
        upsert=True,
    )


def get_state(user_id: int) -> Dict[str, Any]:
    return states_col.find_one({"user_id": user_id}) or {"state": "", "data": {}}


def clear_state(user_id: int) -> None:
    states_col.delete_one({"user_id": user_id})


def log_tx(user_id: int, action: str, amount: int = 0, details: str = "") -> None:
    transactions_col.insert_one(
        {
            "user_id": user_id,
            "action": action,
            "amount": amount,
            "details": details,
            "created_at": now_utc(),
        }
    )


def progress_frames(kind: str) -> List[str]:
    if kind == "boot":
        return [
            "⚙️ <i>Initializing ultra core…</i>\n[■□□□□□□□□□] 10%",
            "🎨 <i>Loading premium UI pack…</i>\n[■■■□□□□□□□] 30%",
            "🔐 <i>Securing bot systems…</i>\n[■■■■■□□□□□] 50%",
            "📦 <i>Preparing smart menus…</i>\n[■■■■■■■□□□] 70%",
            "🚀 <i>Launching X-PRIME interface…</i>\n[■■■■■■■■■■] 100%",
        ]
    if kind == "search":
        return [
            "🔎 <i>Searching sources…</i>",
            "🎵 <i>Collecting top results…</i>",
            "✨ <i>Building clean result list…</i>",
        ]
    if kind == "download":
        return [
            "🔎 <i>Fetching media info…</i>",
            "📦 <i>Downloading best stream…</i>",
            "🛠 <i>Processing with ffmpeg…</i>",
            "📤 <i>Uploading to Telegram…</i>",
        ]
    if kind == "shazam":
        return [
            "🎧 <i>Extracting audio…</i>",
            "🎼 <i>Analyzing sound fingerprint…</i>",
            "🔎 <i>Looking for best match…</i>",
            "✨ <i>Preparing result…</i>",
        ]
    return []


def animate(chat_id: int, message_id: int, frames: List[str], delay: float = 0.7) -> None:
    def runner() -> None:
        for frame in frames:
            try:
                bot.edit_message_text(frame, chat_id, message_id)
                time.sleep(delay)
            except Exception:
                return

    if frames:
        threading.Thread(target=runner, daemon=True).start()


def animate_sync(chat_id: int, message_id: int, frames: List[str], delay: float = 0.45) -> None:
    for frame in frames:
        try:
            bot.edit_message_text(frame, chat_id, message_id)
        except Exception:
            break
        time.sleep(delay)


def send_home_message(chat_id: int, user_id: int, old_message_id: Optional[int] = None):
    try:
        if old_message_id:
            try:
                bot.delete_message(chat_id, old_message_id)
            except Exception:
                pass
        return bot.send_message(chat_id, home_text(user_id), reply_markup=main_kb(user_id))
    except Exception:
        fallback = (
            f"<b>{esc(cfg().get('bot_name', BOT_NAME))}</b>\n"
            f"Coins: <code>{get_user(user_id).get('coins', 0)}</code>\n"
            f"Use the menu below."
        )
        return bot.send_message(chat_id, fallback, reply_markup=main_kb(user_id))


def safe_edit(chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup)
        return
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
    try:
        bot.edit_message_caption(text, chat_id, message_id, reply_markup=reply_markup)
        return
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
    bot.send_message(chat_id, text, reply_markup=reply_markup)


def middleware_ok(chat_id: int, user_id: int) -> bool:
    c = cfg()
    if c.get("maintenance_mode") and not is_admin(user_id):
        bot.send_message(chat_id, "🔧 <b>Bot is under maintenance.</b>")
        return False
    if is_banned(user_id):
        u = get_user(user_id)
        bot.send_message(
            chat_id,
            f"🚫 <b>You are banned.</b>\nReason: <code>{esc(u.get('ban_reason', 'No reason'))}</code>",
        )
        return False

    force_ch = (c.get("force_sub_channel") or "").strip()
    if force_ch:
        try:
            member = bot.get_chat_member(force_ch, user_id)
            if member.status not in ("member", "administrator", "creator"):
                raise RuntimeError("not joined")
        except Exception:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{force_ch.lstrip('@')}"))
            kb.add(InlineKeyboardButton("🔁 Check Again", callback_data="back_home"))
            bot.send_message(chat_id, "🔒 <b>Join the channel first.</b>", reply_markup=kb)
            return False
    return True


def get_multiplier(streak: int) -> float:
    if streak <= 1:
        return 1.0
    if streak == 2:
        return 1.5
    if streak == 3:
        return 2.0
    if streak == 4:
        return 2.5
    return 3.0


def referral_link(user_id: int) -> str:
    me = bot.get_me()
    return f"https://t.me/{me.username}?start={user_id}"


def get_stats() -> Dict[str, int]:
    return {
        "users": users_col.count_documents({}),
        "premium": users_col.count_documents({"is_premium": True}),
        "banned": users_col.count_documents({"is_banned": True}),
        "codes": redeem_col.count_documents({}),
        "admins": len(cfg().get("admins", [])) + 1,
    }


def cache_put(payload: Dict[str, Any]) -> str:
    cid = uuid.uuid4().hex[:8]
    payload["created_at"] = time.time()
    with CACHE_LOCK:
        SEARCH_CACHE[cid] = payload
    return cid


def cache_get(cid: str) -> Optional[Dict[str, Any]]:
    with CACHE_LOCK:
        item = SEARCH_CACHE.get(cid)
        if item and time.time() - item.get("created_at", time.time()) <= 3600:
            return item
        if item:
            SEARCH_CACHE.pop(cid, None)
    return None


def cache_cleanup() -> None:
    with CACHE_LOCK:
        to_del = [k for k, v in SEARCH_CACHE.items() if time.time() - v.get("created_at", time.time()) > 3600]
        for key in to_del:
            SEARCH_CACHE.pop(key, None)


def acquire_chat_job(chat_id: int) -> bool:
    with ACTIVE_LOCK:
        if chat_id in ACTIVE_CHAT_JOBS:
            return False
        ACTIVE_CHAT_JOBS.add(chat_id)
        return True


def release_chat_job(chat_id: int) -> None:
    with ACTIVE_LOCK:
        ACTIVE_CHAT_JOBS.discard(chat_id)


# ---------------------------
# yt-dlp helpers
# ---------------------------
def yt_base_opts() -> Dict[str, Any]:
    return {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "skip_unavailable_fragments": True,
        "ffmpeg_location": ffmpeg_path,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extract_flat": False,
        "http_chunk_size": 10485760,
    }


def search_media(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    if not YTDLP_OK:
        raise RuntimeError("yt-dlp is not installed")
    opts = yt_base_opts() | {"default_search": f"ytsearch{limit}"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = []
    for entry in (data or {}).get("entries", []) or []:
        if not entry:
            continue
        entries.append(
            {
                "title": entry.get("title") or "Untitled",
                "webpage_url": entry.get("webpage_url") or entry.get("url") or "",
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or entry.get("channel") or "Unknown",
                "view_count": entry.get("view_count"),
                "thumbnail": entry.get("thumbnail"),
                "extractor": entry.get("extractor") or "YouTube",
                "description": entry.get("description") or "",
                "id": entry.get("id") or "",
            }
        )
    return entries


def extract_direct_info(url: str) -> Dict[str, Any]:
    if not YTDLP_OK:
        raise RuntimeError("yt-dlp is not installed")
    primary_opts = yt_base_opts()
    fallback_opts = yt_base_opts() | {
        # Some platforms are more stable with a realistic mobile UA/cookies behavior.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36"
            )
        },
    }
    info: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    expanded_url = expand_redirect_url(url)
    for opts in (primary_opts, fallback_opts):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = resolve_ydl_entry(ydl.extract_info(expanded_url, download=False))
            if info:
                break
        except Exception as exc:
            last_exc = exc

    if not info:
        if last_exc:
            raise RuntimeError(f"Unable to extract media info: {last_exc}")
        raise RuntimeError("Unable to extract media info")

    title = info.get("title") or "Untitled"
    extractor_name = prettify_extractor_name(info.get("extractor_key") or info.get("extractor") or "Unknown")

    return {
        "title": title,
        "webpage_url": info.get("webpage_url") or expanded_url,
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "extractor": extractor_name,
        "description": info.get("description") or "",
        "id": info.get("id") or "",
        "formats": info.get("formats") or [],
    }


def pick_video_qualities(url: str) -> List[Dict[str, Any]]:
    if not YTDLP_OK:
        raise RuntimeError("yt-dlp is not installed")
    source = (url or "").strip()
    if not source:
        raise RuntimeError("Empty media source")
    opts = yt_base_opts()
    if not is_url(source) and not source.startswith("ytsearch"):
        opts["default_search"] = "ytsearch1"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = resolve_ydl_entry(ydl.extract_info(source, download=False))

    if not info:
        raise RuntimeError("Unable to extract video formats")

    formats = info.get("formats") or []
    collected: Dict[str, Dict[str, Any]] = {}
    for fmt in formats:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        ext = fmt.get("ext")
        height = fmt.get("height")
        if not height or vcodec in (None, "none"):
            continue
        if ext not in ("mp4", "webm", "mkv"):
            continue

        label = f"{height}p"
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        score = (
            int(height),
            1 if acodec not in (None, "none") else 0,
            1 if ext == "mp4" else 0,
            fmt.get("tbr") or 0,
        )
        current = collected.get(label)
        candidate = {
            "label": label,
            "height": height,
            "format_id": fmt.get("format_id"),
            "ext": ext,
            "filesize": size,
            "has_audio": acodec not in (None, "none"),
            "score": score,
        }
        if not current or candidate["score"] > current["score"]:
            collected[label] = candidate

    qualities = sorted(collected.values(), key=lambda x: x["height"], reverse=True)
    trimmed = []
    seen = set()
    for item in qualities:
        if item["label"] in seen:
            continue
        seen.add(item["label"])
        item.pop("score", None)
        trimmed.append(item)
    return trimmed[:8]


def download_audio(source: str, work_dir: Path) -> Dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(work_dir / "audio.%(ext)s")
    source = (source or "").strip()
    opts = yt_base_opts() | {
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }
    if not is_url(source) and not source.startswith("ytsearch"):
        opts["default_search"] = "ytsearch1"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = resolve_ydl_entry(ydl.extract_info(source, download=True))
    files = sorted(work_dir.glob("audio.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_file = next((p for p in files if p.suffix.lower() == ".mp3"), None) or (files[0] if files else None)
    if not final_file:
        raise RuntimeError("Audio output not found")
    return {"path": final_file, "info": info}


def download_video(source: str, format_id: Optional[str], max_height: Optional[int], work_dir: Path) -> Dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    source = (source or "").strip()
    outtmpl = str(work_dir / "video.%(ext)s")
    if format_id:
        format_selector = f"{format_id}+bestaudio/{format_id}/best"
    elif max_height:
        format_selector = (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/best"
        )
    else:
        format_selector = "bestvideo*+bestaudio/bestvideo+bestaudio/best[ext=mp4]/best"

    opts = yt_base_opts() | {
        "outtmpl": outtmpl,
        "format": format_selector,
        "merge_output_format": "mp4",
    }
    if not is_url(source) and not source.startswith("ytsearch"):
        opts["default_search"] = "ytsearch1"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = resolve_ydl_entry(ydl.extract_info(source, download=True))
    files = sorted(work_dir.glob("video.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_file = next((p for p in files if p.suffix.lower() == ".mp4"), None) or (files[0] if files else None)
    if not final_file:
        raise RuntimeError("Video output not found")
    return {"path": final_file, "info": info}


# ---------------------------
# UI builders
# ---------------------------
def main_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔎 Music Search", callback_data="open_search_music"),
        InlineKeyboardButton("🌐 Link Grabber", callback_data="open_link_download"),
    )
    kb.add(
        InlineKeyboardButton("🎧 Song ID", callback_data="open_shazam"),
        InlineKeyboardButton("✨ Ultra Tools", callback_data="open_tools"),
    )
    kb.add(
        InlineKeyboardButton("🎁 Daily Bonus", callback_data="claim_bonus"),
        InlineKeyboardButton("🎟 Redeem Code", callback_data="open_redeem"),
    )
    kb.add(
        InlineKeyboardButton("👤 My Profile", callback_data="open_profile"),
        InlineKeyboardButton("👥 Invite & Earn", callback_data="open_referral"),
    )
    kb.add(InlineKeyboardButton("💎 Premium Shop", callback_data="open_premium"))
    if is_admin(user_id):
        kb.add(InlineKeyboardButton("👨‍💻 Control Panel", callback_data="open_admin"))
    return kb


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    return kb


def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
        InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
    )
    kb.add(
        InlineKeyboardButton("🎁 Set Bonus", callback_data="adm_set_bonus"),
        InlineKeyboardButton("🎟 Create Redeem", callback_data="adm_create_redeem"),
    )
    kb.add(
        InlineKeyboardButton("👥 Add Admin", callback_data="adm_add_admin"),
        InlineKeyboardButton("➖ Remove Admin", callback_data="adm_remove_admin"),
    )
    kb.add(
        InlineKeyboardButton("➕ Give Coins", callback_data="adm_add_coins"),
        InlineKeyboardButton("👑 Give Premium", callback_data="adm_give_premium"),
    )
    kb.add(
        InlineKeyboardButton("🚫 Ban User", callback_data="adm_ban_user"),
        InlineKeyboardButton("✅ Unban User", callback_data="adm_unban_user"),
    )
    kb.add(
        InlineKeyboardButton("📢 Force Join", callback_data="adm_force_join"),
        InlineKeyboardButton("🛠 Maintenance", callback_data="adm_maintenance"),
    )
    kb.add(
        InlineKeyboardButton("🎵 Toggle Shazam", callback_data="adm_toggle_shazam"),
        InlineKeyboardButton("📥 Toggle Downloads", callback_data="adm_toggle_downloads"),
    )
    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    return kb


def home_text(user_id: int) -> str:
    u = get_user(user_id)
    premium_text = "🟢 Active" if u.get("is_premium") else "🔴 Free"
    return (
        f"╔══════════════════════════════╗\n"
        f"║   <b>{esc(cfg().get('bot_name', BOT_NAME))}</b>   ║\n"
        f"╚══════════════════════════════╝\n"
        f"👋 <b>Welcome:</b> {esc(u.get('first_name', 'User'))}\n"
        f"💰 <b>Coins:</b> <code>{u.get('coins', 0)}</code>\n"
        f"👑 <b>Premium:</b> {premium_text}\n"
        f"🔥 <b>Daily Streak:</b> <code>{u.get('daily_streak', 0)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <i>Search any song title like YouTube</i>\n"
        f"🌐 <i>Paste public video or social links</i>\n"
        f"🎧 <i>Send audio, voice, or video for song ID</i>\n"
        f"✨ <i>Tap the menu below to use the premium ultra interface.</i>"
    )


def render_search_results(search_id: str) -> tuple[str, InlineKeyboardMarkup]:
    payload = cache_get(search_id)
    if not payload:
        return "❌ Search expired. Please search again.", back_kb()

    results = payload.get("entries", [])
    query = payload.get("query", "")
    lines = [f"🎵 <b>Top Search Results</b>", f"🔎 Query: <code>{esc(query)}</code>", f"📌 Tap any result below to open the media card.", ""]
    kb = InlineKeyboardMarkup(row_width=1)
    for idx, entry in enumerate(results[:8]):
        title = short_title(entry.get("title", "Untitled"), 45)
        dur = format_duration(entry.get("duration"))
        uploader = short_title(entry.get("uploader", "Unknown"), 20)
        lines.append(f"{idx + 1}. <b>{esc(title)}</b>")
        lines.append(f"   ⏱ {esc(dur)}  •  👤 {esc(uploader)}  •  👀 {esc(format_number(entry.get('view_count')))}")
        kb.add(InlineKeyboardButton(f"🎵 {idx + 1}. {title}", callback_data=f"sr|{search_id}|{idx}"))

    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    return "\n".join(lines), kb


def render_media_entry(search_id: str, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    payload = cache_get(search_id)
    if not payload:
        return "❌ Session expired. Search again.", back_kb()
    entries = payload.get("entries", [])
    if idx < 0 or idx >= len(entries):
        return "❌ Invalid selection.", back_kb()

    entry = entries[idx]
    txt = (
        f"🎬 <b>{esc(entry.get('title', 'Untitled'))}</b>\n"
        f"👤 <b>Uploader:</b> <code>{esc(entry.get('uploader', 'Unknown'))}</code>\n"
        f"⏱ <b>Duration:</b> <code>{esc(format_duration(entry.get('duration')))}</code>\n"
        f"👀 <b>Views:</b> <code>{esc(format_number(entry.get('view_count')))}</code>\n"
        f"🌐 <b>Source:</b> <code>{esc(entry.get('extractor', 'Unknown'))}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎧 Choose <b>Audio</b> for MP3\n"
        f"🎞 Choose <b>Video</b> (auto-fallback to best if qualities unavailable)"
    )
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎧 Audio", callback_data=f"sa|{search_id}|{idx}"),
        InlineKeyboardButton("🎞 Video", callback_data=f"sv|{search_id}|{idx}"),
    )
    kb.add(InlineKeyboardButton("⬅️ Back to Results", callback_data=f"sb|{search_id}"))
    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    return txt, kb


def render_video_quality_menu(search_id: str, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    payload = cache_get(search_id)
    if not payload:
        return "❌ Session expired. Search again.", back_kb()
    entries = payload.get("entries", [])
    entry = entries[idx]
    qualities = entry.get("qualities") or []
    if not qualities:
        return "❌ No selectable video qualities found.", back_kb()

    txt = [
        f"🎞 <b>Select Video Quality</b>",
        f"🎬 <code>{esc(entry.get('title', 'Untitled'))}</code>",
        f"📦 Safe bot upload limit: <code>{esc(upload_limit_text())}</code>",
        f"✅ = recommended   🚫 = too large for safe upload",
        "",
    ]
    kb = InlineKeyboardMarkup(row_width=2)
    for qidx, quality in enumerate(qualities):
        label = quality.get("label", "Unknown")
        size_bytes = quality.get("filesize")
        size = format_bytes(size_bytes)
        audio_note = " + 🔊" if quality.get("has_audio") else ""
        too_large = is_over_upload_limit(size_bytes)
        prefix = "🚫" if too_large else "✅"
        txt.append(f"• {prefix} {esc(label)} — {esc(size)}{audio_note}")
        callback = f"vqlarge|{search_id}|{idx}|{qidx}" if too_large else f"vq|{search_id}|{idx}|{qidx}"
        btn_prefix = "🚫" if too_large else "✨"
        kb.add(InlineKeyboardButton(f"{btn_prefix} {label} ({size})", callback_data=callback))
    kb.add(InlineKeyboardButton("⬅️ Back", callback_data=f"sr|{search_id}|{idx}"))
    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    return "\n".join(txt), kb


# ---------------------------
# Text / callbacks
# ---------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message)
    user_id = message.from_user.id

    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        try:
            referrer_id = int(args[1])
            u = get_user(user_id)
            if referrer_id != user_id and not u.get("referred_by") and get_user(referrer_id):
                reward = int(cfg().get("referral_reward", 100))
                users_col.update_one({"user_id": user_id}, {"$set": {"referred_by": referrer_id}})
                users_col.update_one(
                    {"user_id": referrer_id},
                    {"$inc": {"coins": reward, "total_referrals": 1}},
                )
                log_tx(referrer_id, "referral_reward", reward, f"new referral {user_id}")
                try:
                    bot.send_message(referrer_id, f"🎉 New referral joined. +{reward} coins added.")
                except Exception:
                    pass
        except Exception:
            pass

    if not middleware_ok(message.chat.id, user_id):
        return

    startup_msg = None
    try:
        frames = progress_frames("boot")
        startup_msg = bot.send_message(message.chat.id, frames[0])
        if len(frames) > 1:
            animate_sync(message.chat.id, startup_msg.message_id, frames[1:], 0.4)
    except Exception as exc:
        log.exception("Startup animation failed: %s", exc)
    finally:
        send_home_message(message.chat.id, user_id, startup_msg.message_id if startup_msg else None)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    ensure_user(message)
    if not middleware_ok(message.chat.id, message.from_user.id):
        return
    bot.reply_to(
        message,
        (
            "🆘 <b>Help</b>\n"
            "• /start — main menu\n"
            "• /cancel — clear current waiting state\n"
            "• Send song name — when Search Music asks for it\n"
            "• Send supported direct URL — bot also auto-detects links\n"
            "• Send audio/voice/video/document — for Shazam recognition"
        ),
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    clear_state(message.from_user.id)
    bot.reply_to(message, "❌ Cancelled.")
    bot.send_message(message.chat.id, home_text(message.from_user.id), reply_markup=main_kb(message.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data == "back_home")
def cb_back_home(call):
    users_col.update_one(
        {"user_id": call.from_user.id},
        {"$set": {"last_seen_at": now_utc()}},
        upsert=True,
    )
    if not middleware_ok(call.message.chat.id, call.from_user.id):
        return
    clear_state(call.from_user.id)
    safe_edit(call.message.chat.id, call.message.message_id, home_text(call.from_user.id), reply_markup=main_kb(call.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data == "open_profile")
def cb_profile(call):
    u = get_user(call.from_user.id)
    premium = "Yes" if u.get("is_premium") else "No"
    txt = (
        f"👤 <b>Profile</b>\n"
        f"🆔 <code>{call.from_user.id}</code>\n"
        f"💰 Coins: <code>{u.get('coins', 0)}</code>\n"
        f"👑 Premium: {premium}\n"
        f"🔥 Streak: {u.get('daily_streak', 0)}\n"
        f"👥 Referrals: {u.get('total_referrals', 0)}"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_referral")
def cb_referral(call):
    u = get_user(call.from_user.id)
    txt = (
        f"🔗 <b>Referral Center</b>\n"
        f"Your link:\n<code>{esc(referral_link(call.from_user.id))}</code>\n\n"
        f"👥 Total referrals: {u.get('total_referrals', 0)}\n"
        f"💰 Reward per invite: {cfg().get('referral_reward', 100)} coins"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_premium")
def cb_premium(call):
    u = get_user(call.from_user.id)
    price = int(cfg().get("premium_price", 1200))
    txt = (
        f"💎 <b>Premium</b>\n"
        f"Price: <code>{price}</code> coins\n"
        f"Your balance: <code>{u.get('coins', 0)}</code>\n\n"
        f"Benefits:\n"
        f"• 2x daily bonus\n"
        f"• Premium badge\n"
        f"• Priority queue feel\n"
        f"• Better experience for heavy users"
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛒 Buy Premium", callback_data="buy_premium"))
    kb.add(InlineKeyboardButton("🏠 Home", callback_data="back_home"))
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "buy_premium")
def cb_buy_premium(call):
    u = get_user(call.from_user.id)
    price = int(cfg().get("premium_price", 1200))
    if u.get("is_premium"):
        safe_answer_callback(call, "Already premium.", show_alert=True)
        return
    if u.get("coins", 0) < price:
        safe_answer_callback(call, "Not enough coins.", show_alert=True)
        return
    users_col.update_one({"user_id": call.from_user.id}, {"$inc": {"coins": -price}, "$set": {"is_premium": True}})
    log_tx(call.from_user.id, "buy_premium", -price, "coins purchase")
    safe_answer_callback(call, "Premium activated.", show_alert=True)
    cb_back_home(call)


@bot.callback_query_handler(func=lambda c: c.data == "claim_bonus")
def cb_bonus(call):
    if not cfg().get("feature_flags", {}).get("bonus", True):
        safe_answer_callback(call, "Bonus disabled.", show_alert=True)
        return

    u = get_user(call.from_user.id)
    base = int(cfg().get("daily_bonus", 50))
    last = normalize_db_datetime(u.get("last_bonus_at"))
    now = now_utc()

    if last:
        elapsed = (now - last).total_seconds()
        if elapsed < 86400:
            rem = int(86400 - elapsed)
            hh = rem // 3600
            mm = (rem % 3600) // 60
            safe_answer_callback(call, f"Come back in {hh}h {mm}m", show_alert=True)
            return
        streak = u.get("daily_streak", 0) + 1 if elapsed < 172800 else 1
    else:
        streak = 1

    mult = get_multiplier(streak)
    premium_mult = 2.0 if u.get("is_premium") else 1.0
    amount = int(base * mult * premium_mult)

    users_col.update_one(
        {"user_id": call.from_user.id},
        {"$set": {"last_bonus_at": now, "daily_streak": streak}, "$inc": {"coins": amount}},
    )
    log_tx(call.from_user.id, "daily_bonus", amount, f"streak={streak}")

    txt = (
        f"🎁 <b>Daily Bonus Claimed</b>\n"
        f"Base: {base}\n"
        f"Streak: {streak} (x{mult})\n"
        f"Premium bonus: x{premium_mult}\n"
        f"Received: <code>+{amount}</code> coins"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_redeem")
def cb_open_redeem(call):
    set_state(call.from_user.id, "await_redeem")
    safe_edit(
        call.message.chat.id,
        call.message.message_id,
        "🎟 <b>Send redeem code now.</b>\nType /cancel to stop.",
        reply_markup=back_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "open_shazam")
def cb_open_shazam(call):
    txt = (
        "🎧 <b>Shazam Ultra</b>\n"
        "Send one of these:\n"
        "• audio\n"
        "• voice\n"
        "• video\n"
        "• music file as document\n\n"
        "After recognition, the bot can open direct audio/video download options."
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_tools")
def cb_tools(call):
    txt = (
        "🧰 <b>Tools</b>\n"
        "• Search Music → YouTube-like result list\n"
        "• Download by Link → direct social/video URL\n"
        "• Shazam → identify track from media\n"
        "• Bonus / redeem / referral / premium\n"
        "• Admin control panel"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_search_music")
def cb_open_search_music(call):
    set_state(call.from_user.id, "await_search_query")
    txt = (
        "🎵 <b>Search Music</b>\n"
        "Send a song or video name.\n\n"
        "Examples:\n"
        "• <code>Alan Walker Faded</code>\n"
        "• <code>Arijit Singh Tum Hi Ho</code>\n"
        "• <code>lofi sad remix</code>"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "open_link_download")
def cb_open_link_download(call):
    set_state(call.from_user.id, "await_direct_url")
    txt = (
        "🌐 <b>Download by Link</b>\n"
        "Send a supported public URL.\n\n"
        "Examples:\n"
        "• YouTube video link\n"
        "• Facebook public post / reel link\n"
        "• Instagram public reel / post link\n"
        "• Other yt-dlp supported URLs\n\n"
        "Tip: send the final public post URL, not a broken copied share shortcut if the platform blocks it."
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=back_kb())


# ---------------------------
# Search flow callbacks
# ---------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("sb|"))
def cb_search_back(call):
    _, sid = call.data.split("|", 1)
    text, kb = render_search_results(sid)
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sr|"))
def cb_search_result_open(call):
    _, sid, idx = call.data.split("|")
    text, kb = render_media_entry(sid, int(idx))
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa|"))
def cb_download_audio_from_entry(call):
    if not cfg().get("downloads_enabled", True) or not cfg().get("download_audio_enabled", True):
        bot.answer_callback_query(call.id, "Audio download disabled.", show_alert=True)
        return
    _, sid, idx = call.data.split("|")
    payload = cache_get(sid)
    if not payload:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    entry = payload["entries"][int(idx)]
    start_download_job(call.message.chat.id, entry, mode="audio")
    bot.answer_callback_query(call.id, "Audio download started.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("sv|"))
def cb_prepare_video_menu(call):
    if not cfg().get("downloads_enabled", True) or not cfg().get("download_video_enabled", True):
        bot.answer_callback_query(call.id, "Video download disabled.", show_alert=True)
        return
    _, sid, idx = call.data.split("|")
    idx_int = int(idx)
    payload = cache_get(sid)
    if not payload:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    entry = payload["entries"][idx_int]
    if not entry.get("qualities"):
        try:
            entry["qualities"] = pick_video_qualities(entry.get("webpage_url") or entry.get("search_query") or "")
        except Exception as exc:
            log.warning("quality extraction failed, using best fallback: %s", exc)
            entry["qualities"] = []
    if not entry.get("qualities"):
        start_download_job(call.message.chat.id, entry, mode="video", quality=None)
        bot.answer_callback_query(call.id, "No quality list from source. Downloading best compatible video.", show_alert=True)
        return
    text, kb = render_video_quality_menu(sid, idx_int)
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("vqlarge|"))
def cb_video_too_large(call):
    safe_answer_callback(call, f"This quality is too large for safe bot upload. Choose a smaller quality under {upload_limit_text()}.", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("vq|"))
def cb_download_video_selected(call):
    if not cfg().get("downloads_enabled", True) or not cfg().get("download_video_enabled", True):
        bot.answer_callback_query(call.id, "Video download disabled.", show_alert=True)
        return
    _, sid, idx, qidx = call.data.split("|")
    payload = cache_get(sid)
    if not payload:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    entry = payload["entries"][int(idx)]
    qualities = entry.get("qualities") or []
    qindex = int(qidx)
    if qindex < 0 or qindex >= len(qualities):
        bot.answer_callback_query(call.id, "Invalid quality.", show_alert=True)
        return
    quality = qualities[qindex]
    if is_over_upload_limit(quality.get("filesize")):
        safe_answer_callback(call, f"This quality is too large for safe bot upload. Choose a smaller quality under {upload_limit_text()}.", show_alert=True)
        return
    start_download_job(call.message.chat.id, entry, mode="video", quality=quality)
    bot.answer_callback_query(call.id, f"Video download started: {quality.get('label', 'best')}")


# ---------------------------
# Admin callbacks
# ---------------------------
@bot.callback_query_handler(func=lambda c: c.data == "open_admin")
def cb_admin(call):
    if not is_admin(call.from_user.id):
        safe_answer_callback(call, "Admin only", show_alert=True)
        return
    safe_edit(call.message.chat.id, call.message.message_id, "👨‍💻 <b>Admin Panel</b>", reply_markup=admin_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_stats")
def cb_adm_stats(call):
    if not is_admin(call.from_user.id):
        return
    st = get_stats()
    txt = (
        f"📊 <b>Bot Stats</b>\n"
        f"👥 Users: <code>{st['users']}</code>\n"
        f"👑 Premium: <code>{st['premium']}</code>\n"
        f"🚫 Banned: <code>{st['banned']}</code>\n"
        f"🎟 Codes: <code>{st['codes']}</code>\n"
        f"👨‍💻 Admins: <code>{st['admins']}</code>"
    )
    safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=admin_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_set_bonus")
def cb_adm_set_bonus(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_set_bonus")
    safe_edit(call.message.chat.id, call.message.message_id, "🎁 Send new daily bonus amount.\nExample: <code>75</code>", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_create_redeem")
def cb_adm_create_redeem(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_create_redeem")
    safe_edit(
        call.message.chat.id,
        call.message.message_id,
        "🎟 Send redeem format:\n<code>CODE AMOUNT MAXUSES</code>\nExample:\n<code>ULTRA100 100 50</code>",
        reply_markup=back_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "adm_add_admin")
def cb_adm_add_admin(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_add_admin")
    safe_edit(call.message.chat.id, call.message.message_id, "👥 Send user id to add as admin.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_remove_admin")
def cb_adm_remove_admin(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_remove_admin")
    safe_edit(call.message.chat.id, call.message.message_id, "➖ Send user id to remove from admin.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_add_coins")
def cb_adm_add_coins(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_add_coins")
    safe_edit(call.message.chat.id, call.message.message_id, "➕ Send format: <code>USER_ID AMOUNT</code>", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_give_premium")
def cb_adm_give_premium(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_give_premium")
    safe_edit(call.message.chat.id, call.message.message_id, "👑 Send user id to give premium.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_ban_user")
def cb_adm_ban_user(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_ban_user")
    safe_edit(call.message.chat.id, call.message.message_id, "🚫 Send format: <code>USER_ID reason here</code>", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_unban_user")
def cb_adm_unban_user(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_unban_user")
    safe_edit(call.message.chat.id, call.message.message_id, "✅ Send user id to unban.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_broadcast")
def cb_adm_broadcast(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_broadcast")
    safe_edit(call.message.chat.id, call.message.message_id, "📢 Send broadcast text now.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_force_join")
def cb_adm_force_join(call):
    if not is_admin(call.from_user.id):
        return
    set_state(call.from_user.id, "adm_force_join")
    safe_edit(call.message.chat.id, call.message.message_id, "📢 Send channel username like <code>@mychannel</code>\nOr send <code>off</code>", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data == "adm_maintenance")
def cb_adm_maintenance(call):
    if not is_admin(call.from_user.id):
        return
    current = bool(cfg().get("maintenance_mode", False))
    config_col.update_one({"_id": "global"}, {"$set": {"maintenance_mode": not current}})
    bot.answer_callback_query(call.id, f"Maintenance set to {not current}", show_alert=True)
    cb_admin(call)


@bot.callback_query_handler(func=lambda c: c.data == "adm_toggle_shazam")
def cb_adm_toggle_shazam(call):
    if not is_admin(call.from_user.id):
        return
    current = bool(cfg().get("feature_flags", {}).get("shazam", True))
    config_col.update_one({"_id": "global"}, {"$set": {"feature_flags.shazam": not current}})
    bot.answer_callback_query(call.id, f"Shazam set to {not current}", show_alert=True)
    cb_admin(call)


@bot.callback_query_handler(func=lambda c: c.data == "adm_toggle_downloads")
def cb_adm_toggle_downloads(call):
    if not is_admin(call.from_user.id):
        return
    current = bool(cfg().get("downloads_enabled", True))
    config_col.update_one({"_id": "global"}, {"$set": {"downloads_enabled": not current}})
    bot.answer_callback_query(call.id, f"Downloads set to {not current}", show_alert=True)
    cb_admin(call)


# ---------------------------
# Text handler / states
# ---------------------------
@bot.message_handler(content_types=["text"])
def on_text(message):
    ensure_user(message)
    if not middleware_ok(message.chat.id, message.from_user.id):
        return

    st = get_state(message.from_user.id).get("state", "")
    txt = (message.text or "").strip()
    raw_url = extract_first_url(txt)
    if not txt:
        bot.reply_to(message, "ℹ️ Send text, a search query, or a URL.")
        return

    # Advanced UX fallback:
    # If user sends a direct URL without opening "Download by Link" first,
    # process it automatically instead of forcing button flow.
    if st != "await_direct_url" and is_url(raw_url):
        st = "await_direct_url"

    if st == "await_redeem":
        if not cfg().get("feature_flags", {}).get("redeem", True):
            bot.reply_to(message, "❌ Redeem disabled.")
            return
        code = txt.upper()
        item = redeem_col.find_one({"code": code})
        if not item:
            bot.reply_to(message, "❌ Invalid code.")
            return
        if item.get("expires_at") and now_utc() > item["expires_at"]:
            bot.reply_to(message, "❌ Code expired.")
            return
        used_by = item.get("used_by", [])
        max_uses = int(item.get("max_uses", 1))
        if message.from_user.id in used_by:
            bot.reply_to(message, "❌ You already used this code.")
            return
        if len(used_by) >= max_uses:
            bot.reply_to(message, "❌ Code usage limit reached.")
            return
        amount = int(item.get("amount", 0))
        users_col.update_one({"user_id": message.from_user.id}, {"$inc": {"coins": amount}})
        redeem_col.update_one({"code": code}, {"$push": {"used_by": message.from_user.id}})
        clear_state(message.from_user.id)
        log_tx(message.from_user.id, "redeem", amount, code)
        bot.reply_to(message, f"✅ Redeemed successfully. +{amount} coins")
        bot.send_message(message.chat.id, home_text(message.from_user.id), reply_markup=main_kb(message.from_user.id))
        return

    if st == "await_search_query":
        clear_state(message.from_user.id)
        if not YTDLP_OK:
            bot.reply_to(message, "❌ yt-dlp not installed.")
            return
        status = bot.reply_to(message, "🔎 <i>Searching…</i>")
        animate(message.chat.id, status.message_id, progress_frames("search"), 0.6)
        try:
            results = search_media(txt, limit=8)
            if not results:
                safe_edit(message.chat.id, status.message_id, "❌ No results found.", reply_markup=back_kb())
                return
            sid = cache_put({"type": "search", "query": txt, "entries": results})
            text, kb = render_search_results(sid)
            safe_edit(message.chat.id, status.message_id, text, reply_markup=kb)
        except Exception as exc:
            safe_edit(message.chat.id, status.message_id, f"❌ <b>Search failed</b>\n<code>{esc(str(exc)[:300])}</code>", reply_markup=back_kb())
        return

    if st == "await_direct_url":
        normalized_url = normalize_public_url(raw_url or txt)
        if not is_url(normalized_url):
            bot.reply_to(message, "❌ Please send a valid URL starting with http:// or https://")
            return
        status = bot.reply_to(message, "🌐 <i>Reading media info…</i>")
        animate(message.chat.id, status.message_id, progress_frames("search"), 0.6)
        try:
            info = extract_direct_info(normalized_url)
            clear_state(message.from_user.id)
            sid = cache_put({"type": "direct", "query": normalized_url, "entries": [info]})
            text, kb = render_media_entry(sid, 0)
            safe_edit(message.chat.id, status.message_id, text, reply_markup=kb)
        except Exception as exc:
            safe_edit(
                message.chat.id,
                status.message_id,
                (
                    "❌ <b>Link processing failed</b>\n"
                    f"<code>{esc(str(exc)[:260])}</code>\n\n"
                    "Try:\n"
                    "• Resend the final public post/video URL (not copied shortcut)\n"
                    "• For Instagram/Facebook, open post in browser and copy full URL\n"
                    "• If TikTok short link fails, open it once in browser and copy final URL\n"
                    "• If private/age-locked, public access is required"
                ),
                reply_markup=back_kb(),
            )
        return

    if st == "adm_set_bonus" and is_admin(message.from_user.id):
        try:
            amount = int(txt)
            if amount < 1 or amount > 100000:
                raise ValueError
            config_col.update_one({"_id": "global"}, {"$set": {"daily_bonus": amount}})
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Daily bonus updated to {amount}")
        except Exception:
            bot.reply_to(message, "❌ Invalid amount.")
        return

    if st == "adm_create_redeem" and is_admin(message.from_user.id):
        try:
            parts = txt.split()
            code = parts[0].upper()
            amount = int(parts[1])
            max_uses = int(parts[2])
            redeem_col.update_one(
                {"code": code},
                {
                    "$set": {
                        "code": code,
                        "amount": amount,
                        "max_uses": max_uses,
                        "used_by": [],
                        "created_by": message.from_user.id,
                        "created_at": now_utc(),
                        "expires_at": None,
                    }
                },
                upsert=True,
            )
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Redeem code created: {code}")
        except Exception:
            bot.reply_to(message, "❌ Wrong format. Use: CODE AMOUNT MAXUSES")
        return

    if st == "adm_add_admin" and is_admin(message.from_user.id):
        try:
            uid = int(txt)
            config_col.update_one({"_id": "global"}, {"$addToSet": {"admins": uid}})
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Added admin: {uid}")
        except Exception:
            bot.reply_to(message, "❌ Invalid user id.")
        return

    if st == "adm_remove_admin" and is_admin(message.from_user.id):
        try:
            uid = int(txt)
            if uid == OWNER_ID:
                bot.reply_to(message, "❌ Owner cannot be removed.")
                return
            config_col.update_one({"_id": "global"}, {"$pull": {"admins": uid}})
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Removed admin: {uid}")
        except Exception:
            bot.reply_to(message, "❌ Invalid user id.")
        return

    if st == "adm_add_coins" and is_admin(message.from_user.id):
        try:
            uid, amount = txt.split(maxsplit=1)
            uid = int(uid)
            amount = int(amount)
            users_col.update_one({"user_id": uid}, {"$inc": {"coins": amount}}, upsert=True)
            log_tx(uid, "admin_add_coins", amount, f"by {message.from_user.id}")
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Added {amount} coins to {uid}")
        except Exception:
            bot.reply_to(message, "❌ Wrong format. USER_ID AMOUNT")
        return

    if st == "adm_give_premium" and is_admin(message.from_user.id):
        try:
            uid = int(txt)
            users_col.update_one({"user_id": uid}, {"$set": {"is_premium": True}}, upsert=True)
            log_tx(uid, "admin_give_premium", 0, f"by {message.from_user.id}")
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ Premium given to {uid}")
        except Exception:
            bot.reply_to(message, "❌ Invalid user id.")
        return

    if st == "adm_ban_user" and is_admin(message.from_user.id):
        try:
            uid_text, reason = txt.split(maxsplit=1)
            uid = int(uid_text)
            if uid == OWNER_ID:
                bot.reply_to(message, "❌ Owner cannot be banned.")
                return
            users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True, "ban_reason": reason}}, upsert=True)
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ User {uid} banned.")
        except Exception:
            bot.reply_to(message, "❌ Wrong format. USER_ID reason here")
        return

    if st == "adm_unban_user" and is_admin(message.from_user.id):
        try:
            uid = int(txt)
            users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False, "ban_reason": ""}}, upsert=True)
            clear_state(message.from_user.id)
            bot.reply_to(message, f"✅ User {uid} unbanned.")
        except Exception:
            bot.reply_to(message, "❌ Invalid user id.")
        return

    if st == "adm_broadcast" and is_admin(message.from_user.id):
        clear_state(message.from_user.id)
        sent = 0
        failed = 0
        for doc in users_col.find({}, {"user_id": 1}):
            uid = doc.get("user_id")
            if not uid:
                continue
            try:
                bot.send_message(uid, txt)
                sent += 1
                time.sleep(0.05)
            except Exception:
                failed += 1
        bot.reply_to(message, f"✅ Broadcast finished. Sent: {sent}, Failed: {failed}")
        return

    if st == "adm_force_join" and is_admin(message.from_user.id):
        value = txt.strip()
        if value.lower() == "off":
            config_col.update_one({"_id": "global"}, {"$set": {"force_sub_channel": ""}})
            clear_state(message.from_user.id)
            bot.reply_to(message, "✅ Force join disabled.")
            return
        if not value.startswith("@"):
            bot.reply_to(message, "❌ Must start with @")
            return
        config_col.update_one({"_id": "global"}, {"$set": {"force_sub_channel": value}})
        clear_state(message.from_user.id)
        bot.reply_to(message, f"✅ Force join set to {value}")
        return

    bot.reply_to(message, "ℹ️ Use the buttons. Type /start for the main menu.")


# ---------------------------
# Shazam media handler
# ---------------------------
@bot.message_handler(content_types=["audio", "voice", "video", "document"])
def on_media(message):
    ensure_user(message)
    if not middleware_ok(message.chat.id, message.from_user.id):
        return
    if not cfg().get("feature_flags", {}).get("shazam", True):
        bot.reply_to(message, "❌ Shazam feature disabled.")
        return
    if not SHAZAM_OK:
        bot.reply_to(message, "❌ shazamio not installed.")
        return

    file_id = None
    if message.audio:
        file_id = message.audio.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        bot.reply_to(message, "❌ Unsupported file.")
        return

    status = bot.reply_to(message, "🎧 <i>Processing media…</i>")
    animate(message.chat.id, status.message_id, progress_frames("shazam"), 0.85)

    temp_dir = TMP_DIR / f"shazam_{message.chat.id}_{message.message_id}_{uuid.uuid4().hex[:6]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_in = temp_dir / "input.bin"
    temp_audio = temp_dir / "input.wav"

    try:
        info = bot.get_file(file_id)
        content = bot.download_file(info.file_path)
        ext = os.path.splitext(info.file_path)[1] or ".bin"
        temp_in = temp_dir / f"input{ext}"
        with open(temp_in, "wb") as fh:
            fh.write(content)

        file_to_scan = temp_in
        try:
            subprocess.run(
                [
                    ffmpeg_path,
                    "-i",
                    str(temp_in),
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(temp_audio),
                    "-y",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
            if temp_audio.exists():
                file_to_scan = temp_audio
        except Exception:
            pass

        async def recognize() -> dict:
            return await shazam.recognize(str(file_to_scan))

        result = asyncio.run(recognize())
        track = (result or {}).get("track") if isinstance(result, dict) else None
        if not track:
            safe_edit(message.chat.id, status.message_id, "❌ <b>No song matched.</b>", reply_markup=back_kb())
            return

        title = track.get("title", "Unknown")
        artist = track.get("subtitle", "Unknown Artist")
        cover = track.get("images", {}).get("coverarthq", "")
        query = f"{title} {artist}".strip()
        entry = {
            "title": f"{title} - {artist}",
            "webpage_url": "",
            "duration": None,
            "uploader": artist,
            "view_count": None,
            "thumbnail": cover,
            "extractor": "Shazam Match",
            "search_query": query,
            "qualities": [],
        }
        sid = cache_put({"type": "shazam", "query": query, "entries": [entry]})
        text, kb = render_media_entry(sid, 0)
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass
        if cover:
            try:
                bot.send_photo(message.chat.id, cover, caption=text, reply_markup=kb)
            except Exception:
                bot.send_message(message.chat.id, text, reply_markup=kb)
        else:
            bot.send_message(message.chat.id, text, reply_markup=kb)
    except Exception as exc:
        safe_edit(message.chat.id, status.message_id, f"❌ <b>Recognition failed</b>\n<code>{esc(str(exc)[:300])}</code>", reply_markup=back_kb())
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------
# Download execution
# ---------------------------
def start_download_job(chat_id: int, entry: Dict[str, Any], mode: str, quality: Optional[Dict[str, Any]] = None) -> None:
    if not acquire_chat_job(chat_id):
        bot.send_message(chat_id, "⏳ Another download is already running in this chat. Wait for it to finish.")
        return

    status = bot.send_message(chat_id, "📥 <i>Preparing download job…</i>")
    DOWNLOAD_POOL.submit(run_download_job, chat_id, status.message_id, entry, mode, quality)


def run_download_job(chat_id: int, status_message_id: int, entry: Dict[str, Any], mode: str, quality: Optional[Dict[str, Any]] = None) -> None:
    job_id = uuid.uuid4().hex[:10]
    work_dir = TMP_DIR / f"job_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = entry.get("webpage_url") or entry.get("search_query") or entry.get("title")
        if not source:
            raise RuntimeError("No media source available")

        safe_edit(chat_id, status_message_id, "🔎 <i>Fetching media info…</i>")
        time.sleep(0.2)

        if mode == "audio":
            safe_edit(chat_id, status_message_id, "📦 <i>Downloading best audio…</i>")
            result = download_audio(source, work_dir)
            info = result.get("info") or {}
            file_path: Path = result["path"]
            size = file_path.stat().st_size if file_path.exists() else None
            if is_over_upload_limit(size):
                raise RuntimeError(
                    f"Audio file is too large for bot upload. File size: {format_bytes(size)}. Please try a shorter track or smaller source. Safe limit: {upload_limit_text()}."
                )
            caption = (
                f"🎧 <b>{esc(info.get('title') or entry.get('title') or 'Audio')}</b>\n"
                f"👤 Uploader: <code>{esc(info.get('uploader') or info.get('channel') or entry.get('uploader') or 'Unknown')}</code>\n"
                f"⏱ Duration: <code>{esc(format_duration(info.get('duration') or entry.get('duration')))}</code>\n"
                f"💾 Size: <code>{esc(format_bytes(size))}</code>\n"
                f"🎵 Format: <code>MP3</code>"
            )
            safe_edit(chat_id, status_message_id, "📤 <i>Uploading audio…</i>")
            with open(file_path, "rb") as audio_fh:
                try:
                    bot.send_audio(chat_id, audio_fh, caption=caption, title=info.get("title") or entry.get("title") or "Audio")
                except Exception as exc:
                    audio_fh.seek(0)
                    err = str(exc).lower()
                    if "too large" in err or "413" in err or "request entity too large" in err:
                        raise RuntimeError(
                            f"Telegram rejected this audio as too large. File size: {format_bytes(size)}. Please try a shorter track. Safe limit: {upload_limit_text()}."
                        )
                    bot.send_document(chat_id, audio_fh, caption=caption, visible_file_name=sanitize_filename((info.get("title") or entry.get("title") or "audio") + ".mp3"))

        elif mode == "video":
            label = quality.get("label") if quality else "best"
            safe_edit(chat_id, status_message_id, f"📦 <i>Downloading video {esc(label)}…</i>")
            result = download_video(
                source,
                format_id=quality.get("format_id") if quality else None,
                max_height=quality.get("height") if quality else None,
                work_dir=work_dir,
            )
            info = result.get("info") or {}
            file_path = result["path"]
            size = file_path.stat().st_size if file_path.exists() else None
            if is_over_upload_limit(size):
                raise RuntimeError(
                    f"Selected video is too large for bot upload. Quality: {label}. File size: {format_bytes(size)}. Please choose a smaller quality under {upload_limit_text()}."
                )
            caption = (
                f"🎞 <b>{esc(info.get('title') or entry.get('title') or 'Video')}</b>\n"
                f"👤 Uploader: <code>{esc(info.get('uploader') or info.get('channel') or entry.get('uploader') or 'Unknown')}</code>\n"
                f"⏱ Duration: <code>{esc(format_duration(info.get('duration') or entry.get('duration')))}</code>\n"
                f"📺 Quality: <code>{esc(label)}</code>\n"
                f"💾 Size: <code>{esc(format_bytes(size))}</code>"
            )
            safe_edit(chat_id, status_message_id, "📤 <i>Uploading video…</i>")
            with open(file_path, "rb") as video_fh:
                try:
                    bot.send_video(chat_id, video_fh, caption=caption, supports_streaming=True)
                except Exception as exc:
                    err = str(exc).lower()
                    if "too large" in err or "413" in err or "request entity too large" in err:
                        raise RuntimeError(
                            f"Telegram rejected this video as too large. Quality: {label}. File size: {format_bytes(size)}. Please choose a smaller quality under {upload_limit_text()}."
                        )
                    video_fh.seek(0)
                    bot.send_document(chat_id, video_fh, caption=caption, visible_file_name=sanitize_filename((info.get("title") or entry.get("title") or "video") + file_path.suffix))
        else:
            raise RuntimeError("Unsupported download mode")

        try:
            bot.delete_message(chat_id, status_message_id)
        except Exception:
            safe_edit(chat_id, status_message_id, "✅ Download completed.", reply_markup=back_kb())

    except Exception as exc:
        safe_edit(chat_id, status_message_id, f"❌ <b>Download failed</b>\n<code>{esc(str(exc)[:400])}</code>", reply_markup=back_kb())
    finally:
        release_chat_job(chat_id)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------
# Bot lifecycle
# ---------------------------
def startup_check() -> None:
    client.admin.command("ping")
    try:
        bot.set_my_commands([
            BotCommand("start", "Open the ultra home menu"),
            BotCommand("help", "Show help"),
            BotCommand("cancel", "Cancel current input state"),
        ])
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)
    log.info("✅ MongoDB connected")
    log.info("✅ ffmpeg detected at %s", ffmpeg_path)
    log.info("✅ safe upload limit set to %s", upload_limit_text())
    log.info("✅ yt-dlp available: %s", YTDLP_OK)
    log.info("✅ shazamio available: %s", SHAZAM_OK)
    cache_cleanup()


def run_bot() -> None:
    startup_check()
    try:
        bot.remove_webhook()
    except Exception:
        pass

    while True:
        try:
            log.info("🚀 Bot polling started")
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except KeyboardInterrupt:
            log.info("Bot stopped by keyboard interrupt")
            break
        except Exception as exc:
            log.exception("Polling crashed: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
