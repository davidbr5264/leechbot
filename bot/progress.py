import time

from . import config
from .utils import human_size, human_time


def _bar(pct: float, width: int = 12) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


class Progress:
    """Edits one Telegram message with throttled progress updates."""

    def __init__(self, msg):
        self.msg = msg
        self._stage = None
        self._t0 = 0.0
        self._last = 0.0
        self.upload_label = "⬆️ Uploading"

    async def edit(self, text: str) -> None:
        try:
            await self.msg.edit_text(text)
        except Exception:
            pass  # not-modified, flood wait, deleted message: never fail a job over this

    async def _update(self, label: str, current: int, total: int) -> None:
        now = time.monotonic()
        if label != self._stage:
            self._stage, self._t0, self._last = label, now, 0.0
        if now - self._last < config.PROGRESS_INTERVAL and current != total:
            return
        self._last = now

        speed = current / max(now - self._t0, 0.001)
        lines = [label]
        if total:
            pct = min(current * 100 / total, 100)
            eta = (total - current) / speed if speed else 0
            lines += [
                f"{_bar(pct)} {pct:.1f}%",
                f"{human_size(current)} / {human_size(total)}",
                f"{human_size(speed)}/s · ETA {human_time(eta)}",
            ]
        else:
            lines += [human_size(current), f"{human_size(speed)}/s"]
        await self.edit("\n".join(lines))

    async def download(self, current: int, total: int) -> None:
        await self._update("⬇️ Downloading", current, total)

    async def upload(self, current: int, total: int) -> None:
        await self._update(self.upload_label, current, total)
