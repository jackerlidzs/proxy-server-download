"""
Download Proxy Server - FastAPI Service
Downloads files using aria2c and serves them via Nginx
"""

import os
import uuid
import json
import time
import shlex
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

# Configuration
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
API_KEY = os.getenv("API_KEY", "changeme")
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "16"))
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "48"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
SERVER_URL = os.getenv("SERVER_URL", "http://localhost")

# State
downloads: dict = {}
download_semaphore: asyncio.Semaphore = None


# --- Models ---
class DownloadRequest(BaseModel):
    url: str
    headers: Optional[dict] = None
    filename: Optional[str] = None
    connections: Optional[int] = None  # override MAX_CONNECTIONS per request
    curl_command: Optional[str] = None  # paste raw curl command


class DownloadStatus(BaseModel):
    task_id: str
    status: str  # queued, downloading, completed, failed
    filename: Optional[str] = None
    file_size: Optional[int] = None
    download_url: Optional[str] = None
    progress: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


# --- Helpers ---
def parse_curl_command(curl_cmd: str) -> tuple[str, dict]:
    """Parse a curl command string and extract URL + headers."""
    parts = shlex.split(curl_cmd.replace("\\\n", " "))
    url = ""
    headers = {}

    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "curl":
            i += 1
            continue
        elif part in ("-H", "--header"):
            i += 1
            if i < len(parts):
                header_str = parts[i]
                if ": " in header_str:
                    key, val = header_str.split(": ", 1)
                    # Skip browser-internal headers
                    if not key.lower().startswith(("sec-ch-ua", "sec-fetch", "priority")):
                        headers[key] = val
                elif ":" in header_str:
                    key, val = header_str.split(":", 1)
                    if not key.lower().startswith(("sec-ch-ua", "sec-fetch", "priority")):
                        headers[key] = val.strip()
        elif part in ("-X", "--request"):
            i += 1  # skip method
        elif not part.startswith("-") and not url:
            url = part.strip("'\"")
        i += 1

    return url, headers


def get_filename_from_url(url: str) -> str:
    """Extract filename from URL."""
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    filename = unquote(path.split("/")[-1])
    if not filename:
        filename = f"download_{uuid.uuid4().hex[:8]}"
    return filename


def generate_task_id() -> str:
    return uuid.uuid4().hex[:12]


async def verify_api_key(authorization: Optional[str] = Header(None)):
    """Simple API key auth."""
    if API_KEY == "public":
        return True
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return True


async def cleanup_old_files():
    """Remove files older than CLEANUP_HOURS."""
    while True:
        try:
            cutoff = datetime.now() - timedelta(hours=CLEANUP_HOURS)
            if DOWNLOAD_DIR.exists():
                for f in DOWNLOAD_DIR.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        if mtime < cutoff:
                            f.unlink()
                            # Remove from downloads dict
                            to_remove = []
                            for tid, info in downloads.items():
                                if info.get("filename") == f.name:
                                    to_remove.append(tid)
                            for tid in to_remove:
                                del downloads[tid]
        except Exception as e:
            print(f"Cleanup error: {e}")
        await asyncio.sleep(3600)  # Run every hour


async def run_download(task_id: str, url: str, headers: dict, filename: str, connections: int):
    """Download file using aria2c."""
    async with download_semaphore:
        filepath = DOWNLOAD_DIR / filename
        downloads[task_id]["status"] = "downloading"

        # Build aria2c command
        cmd = [
            "aria2c",
            "--max-connection-per-server", str(connections),
            "--split", str(connections),
            "--min-split-size", "1M",
            "--max-tries", "5",
            "--retry-wait", "3",
            "--timeout", "60",
            "--connect-timeout", "30",
            "--continue", "true",
            "--auto-file-renaming", "false",
            "--allow-overwrite", "true",
            "--dir", str(DOWNLOAD_DIR),
            "--out", filename,
            "--console-log-level", "notice",
            "--summary-interval", "5",
            "--file-allocation", "none",
        ]

        # Add custom headers
        for key, val in headers.items():
            cmd.extend(["--header", f"{key}: {val}"])

        cmd.append(url)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            last_line = ""
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="ignore").strip()
                if decoded:
                    last_line = decoded
                    # Parse progress from aria2c output
                    if "%" in decoded or "DL:" in decoded:
                        downloads[task_id]["progress"] = decoded

            await process.wait()

            if process.returncode == 0 and filepath.exists():
                file_size = filepath.stat().st_size
                downloads[task_id].update({
                    "status": "completed",
                    "file_size": file_size,
                    "download_url": f"{SERVER_URL}/files/{filename}",
                    "completed_at": datetime.now().isoformat(),
                    "progress": "100%",
                })
            else:
                downloads[task_id].update({
                    "status": "failed",
                    "error": f"aria2c exit code: {process.returncode}. {last_line}",
                })

        except Exception as e:
            downloads[task_id].update({
                "status": "failed",
                "error": str(e),
            })


# --- App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global download_semaphore
    download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_task = asyncio.create_task(cleanup_old_files())
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="Download Proxy Server",
    description="Download files and serve them for fast re-downloading",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/download", response_model=DownloadStatus)
async def create_download(req: DownloadRequest, _=Depends(verify_api_key)):
    """Submit a new download task."""

    url = req.url
    headers = req.headers or {}

    # Parse curl command if provided
    if req.curl_command:
        url, parsed_headers = parse_curl_command(req.curl_command)
        headers = {**parsed_headers, **headers}  # explicit headers override
        if not url:
            raise HTTPException(400, "Could not parse URL from curl command")

    if not url:
        raise HTTPException(400, "URL is required")

    # Determine filename
    filename = req.filename or get_filename_from_url(url)

    # Sanitize filename
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")

    task_id = generate_task_id()
    connections = req.connections or MAX_CONNECTIONS

    downloads[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "filename": filename,
        "url": url,
        "created_at": datetime.now().isoformat(),
    }

    # Start download in background
    asyncio.create_task(run_download(task_id, url, headers, filename, connections))

    return DownloadStatus(
        task_id=task_id,
        status="queued",
        filename=filename,
        created_at=downloads[task_id]["created_at"],
    )


@app.post("/api/download/curl", response_model=DownloadStatus)
async def create_download_from_curl(
    _=Depends(verify_api_key),
    curl_command: str = "",
    filename: Optional[str] = None,
):
    """Submit download by pasting raw curl command in body."""
    if not curl_command:
        raise HTTPException(400, "curl_command is required")

    url, headers = parse_curl_command(curl_command)
    if not url:
        raise HTTPException(400, "Could not parse URL from curl command")

    fname = filename or get_filename_from_url(url)
    fname = fname.replace("/", "_").replace("\\", "_").replace("..", "_")

    task_id = generate_task_id()

    downloads[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "filename": fname,
        "url": url,
        "created_at": datetime.now().isoformat(),
    }

    asyncio.create_task(run_download(task_id, url, headers, fname, MAX_CONNECTIONS))

    return DownloadStatus(
        task_id=task_id,
        status="queued",
        filename=fname,
        created_at=downloads[task_id]["created_at"],
    )


@app.get("/api/status/{task_id}", response_model=DownloadStatus)
async def get_status(task_id: str, _=Depends(verify_api_key)):
    """Check download status."""
    if task_id not in downloads:
        raise HTTPException(404, "Task not found")

    info = downloads[task_id]
    return DownloadStatus(
        task_id=task_id,
        status=info.get("status", "unknown"),
        filename=info.get("filename"),
        file_size=info.get("file_size"),
        download_url=info.get("download_url"),
        progress=info.get("progress"),
        error=info.get("error"),
        created_at=info.get("created_at", ""),
        completed_at=info.get("completed_at"),
    )


@app.get("/api/files")
async def list_files(_=Depends(verify_api_key)):
    """List all downloaded files."""
    files = []
    if DOWNLOAD_DIR.exists():
        for f in sorted(DOWNLOAD_DIR.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "size": stat.st_size,
                    "size_human": _human_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "download_url": f"{SERVER_URL}/files/{f.name}",
                })
    return {"files": files, "total": len(files)}


@app.delete("/api/files/{filename}")
async def delete_file(filename: str, _=Depends(verify_api_key)):
    """Delete a downloaded file."""
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    filepath.unlink()
    return {"message": f"Deleted {filename}"}


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_api_key)):
    """List all download tasks."""
    return {
        "downloads": list(downloads.values()),
        "total": len(downloads),
    }


@app.get("/health")
async def health():
    """Health check."""
    disk = os.statvfs(str(DOWNLOAD_DIR)) if hasattr(os, 'statvfs') else None
    free_gb = (disk.f_bavail * disk.f_frsize / (1024**3)) if disk else None
    return {
        "status": "ok",
        "downloads_active": sum(1 for d in downloads.values() if d["status"] == "downloading"),
        "downloads_queued": sum(1 for d in downloads.values() if d["status"] == "queued"),
        "files_count": sum(1 for f in DOWNLOAD_DIR.iterdir() if f.is_file()) if DOWNLOAD_DIR.exists() else 0,
        "disk_free_gb": round(free_gb, 2) if free_gb else None,
    }


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
