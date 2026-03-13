"""
Shared Utilities - common functions used across services
"""
from pathlib import Path
from fastapi import HTTPException
from config import DOWNLOAD_DIR


def human_size(b) -> str:
    """Human-readable file size."""
    if not b:
        return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def safe_path(filename: str) -> Path:
    """Resolve path and validate it's within DOWNLOAD_DIR. Raises 403 if traversal detected."""
    fp = (DOWNLOAD_DIR / filename).resolve()
    if not str(fp).startswith(str(DOWNLOAD_DIR.resolve())):
        raise HTTPException(403, "Access denied — path traversal")
    return fp


def fmt_time(secs) -> str:
    """Format seconds into human-readable HH:MM:SS or MM:SS."""
    if not secs or secs < 0 or secs > 86400:
        return "--:--"
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
