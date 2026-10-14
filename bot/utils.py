import asyncio
import ipaddress
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse


class JobError(Exception):
    """A user-facing error: the message is shown in the chat."""


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def safe_filename(name: str, default: str = "file") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if len(name) > 200:
        ext = Path(name).suffix[:10]
        name = name[: 200 - len(ext)] + ext
    return name or default


def free_space(path: Path) -> int:
    """Free bytes on the filesystem holding `path` (or its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def resolve_public(url: str) -> tuple[str, int, list[str]]:
    """Blocking. Validate an http(s) URL and resolve it; every address must be public.

    Returns (host, port, ips) so callers can connect to the *validated* IP itself,
    which closes the DNS-rebinding gap between "check" and "connect"."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise JobError("Only http(s) URLs are supported.")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise JobError("That URL has an invalid port.") from None
    try:
        infos = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise JobError("Could not resolve that host.") from None

    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:  # ::ffff:127.0.0.1 must be judged as 127.0.0.1
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise JobError("Blocked: URL points to a private or local address.")
        if str(ip) not in ips:
            ips.append(str(ip))
    return p.hostname, port, ips


async def assert_safe_url(url: str) -> None:
    await asyncio.get_running_loop().run_in_executor(None, resolve_public, url)
