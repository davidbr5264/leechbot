"""Hygiene for heavy child processes (yt-dlp, ffmpeg) so low-RAM servers don't crash.

1. OOM priority: children are marked oom_score_adj=+500, so if memory runs out the kernel (and
   earlyoom) kill THEM first and the bot survives.  They also run at nice 10 to keep the bot responsive.
2. Concurrency: only 1 heavy process at a time on small servers (2 on >= 1.5 GB).
3. Admission control: before starting one, wait until enough RAM is actually free.
"""
import asyncio
from contextlib import asynccontextmanager

from . import config, sysinfo
from .utils import JobError

_WRAP = (
    "echo 500 > /proc/self/oom_score_adj 2>/dev/null; "
    'if command -v nice >/dev/null 2>&1; then exec nice -n 10 "$@"; fi; exec "$@"'
)
OOM_MESSAGE = (
    "💥 The server ran out of memory and the process was killed. "
    "Try a lower resolution or audio only (see /status)."
)

_sem: asyncio.Semaphore | None = None


def init(limit: int | None = None) -> int:
    """Create the heavy-process semaphore; returns its size."""
    global _sem
    n = limit or (1 if sysinfo.meminfo().get("MemTotal", 0) < int(1.5 * 2**30) else 2)
    _sem = asyncio.Semaphore(n)
    return n


def _get() -> asyncio.Semaphore:
    if _sem is None:
        init()
    return _sem  # type: ignore[return-value]


def wrap(cmd: list[str]) -> list[str]:
    """exec the command through a tiny shell that lowers its priority (same PID afterwards)."""
    return ["/bin/sh", "-c", _WRAP, "sh", *cmd]


async def spawn(cmd: list[str], *, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT):
    # limit: a pathological very long output line can't blow up the StreamReader
    return await asyncio.create_subprocess_exec(*wrap(cmd), stdout=stdout, stderr=stderr, limit=1 << 20)


def killed_by_signal_9(returncode: int | None) -> bool:
    return returncode in (-9, 137)


async def wait_for_memory(need_mb: int | None = None, timeout: float = 120, on_wait=None, poll: float = 3.0) -> None:
    need = (need_mb or config.MIN_FREE_RAM_MB) * 2**20
    waited = 0.0
    while (free := sysinfo.headroom()) < need:
        if waited >= timeout:
            raise JobError(
                f"Not enough free memory ({free // 2**20} MB free, need {need // 2**20} MB). "
                "Try again in a moment (see /status)."
            )
        if waited == 0 and on_wait:
            await on_wait(f"⏳ Low memory ({free // 2**20} MB free). Waiting for it to free up…")
        await asyncio.sleep(poll)
        waited += poll


@asynccontextmanager
async def heavy_slot(on_wait=None, need_mb: int | None = None):
    """Hold this while a yt-dlp/ffmpeg process runs."""
    sem = _get()
    if sem.locked() and on_wait:
        await on_wait("⏳ Waiting for the current heavy task to finish (low-memory mode)…")
    async with sem:
        await wait_for_memory(need_mb, on_wait=on_wait)
        yield
