"""Memory reporting for /status. Linux only; every reader degrades to None/empty on failure."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from .utils import human_time

PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
PAGE = os.sysconf("SC_PAGE_SIZE")
_UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a huge number


@dataclass
class Snapshot:
    mem_total: int = 0
    mem_available: int = 0
    swap_total: int = 0
    swap_used: int = 0
    bot_rss: int = 0
    bot_peak: int = 0
    children: list = field(default_factory=list)  # [(label, rss_bytes)]
    cg_used: int | None = None
    cg_limit: int | None = None
    cg_anon: int | None = None
    cg_cache: int | None = None
    cg_peak: int | None = None
    load: tuple | None = None
    uptime: float | None = None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def meminfo() -> dict[str, int]:
    """/proc/meminfo as {key: bytes}."""
    out: dict[str, int] = {}
    for line in (_read(PROC / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            out[key] = int(rest.split()[0]) * 1024
    return out


def own_rss() -> tuple[int, int]:
    """(current RSS, peak RSS) of this process in bytes."""
    rss = peak = 0
    for line in (_read(PROC / "self" / "status") or "").splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
        elif line.startswith("VmHWM:"):
            peak = int(line.split()[1]) * 1024
    return rss, peak


def _label(pid: str, comm: str) -> str:
    cmdline = (_read(PROC / pid / "cmdline") or "").replace("\0", " ")
    if "yt_dlp" in cmdline:
        return "yt-dlp"
    return comm


def children(root_pid: int | None = None) -> list[tuple[str, int]]:
    """All descendants of this process as [(label, rss_bytes)], largest first."""
    root = root_pid or os.getpid()
    procs: dict[int, tuple[int, str, int, str]] = {}  # pid -> (ppid, comm, rss_bytes, pid_str)
    try:
        entries = [p.name for p in PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    for pid in entries:
        data = _read(PROC / pid / "stat")
        if not data or ")" not in data:
            continue
        comm = data[data.index("(") + 1: data.rindex(")")]
        rest = data[data.rindex(")") + 2:].split()
        try:
            if rest[0] == "Z":  # zombie: already dead, holds no memory
                continue
            procs[int(pid)] = (int(rest[1]), comm, int(rest[21]) * PAGE, pid)
        except (IndexError, ValueError):
            continue

    kids: dict[int, list[int]] = {}
    for pid, (ppid, *_rest) in procs.items():
        kids.setdefault(ppid, []).append(pid)

    out, stack = [], list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        _, comm, rss, pid_str = procs[pid]
        out.append((_label(pid_str, comm), rss))
        stack.extend(kids.get(pid, []))
    return sorted(out, key=lambda x: -x[1])


def _int(path: Path) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None  # "max" -> None


def _stat_map(path: Path) -> dict[str, int]:
    out = {}
    for line in (_read(path) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def cgroup_memory() -> dict | None:
    """Memory of the cgroup this process runs in (the systemd service): used, limit, anon, cache, peak."""
    lines = (_read(PROC / "self" / "cgroup") or "").splitlines()
    v2 = next((ln.split(":", 2)[2] for ln in lines if ln.startswith("0::")), None)
    if v2 is not None:
        base = CGROUP_ROOT / v2.lstrip("/")
        used = _int(base / "memory.current")
        if used is None:
            return None
        stat = _stat_map(base / "memory.stat")
        limit = _int(base / "memory.max")
        return {"used": used, "limit": limit if limit and limit < _UNLIMITED else None,
                "anon": stat.get("anon"), "cache": stat.get("file"), "peak": _int(base / "memory.peak")}

    v1 = next((ln.split(":", 2)[2] for ln in lines if ":memory:" in ln), None)
    if v1 is not None:
        base = CGROUP_ROOT / "memory" / v1.lstrip("/")
        used = _int(base / "memory.usage_in_bytes")
        if used is None:
            return None
        stat = _stat_map(base / "memory.stat")
        limit = _int(base / "memory.limit_in_bytes")
        return {"used": used, "limit": limit if limit and limit < _UNLIMITED else None,
                "anon": stat.get("rss"), "cache": stat.get("cache"), "peak": _int(base / "memory.max_usage_in_bytes")}
    return None


def headroom() -> int:
    """Bytes we can still allocate: system MemAvailable, further capped by the service's cgroup limit
    (page cache excluded, since it is reclaimable)."""
    mi = meminfo()
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    cg = cgroup_memory()
    if cg and cg["limit"]:
        avail = min(avail, max(cg["limit"] - (cg["used"] - (cg["cache"] or 0)), 0))
    return avail


def snapshot() -> Snapshot:
    mi = meminfo()
    s = Snapshot(
        mem_total=mi.get("MemTotal", 0),
        mem_available=mi.get("MemAvailable", mi.get("MemFree", 0)),
        swap_total=mi.get("SwapTotal", 0),
        swap_used=mi.get("SwapTotal", 0) - mi.get("SwapFree", 0),
    )
    s.bot_rss, s.bot_peak = own_rss()
    s.children = children()
    cg = cgroup_memory()
    if cg:
        s.cg_used, s.cg_limit = cg["used"], cg["limit"]
        s.cg_anon, s.cg_cache, s.cg_peak = cg["anon"], cg["cache"], cg["peak"]
    try:
        s.load = os.getloadavg()
    except OSError:
        pass
    up = _read(PROC / "uptime")
    if up:
        s.uptime = float(up.split()[0])
    return s


def _mb(n: float) -> str:
    return f"{n / 2**20:,.0f} MB"


def render(s: Snapshot) -> str:
    used = s.mem_total - s.mem_available
    lines = ["🧠 Memory"]
    lines.append(
        f"System: {_mb(used)} used of {_mb(s.mem_total)} ({_mb(s.mem_available)} available)"
    )
    lines.append(
        f"Swap: {_mb(s.swap_used)} used of {_mb(s.swap_total)}" if s.swap_total else "Swap: none"
    )
    lines.append(f"Bot process: {_mb(s.bot_rss)} (peak {_mb(s.bot_peak)})")
    big = [(name, rss) for name, rss in s.children if rss >= 2**20]
    if big:
        lines.append("Running now: " + ", ".join(f"{name} {_mb(rss)}" for name, rss in big[:4]))
    if s.cg_used is not None:
        detail = []
        if s.cg_anon is not None and s.cg_cache is not None:
            detail.append(f"anon {_mb(s.cg_anon)} · cache {_mb(s.cg_cache)}")
        if s.cg_peak:
            detail.append(f"peak {_mb(s.cg_peak)}")
        cap = f" of {_mb(s.cg_limit)} cap" if s.cg_limit else ""
        lines.append(f"Service total: {_mb(s.cg_used)}{cap}" + (f" ({', '.join(detail)})" if detail else ""))

    tight = (s.mem_total and s.mem_available < 0.10 * s.mem_total) or (
        s.cg_limit and s.cg_used is not None and s.cg_used > 0.85 * s.cg_limit
    )
    if tight:
        lines.append("⚠️ Memory is tight: new jobs may be slow, or get killed.")

    extra = []
    if s.load:
        extra.append(f"Load {s.load[0]:.2f} {s.load[1]:.2f} {s.load[2]:.2f}")
    if s.uptime:
        extra.append(f"Up {human_time(s.uptime)}")
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(lines)
