"""Direct-URL downloader on the standard library only (no aiohttp: it costs ~13 MB of RAM).

The blocking transfer runs in one worker thread. The async side polls its counters once a
second for progress and can abort it. Features: per-hop SSRF checks, connecting to the
*validated* IP, Range-based resume, size/disk limits."""
import asyncio
import http.client
import mimetypes
import os
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from .. import config
from ..utils import JobError, free_space, human_size, resolve_public, safe_filename

UA = "Mozilla/5.0 (X11; Linux x86_64) leechbot/0.1"
CHUNK = 128 * 1024
MAX_RETRIES = 5
BACKOFF = 3  # seconds * attempt number, capped at 15
FLUSH_EVERY = 16 << 20  # bytes


def _drop_cache(fd: int) -> None:
    """After data is safely on disk, tell the kernel not to keep it cached. Without this, downloading a
    multi-GB file fills RAM with page cache and the service's memory cap starts throttling everything."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass
REDIRECTS = (301, 302, 303, 307, 308)
_SAFE = "/%:@!$&'()*+,;=-._~"


class _Cancelled(Exception):
    pass


class _State:
    """Shared between the worker thread and the async side."""

    def __init__(self):
        self.done = 0
        self.total = 0
        self.cancel = threading.Event()
        self.sock = None

    def abort(self):
        self.cancel.set()
        sock = self.sock
        if sock is not None:  # wake a thread blocked in recv()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _target(parts) -> str:
    path = quote(parts.path or "/", safe=_SAFE)
    return path + ("?" + quote(parts.query, safe=_SAFE + "?") if parts.query else "")


def _cd_filename(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    return msg.get_filename()


def _connect(url: str, st: _State):
    parts = urlsplit(url)
    host, port, ips = resolve_public(url)  # SSRF check on every hop
    sock, last = None, None
    for ip in ips:  # connect to the validated address, not whatever DNS says next time
        if st.cancel.is_set():
            raise _Cancelled
        try:
            sock = socket.create_connection((ip, port), timeout=30)
            break
        except OSError as e:
            last = e
    if sock is None:
        raise last or OSError("could not connect")
    sock.settimeout(60)
    if parts.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=60)
    conn.sock = sock  # pre-connected: http.client won't resolve/connect again
    st.sock = sock
    return conn, parts


def _open(url: str, headers: dict, st: _State):
    """GET with manual redirects so every hop is validated."""
    for _ in range(6):
        conn, parts = _connect(url, st)
        try:
            conn.request(
                "GET", _target(parts),
                headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity", **headers},
            )
            resp = conn.getresponse()
        except BaseException:
            conn.close()
            raise
        if resp.status in REDIRECTS:
            loc = resp.getheader("Location")
            resp.close()
            conn.close()
            if not loc:
                raise JobError("Redirect without a Location header.")
            url = urljoin(url, loc)
            continue
        return conn, resp, url
    raise JobError("Too many redirects.")


def _worker(url: str, dest_dir: Path, name: str | None, st: _State) -> Path:
    path: Path | None = None
    total = done = retries = 0

    while True:
        if st.cancel.is_set():
            raise _Cancelled
        headers = {"Range": f"bytes={done}-"} if done else {}
        conn = None
        try:
            conn, resp, final_url = _open(url, headers, st)

            if resp.status == 206 and done:
                m = re.match(r"bytes (\d+)-", resp.getheader("Content-Range") or "")
                if not m or int(m.group(1)) != done:  # server resumed at the wrong offset
                    done = 0
                    continue
                mode = "ab"
            elif resp.status == 200:
                done, mode = 0, "wb"  # fresh start, or the server ignored Range
            else:
                raise JobError(f"Server returned HTTP {resp.status}.")
            st.done = done

            if path is None:
                cl = resp.getheader("Content-Length")
                total = int(cl) if cl and cl.isdigit() else 0
                if total > config.MAX_DOWNLOAD:
                    raise JobError(
                        f"File is {human_size(total)}; limit is {human_size(config.MAX_DOWNLOAD)}."
                    )
                need = total + config.MIN_FREE_SPACE  # splitting needs the original + one part
                if total > config.MAX_FILE_SIZE:
                    need += config.MAX_FILE_SIZE
                if total and free_space(dest_dir) < need:
                    raise JobError("Not enough free disk space for this file.")
                filename = (
                    name
                    or _cd_filename(resp.getheader("Content-Disposition"))
                    or unquote(Path(urlsplit(final_url).path).name)
                    or "file"
                )
                if not Path(filename).suffix:
                    ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip()
                    filename += mimetypes.guess_extension(ctype) or ""
                path = dest_dir / safe_filename(filename)
                st.total = total

            try:
                f = open(path, mode)
            except OSError as e:
                raise JobError(f"Can't write the download: {e.strerror or e}") from e
            with f:
                unflushed = 0
                while True:
                    chunk = resp.read1(CHUNK)
                    if not chunk:
                        break
                    try:
                        f.write(chunk)
                        unflushed += len(chunk)
                        if unflushed >= FLUSH_EVERY:  # keep the page cache small (see _drop_cache)
                            f.flush()
                            os.fdatasync(f.fileno())
                            _drop_cache(f.fileno())
                            unflushed = 0
                    except OSError as e:
                        raise JobError(f"Disk write failed: {e.strerror or e}") from e
                    done += len(chunk)
                    st.done = done
                    if done > config.MAX_DOWNLOAD:
                        raise JobError("Download exceeded the size limit; aborted.")
                    if st.cancel.is_set():
                        raise _Cancelled
            if total and done < total:
                raise http.client.IncompleteRead(b"", total - done)
            return path

        except ssl.SSLCertVerificationError as e:  # a bad certificate won't fix itself: no retry
            raise JobError(f"TLS certificate check failed: {e.verify_message or e}") from e
        except (http.client.HTTPException, OSError) as e:
            if st.cancel.is_set():
                raise _Cancelled from e
            retries += 1
            if retries > MAX_RETRIES:
                raise JobError(f"Connection failed repeatedly ({type(e).__name__}).") from e
            st.cancel.wait(min(BACKOFF * retries, 15))  # back off, then resume with a Range request
        finally:
            if conn is not None:
                conn.close()
            st.sock = None


async def download(url: str, dest_dir: Path, name: str | None, on_progress) -> Path:
    st = _State()
    fut = asyncio.get_running_loop().run_in_executor(None, _worker, url, dest_dir, name, st)
    try:
        while True:
            finished, _ = await asyncio.wait({fut}, timeout=1.0)
            if finished:
                path = fut.result()
                await on_progress(st.done, st.total)
                return path
            await on_progress(st.done, st.total)
    except asyncio.CancelledError:
        st.abort()
        await asyncio.wait({fut}, timeout=5)  # let the thread stop before the caller deletes files
        if fut.done() and not fut.cancelled():
            fut.exception()  # mark retrieved
        raise


@dataclass
class Head:
    status: int
    content_type: str
    size: int  # 0 = unknown
    filename: str | None


def _probe_blocking(url: str, st: _State) -> Head:
    conn, resp, final_url = _open(url, {"Range": "bytes=0-0"}, st)  # GET 0-0: works where HEAD is blocked
    try:
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        size = 0
        m = re.match(r"bytes \d+-\d+/(\d+)", resp.getheader("Content-Range") or "")
        if m:
            size = int(m.group(1))
        elif resp.status == 200:
            cl = resp.getheader("Content-Length")
            size = int(cl) if cl and cl.isdigit() else 0
        name = _cd_filename(resp.getheader("Content-Disposition")) or unquote(Path(urlsplit(final_url).path).name) or None
        return Head(resp.status, ctype, size, name)
    finally:
        conn.close()


async def probe(url: str) -> Head:
    """What does this URL serve? (content type, size, filename). Raises JobError for unsafe/unreachable URLs."""
    st = _State()
    fut = asyncio.get_running_loop().run_in_executor(None, _probe_blocking, url, st)
    try:
        return await asyncio.wait_for(fut, 45)
    except asyncio.TimeoutError:
        st.abort()
        raise JobError("That server didn't answer in time.") from None
    except (http.client.HTTPException, OSError) as e:
        raise JobError(f"Couldn't reach that link ({type(e).__name__}).") from e
    except asyncio.CancelledError:
        st.abort()
        raise
