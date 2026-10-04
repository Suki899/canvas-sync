"""Atomic writes and a process lock for each destination."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from .api import SyncError


def safe_name(value):
    name = unicodedata.normalize("NFC", str(value))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "-", name).strip(" .") or "unnamed"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)]}:
        name = "_" + name
    # Leave room for disambiguation suffixes on filesystems with 255-byte names.
    return name.encode("utf-8")[:180].decode("utf-8", errors="ignore")


def checked_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(p in (".", "..") for p in relative.parts):
        raise SyncError("Unsafe path in mirror metadata.")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise SyncError(f"Refusing to follow symlink: {path}")
    return path


def read_json(path, default):
    try:
        if path.is_symlink():
            raise SyncError(f"Refusing to read symlink: {path}")
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise SyncError(f"Invalid metadata at {path}; restore it from a backup before syncing.") from exc


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".canvas-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(path):
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise SyncError(f"Expected a regular file: {path}")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def mirror_lock(root):
    try:
        import fcntl
    except ImportError as exc:
        raise SyncError("Canvas Sync currently supports Linux and macOS.") from exc
    metadata = checked_path(root, ".canvas-sync")
    metadata.mkdir(mode=0o700, exist_ok=True)
    lock_path = checked_path(root, ".canvas-sync/lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another Canvas Sync process is already using this folder.") from exc
        try:
            yield metadata
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
