"""
Download Proxy + Media Server v5.0
Advanced file management, streaming, and multi-user support
Optimized for: 2-core Xeon, 1.9GB RAM, Debian 12
"""
import os
import shutil
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import DOWNLOAD_DIR, TRASH_DIR, VERSIONS_DIR, HLS_DIR, CLEANUP_HOURS
from database import get_db, close_db
from services.download_service import init_semaphore
from services.extract_service import init_extract_semaphore
from services.media_service import init_transcode_semaphore
from services.file_service import auto_purge_expired

STATIC_DIR = Path(__file__).parent / "static"


async def cleanup_loop():
    """Periodic cleanup: old files + expired trash."""
    while True:
        try:
            # Clean old downloads
            cut = datetime.now() - timedelta(hours=CLEANUP_HOURS)
            if DOWNLOAD_DIR.exists():
                for f in DOWNLOAD_DIR.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        if datetime.fromtimestamp(f.stat().st_mtime) < cut:
                            f.unlink()
            # Purge expired trash
            await auto_purge_expired()
        except Exception:
            pass
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    # Startup
    init_semaphore()
    init_extract_semaphore()
    init_transcode_semaphore()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    HLS_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize database
    await get_db()

    # Start background tasks
    cleanup_task = asyncio.create_task(cleanup_loop())

    yield

    # Shutdown
    cleanup_task.cancel()
    await close_db()


app = FastAPI(title="Download Proxy + Media Server", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Include routers
from routers.downloads import router as dl_router
from routers.files import router as files_router
from routers.media import router as media_router
from routers.admin import router as admin_router
from routers.share import router as share_router

app.include_router(dl_router)
app.include_router(files_router)
app.include_router(media_router)
app.include_router(admin_router)
app.include_router(share_router)

# Mount static files (CSS, JS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    from services.download_service import downloads
    disk = os.statvfs(str(DOWNLOAD_DIR)) if hasattr(os, "statvfs") else None
    free = (disk.f_bavail * disk.f_frsize / 1073741824) if disk else None
    total_d = (disk.f_blocks * disk.f_frsize / 1073741824) if disk else None
    used = total_d - free if total_d and free else None

    from config import VIDEO_EXTS, AUDIO_EXTS, SYSTEM_DIRS
    media_c = 0
    files_c = 0
    if DOWNLOAD_DIR.exists():
        for f in DOWNLOAD_DIR.rglob("*"):
            if f.is_file() and not f.name.startswith(".") and not any(sd in f.parts for sd in SYSTEM_DIRS):
                files_c += 1
                if f.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                    media_c += 1

    # Trash info
    db = await get_db()
    trash_rows = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM recycle_bin")
    trash_count = trash_rows[0]["cnt"] if trash_rows else 0

    return {
        "status": "ok", "version": "5.0.0",
        "engines": {
            "curl_chrome": shutil.which("curl_chrome") is not None,
            "aria2c": shutil.which("aria2c") is not None,
            "unrar": shutil.which("unrar") is not None,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "7z": shutil.which("7z") is not None,
        },
        "downloads_active": sum(1 for d in downloads.values() if d["status"] in ("downloading", "extracting", "compressing")),
        "files_count": files_c,
        "media_count": media_c,
        "trash_count": trash_count,
        "disk_free_gb": round(free, 2) if free else None,
        "disk_total_gb": round(total_d, 2) if total_d else None,
        "disk_used_gb": round(used, 2) if used else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
