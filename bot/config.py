import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/var/tmp/leechbot"))
# Max size of ONE Telegram upload; bigger files are split into parts of this size.
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "1990")) * 1024 * 1024
# Max size of a whole download (before splitting).
MAX_DOWNLOAD = int(os.getenv("MAX_DOWNLOAD_MB", "8000")) * 1024 * 1024
MIN_FREE_SPACE = int(os.getenv("MIN_FREE_MB", "300")) * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent.parent
# Managed with the /cookies command; used by yt-dlp automatically when the file exists.
COOKIES_FILE = Path(os.getenv("COOKIES_FILE") or BASE_DIR / "cookies.txt")

# Seconds between progress message edits (avoids Telegram flood limits).
PROGRESS_INTERVAL = 7

# --- memory safety / behaviour -------------------------------------------------
# Wait (up to 2 min) until this much RAM is free before starting yt-dlp/ffmpeg.
MIN_FREE_RAM_MB = int(os.getenv("MIN_FREE_RAM_MB", "150"))
# MP3 encoder setting for audio downloads. 320K = constant 320 kbit/s, the MP3 maximum.
MP3_QUALITY = os.getenv("MP3_QUALITY", "320K")
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "20"))           # jobs waiting at once
MAX_URLS_PER_MESSAGE = 5
# Written while a job runs so a crash/restart can tell you what was interrupted.
STATE_FILE = BASE_DIR / ".active_job.json"
