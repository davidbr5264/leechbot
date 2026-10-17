"""Settings, read once from the environment / .env file.

Required values fail with a clear message. Optional numeric values that are malformed fall back to their
default instead of crashing the service in a restart loop."""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _clean(value: str) -> str:
    return value.strip().strip("\"'").strip()


def _required(name: str, pattern: str) -> str:
    value = _clean(os.environ.get(name, ""))
    if not value:
        sys.exit(f"Missing {name}. Set it in {BASE_DIR / '.env'} (or run: leechbot settings).")
    if not re.fullmatch(pattern, value):
        sys.exit(f"{name} in {BASE_DIR / '.env'} doesn't look valid. Fix it (or run: leechbot settings).")
    return value


def _number(name: str, default: int, low: int, high: int) -> int:
    raw = _clean(os.environ.get(name, ""))
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"warning: {name}={raw!r} is not a whole number; using {default}", file=sys.stderr)
        return default
    if not low <= value <= high:
        print(f"warning: {name}={value} is outside {low}..{high}; using {default}", file=sys.stderr)
        return default
    return value


API_ID = int(_required("API_ID", r"\d{1,15}"))
API_HASH = _required("API_HASH", r"[0-9a-fA-F]{32}")
BOT_TOKEN = _required("BOT_TOKEN", r"\d{5,}:[A-Za-z0-9_-]{30,}")
OWNER_ID = int(_required("OWNER_ID", r"\d{1,15}"))

MiB = 1024 * 1024
DOWNLOAD_DIR = Path(_clean(os.environ.get("DOWNLOAD_DIR", "")) or "/var/tmp/leechbot")
# Max size of ONE Telegram upload; bigger files are split into parts of this size.
MAX_FILE_SIZE = _number("MAX_FILE_SIZE_MB", 1990, 50, 4000) * MiB
# Max size of a whole download (before splitting).
MAX_DOWNLOAD = _number("MAX_DOWNLOAD_MB", 8000, 100, 1_000_000) * MiB
# Never let a download push free disk space below this.
MIN_FREE_SPACE = _number("MIN_FREE_MB", 300, 0, 100_000) * MiB
# Wait (up to 2 min) until this much RAM is free before starting yt-dlp/ffmpeg.
MIN_FREE_RAM_MB = _number("MIN_FREE_RAM_MB", 150, 0, 8000)
MAX_QUEUE = _number("MAX_QUEUE", 20, 1, 100)  # jobs waiting at once
MAX_URLS_PER_MESSAGE = 5

# Managed with the /cookies command; used by yt-dlp automatically when the file exists.
COOKIES_FILE = Path(_clean(os.environ.get("COOKIES_FILE", "")) or BASE_DIR / "cookies.txt")
# Optional yt-dlp format sort, e.g. "res:1080" or "vcodec:h264" (default: yt-dlp's own "best quality" order).
_sort = _clean(os.environ.get("YTDL_SORT", ""))
YTDL_SORT = _sort if re.fullmatch(r"[A-Za-z0-9:,+~*._-]+", _sort) else ""
if _sort and not YTDL_SORT:
    print(f"warning: YTDL_SORT={_sort!r} has unsupported characters; ignoring it", file=sys.stderr)

# Seconds between progress message edits (avoids Telegram flood limits).
PROGRESS_INTERVAL = 7
# Written while a job runs so a crash/restart can tell you what was interrupted.
STATE_FILE = BASE_DIR / ".active_job.json"
