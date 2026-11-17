"""Service (admin) helpers: password check and receipt folder maintenance."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from .server import SUBFOLDERS

ITERATIONS = 300_000
# PBKDF2 hash of the default service password; the plaintext is intentionally not stored in the source.
DEFAULT_PASSWORD_HASH = (
    "bdd43e5c369cc75cb0c9121428dea432$e6035ab5ef5bdf0f429bfc3b54dcf52866a7a7aeff13ad0ecfc2b908901fde97"
)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hash_password(password, salt).split("$", 1)[1]
    return hmac.compare_digest(candidate, digest_hex)


def _job_files(root: Path, older_than_days: int | None) -> list[Path]:
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    files = []
    for name in SUBFOLDERS:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and (cutoff is None or path.stat().st_mtime < cutoff):
                files.append(path)
    return files


def folder_usage(root: Path, older_than_days: int | None = None) -> tuple[int, int]:
    """(file count, total bytes) of job files under root."""
    files = _job_files(root, older_than_days)
    return len(files), sum(f.stat().st_size for f in files)


def delete_job_files(root: Path, older_than_days: int | None = None) -> int:
    """Delete job files (keeping the folder structure); returns how many were deleted."""
    deleted = 0
    for path in _job_files(root, older_than_days):
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def format_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"
