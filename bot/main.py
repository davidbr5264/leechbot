import asyncio
import itertools
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client, enums, filters, idle
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from . import config, cookies, detect, procs, sysinfo, uploader
from .downloaders import direct, ytdlp
from .progress import Progress
from .utils import JobError, free_space, human_size, human_time

log = logging.getLogger("leechbot")

HELP = (
    "Just send me a link, no command needed:\n"
    "- a direct file link: I download it and upload it here\n"
    "- a YouTube or other video-site link: I download the best quality available "
    "(best video + best audio, no conversion)\n\n"
    "Rename a direct download:  link | new_name.ext\n\n"
    "Commands:\n"
    "/leech <url>  - force a direct download\n"
    "/ytdl <url>  - force yt-dlp\n"
    "/cookies  - YouTube cookies (fixes the 'not a bot' error)\n"
    "/status  - queue, memory, disk\n"
    "/cancel  - cancel the running job  (/cancel all also clears the queue)\n\n"
    "Add 'doc' after a link in /leech or /ytdl to upload it as a file instead of a video."
)


@dataclass
class Job:
    id: int
    kind: str  # "leech" | "ytdl"
    url: str
    chat_id: int
    msg: Message  # the status message we keep editing
    name: str | None = None
    as_document: bool = False


queue: asyncio.Queue = asyncio.Queue()
state: dict = {"job": None, "task": None, "worker": None}
job_ids = itertools.count(1)
shutting_down = False
_tasks: set[asyncio.Task] = set()


def bg(coro) -> None:
    """Run a coroutine in the background so message handlers return immediately."""
    t = asyncio.create_task(coro)
    _tasks.add(t)

    def done(task: asyncio.Task) -> None:
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    t.add_done_callback(done)


async def _edit(msg, text: str, **kw) -> None:
    try:
        await msg.edit_text(text, **kw)
    except Exception:
        pass  # not-modified, flood wait, deleted message: never fail over a status edit


def parse_args(message: Message):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        return None
    left, _, right = parts[1].partition("|")
    tokens = left.split()
    if not tokens:
        return None
    return tokens[0], {t.lower() for t in tokens[1:]}, (right.strip() or None)


def queue_text() -> str:
    ahead = queue.qsize() + (1 if state["job"] else 0)
    return f"🕐 Queued (position {ahead + 1})" if ahead else "🚀 Starting…"


def queue_full() -> bool:
    return queue.qsize() >= config.MAX_QUEUE


# ---------- automatic handling of links ----------------------------------------------

async def handle_url(message: Message, url: str, name: str | None = None, force: str | None = None,
                     as_document: bool = False) -> None:
    if queue_full():
        await message.reply_text(f"The queue is full ({config.MAX_QUEUE} waiting). Try again in a bit.")
        return
    status = await message.reply_text("🔎 Checking link…")
    try:
        verdict = detect.Verdict(force) if force else await detect.classify(url)
        label = ""
        if verdict.kind == "direct":
            head = verdict.head
            if head and (head.filename or head.size):
                label = f"📎 {name or head.filename or 'file'}" + (f" ({human_size(head.size)})" if head.size else "") + "\n"
            kind = "leech"
        else:
            label, kind = "🎬 Best quality available\n", "ytdl"
        await _edit(status, label + queue_text())
        await queue.put(Job(next(job_ids), kind, url, message.chat.id, status, name=name, as_document=as_document))
    except JobError as e:
        await _edit(status, f"❌ {e}")
    except Exception as e:  # never let one bad link kill the handler task silently
        log.exception("handle_url failed")
        await _edit(status, f"❌ {type(e).__name__}: {str(e)[:200]}")


async def handle_urls(message: Message, urls: list) -> None:
    for url, name in urls:  # one at a time: classifying may spawn a subprocess
        await handle_url(message, url, name)


async def on_text(client, message):
    urls = detect.extract_urls(message.text or "", message.entities)
    if not urls:
        await message.reply_text("Send me a link (a direct file or a video site), or /help.")
        return
    bg(handle_urls(message, urls))


async def cmd_start(client, message):
    await message.reply_text(HELP)


async def _command(message: Message, force: str) -> None:
    parsed = parse_args(message)
    if not parsed:
        await message.reply_text(HELP)
        return
    url, flags, name = parsed
    bg(handle_url(message, url, name if force == "direct" else None, force=force, as_document="doc" in flags))


async def cmd_leech(client, message):
    await _command(message, "direct")


async def cmd_ytdl(client, message):
    await _command(message, "ytdl")


COOKIE_HELP = (
    "To fix YouTube's bot check, send me a cookies.txt FILE with the caption /cookies "
    "(or reply to the file with /cookies).\n\n"
    "How to export it (YouTube rotates cookies on open tabs, so do it exactly like this):\n"
    "1. Open a private/incognito window and log in to YouTube (a throwaway account is safest).\n"
    "2. In that same tab, open https://www.youtube.com/robots.txt\n"
    "3. Export youtube.com cookies with the 'Get cookies.txt LOCALLY' (Chrome) or "
    "'cookies.txt' (Firefox) extension.\n"
    "4. Close the private window, then send me the file.\n\n"
    "/cookies clear  - delete the stored cookies"
)


def cookie_status() -> str:
    f = config.COOKIES_FILE
    if not f.is_file():
        return "🍪 No cookies installed."
    return f"🍪 Cookies installed (last updated {human_time(time.time() - f.stat().st_mtime)} ago)."


async def cmd_cookies(client, message):
    parts = (message.text or message.caption or "").split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if arg == "clear":
        config.COOKIES_FILE.unlink(missing_ok=True)
        await message.reply_text("🗑 Cookies removed.")
        return

    src = message if message.document else (
        message.reply_to_message if message.reply_to_message and message.reply_to_message.document else None
    )
    if src is None:
        await message.reply_text(f"{cookie_status()}\n\n{COOKIE_HELP}")
        return

    job = state["job"]
    if job and job.kind == "ytdl":  # yt-dlp rewrites the file on exit and would clobber ours
        await message.reply_text("A yt-dlp job is running. Send the cookies again when it finishes.")
        return
    if src.document.file_size and src.document.file_size > cookies.MAX_BYTES:
        await message.reply_text("❌ That file is too big to be a cookies file (limit 1 MB).")
        return

    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.DOWNLOAD_DIR / f"cookies_upload_{secrets.token_hex(4)}"
    try:
        path = await client.download_media(src, file_name=str(tmp))
        if not path:
            raise JobError("Couldn't download that file from Telegram.")
        info = cookies.parse(Path(path).read_bytes())
    except JobError as e:
        await message.reply_text(f"❌ {e}")
        return
    finally:
        tmp.unlink(missing_ok=True)

    dest = config.COOKIES_FILE
    part = dest.with_name(dest.name + ".tmp")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable
    with os.fdopen(fd, "w") as f:
        f.write(info.text)
    os.replace(part, dest)

    summary = f"✅ Cookies installed: {info.total} cookies ({info.google} for YouTube/Google)."
    summary += (
        "\nLogged-in session detected."
        if info.logged_in else
        "\n⚠️ No login cookies found. This looks like a logged-out session, so YouTube may still ask you to sign in."
    )
    reply = await message.reply_text(summary)
    try:  # don't leave your session cookies sitting in the chat history
        await message.delete()
    except Exception:
        if reply:
            await reply.edit_text(summary + "\n⚠️ I couldn't delete your upload. Please delete it yourself.")


async def cmd_cancel(client, message):
    words = (message.text or "").lower().split()[1:]
    dropped = 0
    if "all" in words:
        while not queue.empty():
            job = queue.get_nowait()
            queue.task_done()
            dropped += 1
            await _edit(job.msg, "❌ Cancelled")
    task = state["task"]
    running = bool(task and not task.done())
    if running:
        task.cancel()
    if not running and not dropped:
        await message.reply_text("Nothing to cancel.")
    elif dropped:
        await message.reply_text(f"Cancelled{' the running job and' if running else ''} {dropped} queued job(s).")


async def cmd_status(client, message):
    job = state["job"]
    active = f"{job.kind}: {job.url[:60]}" if job else "idle"
    await message.reply_text(
        f"📊 Active: {active}\n"
        f"Queued: {queue.qsize()}\n"
        f"💾 Disk free: {human_size(free_space(config.DOWNLOAD_DIR))}\n\n"
        f"{sysinfo.render(sysinfo.snapshot())}"
    )


async def process(client: Client, job: Job):
    workdir = config.DOWNLOAD_DIR / f"job_{job.id}"
    workdir.mkdir(parents=True, exist_ok=True)
    prog = Progress(job.msg)
    try:
        note = ""
        if job.kind == "ytdl":
            got = await ytdlp.download(job.url, workdir, prog.download, on_wait=prog.edit)
            path = got.path
            if got.was_playlist:
                note = "\n(That link is a playlist: only its first video was downloaded.)"
        else:
            path = await direct.download(job.url, workdir, job.name, prog.download)

        if path.stat().st_size > config.MAX_FILE_SIZE:
            await uploader.upload_split(client, job.chat_id, path, prog)
        else:
            await uploader.upload(client, job.chat_id, path, prog.upload, job.as_document)
        await prog.edit(f"✅ Done: {path.name}{note}")
    except asyncio.CancelledError:
        await prog.edit("♻️ The bot is restarting. Send the link again." if shutting_down else "❌ Cancelled")
    except JobError as e:
        await prog.edit(f"❌ {e}")
    except MemoryError:
        await prog.edit(procs.OOM_MESSAGE)
    except Exception as e:
        log.exception("job %s failed", job.id)
        await prog.edit(f"❌ {type(e).__name__}: {str(e)[:300]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # always free the disk


def write_marker(job: Job) -> None:
    try:
        config.STATE_FILE.write_text(json.dumps({"kind": job.kind, "url": job.url, "t": time.time()}))
    except OSError:
        pass


def clear_marker() -> None:
    try:
        config.STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def notify_interrupted(app: Client) -> None:
    """If the last run died mid-job (crash, OOM kill, reboot), say so instead of failing silently."""
    try:
        data = json.loads(config.STATE_FILE.read_text())
    except (OSError, ValueError):
        return
    clear_marker()
    text = (
        "♻️ I was restarted while working on:\n"
        f"{str(data.get('url', '?'))[:200]}\n"
        "If it didn't finish, send the link again. If this keeps happening, check /status for memory."
    )
    try:
        await app.send_message(config.OWNER_ID, text)
    except Exception:
        log.warning("couldn't notify the owner about the interrupted job")


async def worker(client: Client):
    while True:
        job = await queue.get()
        state["job"] = job
        write_marker(job)
        state["task"] = asyncio.create_task(process(client, job))
        try:
            await state["task"]
        except Exception:
            log.exception("worker error")
        finally:
            state["job"] = state["task"] = None
            clear_marker()
            queue.task_done()


def start_worker(client: Client) -> None:
    """Supervised: if the worker ever dies unexpectedly it is restarted, so the queue can't get stuck."""
    t = asyncio.create_task(worker(client))
    state["worker"] = t

    def on_done(task: asyncio.Task) -> None:
        if shutting_down or task.cancelled():
            return
        log.error("worker died, restarting", exc_info=task.exception())
        start_worker(client)

    t.add_done_callback(on_done)


def sweep_orphans():
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for p in config.DOWNLOAD_DIR.iterdir():
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


async def main():
    global shutting_down
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sweep_orphans()
    slots = procs.init()
    log.info("heavy-process slots: %s, min free RAM: %s MB", slots, config.MIN_FREE_RAM_MB)

    # Created inside the running loop so it works on any Python 3.11+.
    app = Client(
        "leechbot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=2,
        sleep_threshold=120,  # ride out Telegram flood waits up to 2 min instead of failing a finished download
        parse_mode=enums.ParseMode.DISABLED,  # titles/filenames contain _ * < > that would be mangled
        max_concurrent_transmissions=1,  # one upload/download stream at a time
        max_message_cache_size=50,       # default 1000 message objects kept in RAM
        max_topic_cache_size=20,         # default 1000
    )
    owner = filters.user(config.OWNER_ID) & filters.private
    for handler, cmds in (
        (cmd_start, ["start", "help"]),
        (cmd_leech, ["leech"]),
        (cmd_ytdl, ["ytdl"]),
        (cmd_cookies, ["cookies"]),
        (cmd_cancel, ["cancel"]),
        (cmd_status, ["status"]),
    ):
        app.add_handler(MessageHandler(handler, owner & filters.command(cmds)))
    app.add_handler(MessageHandler(on_text, owner & filters.text & ~filters.command(["start", "help", "leech", "ytdl", "cookies", "cancel", "status"])))

    await app.start()
    await notify_interrupted(app)
    start_worker(app)
    log.info("Bot started")
    await idle()
    shutting_down = True
    state["worker"].cancel()
    await asyncio.sleep(0.5)  # let the running job report "restarting" and clean up
    await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
