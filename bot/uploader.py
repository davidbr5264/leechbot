import asyncio
import math
import os
from pathlib import Path

from . import config, media

# Only containers Telegram plays inline reliably; mkv/webm/avi go as documents.
VIDEO_EXT = {".mp4", ".m4v", ".mov"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}


async def upload(client, chat_id: int, path: Path, on_progress, as_document: bool = False):
    ext = path.suffix.lower()
    caption = path.name[:1024]
    common = dict(chat_id=chat_id, caption=caption, progress=on_progress)

    if not as_document and ext in VIDEO_EXT:
        meta = await media.probe(path)
        thumb = await media.thumbnail(path, meta["duration"])
        await client.send_video(
            video=str(path),
            duration=meta["duration"],
            width=meta["width"],
            height=meta["height"],
            thumb=thumb,
            supports_streaming=True,
            **common,
        )
    elif not as_document and ext in AUDIO_EXT:
        meta = await media.probe(path)
        await client.send_audio(
            audio=str(path), duration=meta["duration"],
            title=meta["title"] or None, performer=meta["artist"] or None, **common,
        )
    else:
        await client.send_document(document=str(path), force_document=True, **common)


def _write_part(src: Path, dest: Path, offset: int, length: int, bufsize: int = 1 << 20) -> None:
    """Copy [offset, offset+length) of src into dest using a 1 MiB buffer (constant RAM).
    Every 16 MiB the copied range is flushed and dropped from the page cache, so a multi-GB
    split can't fill RAM with cache and trigger memory-pressure throttling."""
    with open(src, "rb") as r, open(dest, "wb") as w:
        r.seek(offset)
        remaining, since_flush, pos = length, 0, offset
        while remaining:
            buf = r.read(min(bufsize, remaining))
            if not buf:
                break
            w.write(buf)
            remaining -= len(buf)
            since_flush += len(buf)
            if since_flush >= 16 << 20:
                w.flush()
                os.fdatasync(w.fileno())
                _drop_cache(w.fileno())
                _drop_cache(r.fileno(), pos, since_flush)
                pos += since_flush
                since_flush = 0
        w.flush()
        os.fdatasync(w.fileno())
        _drop_cache(w.fileno())
        _drop_cache(r.fileno(), pos, since_flush)


def _drop_cache(fd: int, offset: int = 0, length: int = 0) -> None:
    try:
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass  # not supported on this platform/filesystem: harmless


async def upload_split(client, chat_id: int, path: Path, prog) -> None:
    """Upload an oversized file as name.ext.001, .002, ...
    Only one part exists on disk at a time (original + one part)."""
    size = path.stat().st_size
    part_size = config.MAX_FILE_SIZE
    n = math.ceil(size / part_size)
    join_hint = f'cat "{path.name}".* > "{path.name}"'

    for i in range(n):
        part = path.with_name(f"{path.name}.{i + 1:03d}")
        await asyncio.to_thread(
            _write_part, path, part, i * part_size, min(part_size, size - i * part_size)
        )
        prog.upload_label = f"⬆️ Uploading part {i + 1}/{n}"
        try:
            await client.send_document(
                chat_id=chat_id,
                document=str(part),
                force_document=True,
                caption=f"{part.name}\nJoin: {join_hint}"[:1024],
                progress=prog.upload,
            )
        finally:
            part.unlink(missing_ok=True)
