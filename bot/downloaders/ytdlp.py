"""yt-dlp wrapper. yt-dlp always runs as a subprocess so its memory is returned to the OS when it exits."""
import asyncio
import json
import sys
from dataclasses import dataclass, field
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
# Compact output: a metadata object and the formats list (the full -J dump is multi-MB on YouTube).
_META_TEMPLATE = "%(.{title,duration,uploader,is_live,live_status,_type})j"
_FORMATS_TEMPLATE = "%(formats.:.{format_id,height,vcodec,acodec,ext,filesize,filesize_approx,tbr,abr})j"


@dataclass
class Option:
    height: int | None  # None = the link gives no resolution info: offer "best available"
    size: int | None  # estimated bytes (video + best audio), None if unknown

    @property
    def quality(self) -> str:
        return "best" if self.height is None else str(self.height)

    @property
    def label(self) -> str:
        return "Best available" if self.height is None else f"{self.height}p"


@dataclass
class MediaInfo:
    title: str = ""
    duration: int | None = None
    uploader: str | None = None
    options: list = field(default_factory=list)  # [Option], highest first
    has_audio: bool = False

    @property
    def mp3_size(self) -> int | None:
        if not self.duration:
            return None
        return int(self.duration * _mp3_kbps() * 125)


def _mp3_kbps() -> int:
    q = config.MP3_QUALITY.upper().rstrip("K")
    return int(q) if q.isdigit() and int(q) > 9 else 245  # "0"-"9" are VBR levels: V0 averages ~245


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
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace").strip() or None


def build_info(meta: dict, fmts: list[dict]) -> MediaInfo:
    """Turn yt-dlp's metadata + format list into the choices we offer. Pure function."""
    if meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming"):
        raise JobError("Live streams aren't supported.")
    dur = meta.get("duration")
    info = MediaInfo(
        title=(meta.get("title") or "Untitled").strip(),
        duration=int(dur) if dur else None,
        uploader=meta.get("uploader"),
    )

    def has(codec) -> bool:  # a codec is known to be present
        return codec not in (None, "none", "")

    def maybe(codec) -> bool:  # present OR unknown (plain file links often carry no codec info)
        return codec != "none"

    def size_of(f: dict) -> int | None:
        s = f.get("filesize") or f.get("filesize_approx")
        if s:
            return int(s)
        rate = f.get("tbr") or f.get("abr")  # kbit/s
        return int(rate * 125 * dur) if rate and dur else None

    info.has_audio = any(maybe(f.get("acodec")) for f in fmts)
    audio_only = [f for f in fmts if has(f.get("acodec")) and not has(f.get("vcodec"))]
    best_audio = max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_size = size_of(best_audio) if best_audio else None

    by_height: dict[int, list[dict]] = {}
    for f in fmts:
        if has(f.get("vcodec")) and f.get("height"):
            by_height.setdefault(int(f["height"]), []).append(f)

    for h in sorted(by_height, reverse=True):
        cands = by_height[h]
        # the downloader prefers H.264 (plays inline in Telegram); estimate the same stream
        pick = next((f for f in cands if str(f.get("vcodec", "")).startswith(("avc", "h264"))), cands[0])
        size = size_of(pick)
        if size is not None and not has(pick.get("acodec")) and audio_size:
            size += audio_size
        if size is not None and size > config.MAX_DOWNLOAD:
            continue  # couldn't be downloaded anyway
        info.options.append(Option(h, size))

    if not info.options:
        # No format reports a height. If something could still be a video (unknown or present
        # video codec), offer one "best available" choice instead of pretending there is nothing.
        unknown = [f for f in fmts if maybe(f.get("vcodec")) and not (has(f.get("acodec")) and f.get("vcodec") == "none")]
        if unknown:
            best = max(unknown, key=lambda f: size_of(f) or 0)
            size = size_of(best)
            if size is None or size <= config.MAX_DOWNLOAD:
                info.options.append(Option(None, size))
    return info


async def probe(url: str, on_wait=None) -> MediaInfo:
    """Ask yt-dlp which resolutions this link really offers."""
    await assert_safe_url(url)
    cmd = [
        *PY, "--no-playlist", "--flat-playlist", "--playlist-end", "1", "--skip-download",
        "--print", _META_TEMPLATE, "--print", _FORMATS_TEMPLATE,
        *_cookie_args(), url,
    ]
    async with procs.heavy_slot(on_wait):
        proc = await procs.spawn(cmd, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise JobError("Timed out while checking that link.") from None
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

    if procs.killed_by_signal_9(proc.returncode):
        raise JobError(procs.OOM_MESSAGE)
    lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        last = next((ln for ln in reversed(err.decode(errors="replace").splitlines()) if "ERROR" in ln), "")
        raise JobError(explain(last or "Couldn't read that link (unsupported or private?).", config.COOKIES_FILE.is_file()))
    try:
        meta = json.loads(lines[0])
        fmts = json.loads(lines[1]) if len(lines) > 1 and lines[1] not in ("null", "NA") else []
    except json.JSONDecodeError:
        raise JobError("Couldn't understand yt-dlp's answer for that link.") from None
    if meta.get("_type") in ("playlist", "multi_video"):
        raise JobError("That's a playlist. Send a link to a single video.")
    info = build_info(meta, fmts or [])
    if not info.options and not info.has_audio:
        raise JobError("No downloadable video or audio found at that link.")
    return info


def _format_args(quality: str) -> list[str]:
    if quality == "audio":
        # Highest-bitrate source stream, then MP3 at the encoder's maximum (320 kbit/s CBR by default).
        return ["-f", "bestaudio/best", "-S", "abr", "-x", "--audio-format", "mp3",
                "--audio-quality", config.MP3_QUALITY, "--embed-metadata"]
    if quality == "best":
        return ["-S", "vcodec:h264,acodec:aac", "--merge-output-format", "mp4"]
    # Cap at the chosen height, prefer H.264 + AAC (plays inline in Telegram; yt-dlp's default
    # sort would pick VP9/AV1), merge into mp4 without re-encoding.
    return ["-S", f"res:{quality},vcodec:h264,acodec:aac", "--merge-output-format", "mp4"]


def _num(s: str) -> int:
    try:
        return int(float(s))
    except ValueError:
        return 0


async def download(url: str, dest_dir: Path, quality: str, on_progress, on_wait=None) -> Path:
    if quality not in ("audio", "best") and not quality.isdigit():
        raise JobError(f"Unknown quality '{quality}'.")
    await assert_safe_url(url)
    if free_space(dest_dir) < config.MIN_FREE_SPACE:
        raise JobError("Not enough free disk space.")

    cmd = [
        *PY, "--no-playlist", "--quiet", "--progress", "--newline",
        "--progress-template",
        "download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        "--print", "after_move:FILE %(filepath)s",
        "--max-filesize", str(config.MAX_DOWNLOAD),
        "-o", str(dest_dir / "%(title).100B [%(id)s].%(ext)s"),
        *_format_args(quality), *_cookie_args(), url,
    ]
    has_cookies = config.COOKIES_FILE.is_file()

    filepath, last_err = None, ""
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
                elif line.startswith("ERROR"):
                    last_err = line
            rc = await proc.wait()
        finally:
            if proc.returncode is None:  # cancelled or crashed: don't leave it running
                proc.kill()
                await proc.wait()

    if procs.killed_by_signal_9(rc):
        raise JobError(procs.OOM_MESSAGE)
    if rc != 0 or not filepath or not Path(filepath).exists():
        raise JobError(explain(last_err or "yt-dlp produced no file (maybe over the size limit).", has_cookies))
    return Path(filepath)
