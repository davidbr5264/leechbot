import asyncio
import json
from pathlib import Path

from . import procs


async def _run(*args: str) -> tuple[int, bytes]:
    """Run ffmpeg/ffprobe with OOM priority, one heavy process at a time, and a free-RAM check.
    (A missing binary shows up as exit code 127; callers treat any non-zero code as 'unavailable'.)"""
    async with procs.heavy_slot(need_mb=80):
        proc = await procs.spawn(list(args), stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return proc.returncode, out


async def probe(path: Path) -> dict:
    """Duration/width/height and title/artist tags (zeros/empty if ffprobe is missing or fails)."""
    meta = {"duration": 0, "width": 0, "height": 0, "title": "", "artist": ""}
    try:
        rc, out = await _run(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration:format_tags=title,artist",
            "-of", "json", str(path),
        )
        if rc == 0:
            data = json.loads(out or b"{}")
            stream = (data.get("streams") or [{}])[0]
            fmt = data.get("format", {})
            tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}  # TITLE vs title
            meta["width"] = int(stream.get("width") or 0)
            meta["height"] = int(stream.get("height") or 0)
            meta["duration"] = int(float(fmt.get("duration") or 0))
            meta["title"] = str(tags.get("title", ""))[:200]
            meta["artist"] = str(tags.get("artist", ""))[:200]
    except (ValueError, json.JSONDecodeError):
        pass
    return meta


async def thumbnail(path: Path, duration: int) -> str | None:
    """Grab one small JPEG frame. Returns its path, or None on failure."""
    thumb = path.with_suffix(".thumb.jpg")
    seek = "3" if duration > 6 else "0"
    rc, _ = await _run(
        "ffmpeg", "-y", "-threads", "1", "-ss", seek, "-i", str(path),
        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(thumb),
    )
    return str(thumb) if rc == 0 and thumb.exists() else None
