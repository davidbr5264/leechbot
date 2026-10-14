"""Find URLs in a message and decide how to handle each one (no commands needed)."""
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import config
from .downloaders import direct, ytdlp
from .utils import JobError

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\"”’»…"
_NAME_RE = re.compile(r"^\s*(https?://\S+)\s*\|\s*(.+?)\s*$", re.DOTALL | re.IGNORECASE)

# Streams: yt-dlp understands these, a plain download would just fetch a tiny playlist.
STREAM_EXT = {".m3u8", ".mpd"}
# A URL ending in one of these is a file: download it as-is.
FILE_EXT = {
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".mpg", ".mpeg", ".3gp", ".ogv",
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".aiff", ".alac",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".iso", ".img", ".dmg",
    ".pdf", ".epub", ".mobi", ".azw3", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".json",
    ".apk", ".exe", ".msi", ".deb", ".rpm", ".appimage", ".bin", ".torrent",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".heic",
}


@dataclass
class Verdict:
    kind: str  # "direct" | "ytdl"
    head: direct.Head | None = None  # what the server said (direct links probed over the network)
    extractor: str | None = None


def extract_urls(text: str, entities=None) -> list[tuple[str, str | None]]:
    """[(url, custom_name)] in order of appearance. Handles hidden hyperlinks and `url | new_name.ext`."""
    text = text or ""
    found: list[str] = []
    for m in _URL_RE.finditer(text):
        found.append(m.group(0).rstrip(_TRAILING))
    for e in entities or []:  # "text link" entities hide the URL behind words
        u = getattr(e, "url", None)
        if isinstance(u, str) and u.lower().startswith(("http://", "https://")):
            found.append(u)
    urls = list(dict.fromkeys(u for u in found if len(u) > 8))[: config.MAX_URLS_PER_MESSAGE]

    name = None
    if len(urls) == 1:
        m = _NAME_RE.match(text)
        if m and m.group(1).rstrip(_TRAILING) == urls[0]:
            name = m.group(2).splitlines()[0].strip() or None
    return [(u, name if i == 0 else None) for i, u in enumerate(urls)]


def _ext(url: str) -> str:
    return PurePosixPath(urlsplit(url).path).suffix.lower()


async def classify(url: str) -> Verdict:
    """Cheapest check first: extension, then yt-dlp's offline URL matchers, then ask the server."""
    ext = _ext(url)
    if ext in STREAM_EXT:
        return Verdict("ytdl")
    if ext in FILE_EXT:
        return Verdict("direct")

    name = await ytdlp.match_extractor(url)  # YouTube, Vimeo, TikTok, Instagram, ...
    if name:
        return Verdict("ytdl", extractor=name)

    head = await direct.probe(url)  # also rejects private/local addresses
    if head.status >= 400 and head.status not in (403, 405, 416):
        raise JobError(f"That link returned HTTP {head.status}.")
    if head.content_type in ("text/html", "application/xhtml+xml"):
        return Verdict("ytdl", head=head)  # a web page: maybe it embeds a video
    return Verdict("direct", head=head)
