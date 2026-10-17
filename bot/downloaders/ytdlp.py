"""yt-dlp wrapper. yt-dlp always runs as a subprocess so its memory is returned to the OS when it exits.

Always downloads the highest quality available (best video + best audio, merged), or the best audio as
its original file for audio-only links. No conversion."""
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config, procs
from ..utils import JobError, assert_safe_url, free_space

PY = [sys.executable, "-m", "yt_dlp", "--no-warnings"]

_MATCH_CODE = (
    "import sys\n"
    "from yt_dlp.extractor import gen_extractor_classes\n"
    "u = sys.argv[1]\n"
    "for ie in gen_extractor_classes():\n"
    "    if ie.ie_key() != 'Generic' and ie.suitable(u):\n"
    "        print(ie.IE_NAME)\n"
    "        break\n"
)
DISK_CHECK_EVERY = 5  # seconds


@dataclass
class Downloaded:
    path: Path
    was_playlist: bool  # the link was a playlist; only its first item was downloaded


def explain(err: str, has_cookies: bool) -> str:
    """Add an actionable hint to YouTube's bot check."""
    msg = err[:250]
    if "not a bot" in err or "Sign in to confirm" in err:
        msg += (
            "\n\n🍪 YouTube is asking for a login. "
            + ("Your cookies look expired or rotated: export a fresh file and send it with /cookies."
               if has_cookies else
               "Send your YouTube cookies.txt to this chat with the caption /cookies (see /help).")
        )
    return msg


def _cookie_args() -> list[str]:
    return ["--cookies", str(config.COOKIES_FILE)] if config.COOKIES_FILE.is_file() else []


async def match_extractor(url: str) -> str | None:
    """Name of the site-specific yt-dlp extractor for this URL (None = only the generic one applies).
    Offline regex matching in a short-lived subprocess, so the bot never imports yt-dlp itself."""
    proc = await procs.spawn([sys.executable, "-c", _MATCH_CODE, url], stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace").strip() or None


def size_cap(dest_dir: Path) -> int:
    """Largest single stream we can afford. Video and audio are downloaded separately and then merged,
    so peak disk use is about twice the final size."""
    return min(config.MAX_DOWNLOAD, (free_space(dest_dir) - config.MIN_FREE_SPACE) // 2)


def format_args(cap: int) -> list[str]:
    """Best video + best audio, restricted to streams that fit under `cap` (streams of unknown size pass)."""
    fits = f"[filesize<?{cap}][filesize_approx<?{cap}]"
    args = ["-f", f"bv*{fits}+ba{fits}/b{fits}"]
    if config.YTDL_SORT:
        args.append(f"--format-sort={config.YTDL_SORT}")
    return args


def _num(s: str) -> int:
    try:
        return int(float(s))
    except ValueError:
        return 0


async def download(url: str, dest_dir: Path, on_progress, on_wait=None) -> Downloaded:
    await assert_safe_url(url)
    cap = size_cap(dest_dir)
    if cap < 50 * 1024 * 1024:
        raise JobError("Not enough free disk space.")

    cmd = [
        *PY, "--no-playlist", "--playlist-items", "1",   # a playlist link must not download the whole playlist
        "--match-filter", "!is_live",                      # live streams never end
        "--progress", "--newline",
        "--progress-template",
        "download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        # NB: a bare word would be read as a field name, so each marker carries a real field.
        "--print", "playlist:PLAYLIST %(id)s",       # printed once when the link is a playlist
        "--print", "after_filter:PASSED %(id)s",     # printed only for videos that pass --match-filter
        "--print", "after_move:FILE %(filepath)s",
        "--max-filesize", str(cap),
        "-o", str(dest_dir / "%(title).100B [%(id)s].%(ext)s"),
        *format_args(cap), *_cookie_args(), url,
    ]
    has_cookies = config.COOKIES_FILE.is_file()

    filepath, last_err, passed, playlist, disk_full = None, "", False, False, False
    next_check = time.monotonic() + DISK_CHECK_EVERY
    async with procs.heavy_slot(on_wait):
        proc = await procs.spawn(cmd)
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("PROG "):
                    parts = line.split()[1:]
                    if len(parts) >= 3:
                        await on_progress(_num(parts[0]), _num(parts[1]) or _num(parts[2]))
                elif line.startswith("FILE "):
                    filepath = line[5:]
                elif line.startswith("PLAYLIST "):
                    playlist = True
                elif line.startswith("PASSED "):
                    passed = True
                elif line.startswith("ERROR"):
                    last_err = line
                if time.monotonic() >= next_check:  # sizes of HLS/DASH streams are unknown: watch the disk instead
                    next_check = time.monotonic() + DISK_CHECK_EVERY
                    if free_space(dest_dir) < config.MIN_FREE_SPACE // 2:
                        disk_full = True
                        proc.kill()
                        break
            rc = await proc.wait()
        finally:
            if proc.returncode is None:  # cancelled or crashed: don't leave it running
                proc.kill()
                await proc.wait()

    if disk_full:
        raise JobError("The disk is almost full, so the download was stopped. Free some space or lower MAX_DOWNLOAD_MB.")
    if procs.killed_by_signal_9(rc):
        raise JobError(procs.OOM_MESSAGE)
    if rc == 0 and not passed and not filepath:  # yt-dlp skipped it: the only filter we set is "not live"
        raise JobError("Nothing was downloaded. Live streams aren't supported (they never end), and the link may have no media.")
    if rc != 0 or not filepath or not Path(filepath).exists():
        raise JobError(explain(last_err or "Nothing was downloaded (the video may be larger than the allowed size).", has_cookies))
    return Downloaded(Path(filepath), playlist)
