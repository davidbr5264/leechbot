"""Validation for uploaded Netscape-format cookies files (used by yt-dlp)."""
import re
from dataclasses import dataclass

from .utils import JobError

MAX_BYTES = 1_000_000
_HEADER = re.compile(r"^#( Netscape)? HTTP Cookie File")
# Cookies that only exist when logged in to a Google/YouTube account.
_LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "LOGIN_INFO"}


@dataclass
class CookieInfo:
    text: str  # normalised (LF newlines, starts at the header line)
    total: int
    google: int  # cookies for youtube.com / google.com
    logged_in: bool


def parse(raw: bytes) -> CookieInfo:
    if len(raw) > MAX_BYTES:
        raise JobError("That file is too big to be a cookies file (limit 1 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise JobError("That isn't a text file. I need a cookies.txt export.") from None

    # Windows (CRLF) newlines make yt-dlp fail with HTTP 400, so normalise to LF.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not _HEADER.match(lines[0]):
        raise JobError(
            "This isn't a Netscape-format cookies file: the first line must be "
            "'# Netscape HTTP Cookie File'. Use the 'Get cookies.txt LOCALLY' "
            "(Chrome) or 'cookies.txt' (Firefox) extension."
        )

    total = google = 0
    logged_in = False
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue  # comment
        parts = line.split("\t")
        if len(parts) != 7:
            raise JobError(
                f"Line {n} is malformed (expected 7 tab-separated fields, got {len(parts)}). "
                "Re-export the file; don't edit it in a program that converts tabs to spaces."
            )
        domain = parts[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        total += 1
        if domain.endswith(("youtube.com", "google.com")):
            google += 1
            if parts[5] in _LOGIN_COOKIES:
                logged_in = True

    if total == 0:
        raise JobError("The file has the right header but contains no cookies.")
    return CookieInfo("\n".join(lines).rstrip("\n") + "\n", total, google, logged_in)
