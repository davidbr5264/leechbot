#!/usr/bin/env python3
"""Upgrade yt-dlp and enable "lazy extractors".

Why: a normal `python -m yt_dlp` loads ~1,800 site extractors up front (~75 MB RSS).
With a generated lazy_extractors.py it loads them on demand (~58 MB): about 17 MB
saved on every download.

Safety: the generated file must match the installed yt-dlp exactly, so it is deleted
BEFORE every upgrade and regenerated from the sdist of the same version (checksum-verified
against PyPI). If anything fails the file is removed and yt-dlp simply runs in normal mode.

Run with the bot's venv python:  venv/bin/python deploy/update_ytdlp.py [--no-upgrade]
Exit codes: 0 = all good, 3 = upgraded but running in normal mode (no lazy extractors), 1 = pip upgrade failed.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PYPI = "https://pypi.org/pypi/yt-dlp/{}/json"
SAMPLE_URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


def log(msg: str) -> None:
    print(f"[update-ytdlp] {msg}", flush=True)


def package_dir() -> Path:
    spec = importlib.util.find_spec("yt_dlp")
    if not spec or not spec.origin:
        raise SystemExit("yt-dlp is not installed in this environment")
    return Path(spec.origin).parent


def lazy_file() -> Path:
    return package_dir() / "extractor" / "lazy_extractors.py"


def remove_lazy() -> None:
    f = lazy_file()
    f.unlink(missing_ok=True)
    for pyc in (f.parent / "__pycache__").glob("lazy_extractors*"):
        pyc.unlink(missing_ok=True)


def run_py(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=300)


def installed_version() -> str:
    # fresh interpreter: importlib.metadata in *this* process may predate the upgrade
    r = run_py("-c", "import importlib.metadata as m; print(m.version('yt-dlp'))")
    if r.returncode != 0:
        raise RuntimeError("could not read the installed yt-dlp version")
    return r.stdout.strip()


def numeric(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))  # '2026.08.19' == '2026.8.19'


def fetch_sdist(version: str) -> bytes:
    with urllib.request.urlopen(PYPI.format(version), timeout=30) as r:
        meta = json.load(r)
    sd = next((u for u in meta["urls"] if u["packagetype"] == "sdist"), None)
    if not sd:
        raise RuntimeError("no sdist published for this version")
    with urllib.request.urlopen(sd["url"], timeout=120) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sd["digests"]["sha256"]:
        raise RuntimeError("sdist checksum mismatch")
    return data


def safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract without allowing absolute paths, '..' or links to escape `dest`."""
    if hasattr(tarfile, "data_filter"):  # Python >= 3.12 (and recent 3.10/3.11 patch releases)
        tf.extractall(dest, filter="data")
        return
    base = str(dest.resolve())
    for m in tf.getmembers():
        target = str((dest / m.name).resolve())
        if not (target == base or target.startswith(base + os.sep)) or m.issym() or m.islnk():
            raise RuntimeError(f"unsafe path in archive: {m.name}")
    tf.extractall(dest)


def build_lazy(version: str) -> Path:
    data = fetch_sdist(version)
    tmp = Path(tempfile.mkdtemp(prefix="ytdlp-sdist-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            safe_extract(tf, tmp)
        root = next(p for p in tmp.iterdir() if p.is_dir())
        src_version = re.search(r"__version__\s*=\s*'([^']+)'", (root / "yt_dlp" / "version.py").read_text())
        if not src_version or numeric(src_version.group(1)) != numeric(version):
            raise RuntimeError("sdist source version does not match the installed version")
        r = subprocess.run(
            [sys.executable, str(root / "devscripts" / "make_lazy_extractors.py")],
            cwd=root, capture_output=True, text=True, timeout=300,
        )
        out = root / "yt_dlp" / "extractor" / "lazy_extractors.py"
        if r.returncode != 0 or not out.is_file() or out.stat().st_size < 100_000:
            raise RuntimeError(f"generator failed: {(r.stderr or r.stdout).strip()[-200:]}")
        dest = lazy_file()
        part = dest.with_suffix(".tmp")
        shutil.copyfile(out, part)
        part.replace(dest)  # atomic: a job starting right now sees either nothing or the full file
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sanity_check() -> None:
    """The real thing must work in lazy mode: version prints, and YouTube URLs still route."""
    r = run_py("-m", "yt_dlp", "--version")
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp --version failed: {r.stderr.strip()[-200:]}")
    code = (
        "import yt_dlp; ydl = yt_dlp.YoutubeDL({'quiet': True}); "
        "ie = next(e for e in ydl._ies.values() if e.IE_NAME == 'youtube'); "
        f"assert ie.__module__.endswith('lazy_extractors') and ie.suitable({SAMPLE_URL!r})"
    )
    r = run_py("-c", code)
    if r.returncode != 0:
        raise RuntimeError("lazy extractors loaded but did not pass the YouTube routing check")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upgrade", action="store_true", help="only (re)generate lazy extractors")
    args = ap.parse_args()

    remove_lazy()  # a stale file against a new yt-dlp is the one thing that could break downloads
    pip_failed = False
    if not args.no_upgrade:
        log("upgrading yt-dlp ...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "-U", "yt-dlp[default]"]
        )
        if r.returncode != 0:
            # Keep going: whatever is installed still deserves lazy extractors. Report failure at the end.
            log("pip upgrade FAILED; keeping the currently installed yt-dlp")
            pip_failed = True

    try:
        version = installed_version()
        log(f"yt-dlp {version}: generating lazy extractors ...")
        build_lazy(version)
        sanity_check()
    except Exception as e:  # any failure -> normal, fully working mode
        remove_lazy()
        log(f"lazy extractors NOT enabled ({e}); yt-dlp runs in normal mode (uses ~17 MB more RAM)")
        return 1 if pip_failed else 3
    log("lazy extractors enabled")
    return 1 if pip_failed else 0


if __name__ == "__main__":
    sys.exit(main())
