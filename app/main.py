"""
Download Proxy Server - FastAPI Service
Downloads files using curl-impersonate (bypass Cloudflare) or aria2c (fast multi-connection)
"""

import os
import uuid
import shlex
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote, quote

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

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
    connections: Optional[int] = None
    curl_command: Optional[str] = None
    engine: Optional[str] = "auto"  # "auto", "curl", "aria2c"


class DownloadStatus(BaseModel):
    task_id: str
    status: str  # queued, downloading, completed, failed
    filename: Optional[str] = None
    file_size: Optional[int] = None
    download_url: Optional[str] = None
    progress: Optional[str] = None
    error: Optional[str] = None
    engine: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


# --- Helpers ---
def parse_curl_command(curl_cmd: str) -> tuple[str, dict]:
    """Parse a curl command string and extract URL + headers."""
    # Handle Windows \r\n and bash \ line continuations
    cleaned = curl_cmd.replace("\r\n", "\n").replace("\\\n", " ")
    try:
        parts = shlex.split(cleaned)
    except ValueError:
        # If shlex fails, try basic parsing
        parts = cleaned.split()

    url = ""
    headers = {}

    i = 0
    while i < len(parts):
        part = parts[i]
        if part in ("curl", "curl.exe", "curl_chrome", "curl-impersonate-chrome"):
            i += 1
            continue
        elif part in ("-H", "--header"):
            i += 1
            if i < len(parts):
                header_str = parts[i]
                if ": " in header_str:
                    key, val = header_str.split(": ", 1)
                    if not key.lower().startswith(("sec-ch-ua", "sec-fetch", "priority")):
                        headers[key.lower()] = val
                elif ":" in header_str:
                    key, val = header_str.split(":", 1)
                    if not key.lower().startswith(("sec-ch-ua", "sec-fetch", "priority")):
                        headers[key.lower()] = val.strip()
        elif part in ("-b", "--cookie"):
            i += 1
            if i < len(parts):
                headers["cookie"] = parts[i]
        elif part in ("-X", "--request", "-o", "--output"):
            i += 1  # skip next arg
        elif part in ("-L", "--location", "-k", "--insecure", "-s", "--silent",
                       "-S", "--show-error", "-v", "--verbose", "--compressed"):
            pass  # skip boolean flags
        elif not part.startswith("-") and not url:
            url = part.strip("'\"")
        i += 1

    return url, headers


def get_filename_from_url(url: str) -> str:
    """Extract filename from URL."""
    path = urlparse(url).path
    filename = unquote(path.split("/")[-1])
    if not filename or filename == "/":
        filename = f"download_{uuid.uuid4().hex[:8]}"
    return filename


def sanitize_filename(filename: str) -> str:
    """Clean filename of problematic characters."""
    filename = filename.strip().rstrip("^").strip()
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    # Remove any control characters
    filename = "".join(c for c in filename if c.isprintable() and c not in '<>:"|?*')
    return filename or f"download_{uuid.uuid4().hex[:8]}"


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
                            to_remove = [tid for tid, info in downloads.items()
                                         if info.get("filename") == f.name]
                            for tid in to_remove:
                                del downloads[tid]
        except Exception as e:
            print(f"Cleanup error: {e}")
        await asyncio.sleep(3600)


# --- Download Engines ---

async def download_with_curl_impersonate(task_id: str, url: str, headers: dict, filename: str):
    """Download using curl-impersonate (bypasses Cloudflare)."""
    filepath = DOWNLOAD_DIR / filename

    # Build curl-impersonate command
    cmd = [
        "curl_chrome",
        "-L",                    # follow redirects
        "-s", "-S",              # silent but show errors
        "--max-redirs", "10",
        "--retry", "3",
        "--retry-delay", "3",
        "--connect-timeout", "30",
        "--max-time", "3600",    # 1 hour max
        "-o", str(filepath),
        "-w", "%{http_code}|%{size_download}|%{speed_download}",
    ]

    # Add custom headers
    for key, val in headers.items():
        cmd.extend(["-H", f"{key}: {val}"])

    cmd.append(url)

    cmd_display = " ".join(f'"{c}"' if " " in c else c for c in cmd[:8])
    print(f"[Download {task_id}] ENGINE=curl_chrome CMD: {cmd_display}... URL: {url[:100]}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        stdout_text = stdout.decode("utf-8", errors="ignore").strip()
        stderr_text = stderr.decode("utf-8", errors="ignore").strip()

        print(f"[Download {task_id}] curl exit={process.returncode} stdout={stdout_text} stderr={stderr_text[:200]}")

        if process.returncode == 0 and filepath.exists():
            file_size = filepath.stat().st_size

            # Parse the -w output: http_code|size|speed
            parts = stdout_text.split("|")
            http_code = parts[0] if parts else "unknown"

            # Check if we got an error page instead of a file
            if file_size < 1000 and http_code.startswith(("4", "5")):
                content_preview = filepath.read_bytes()[:200].decode("utf-8", errors="ignore")
                filepath.unlink()
                return False, f"HTTP {http_code}: {content_preview[:100]}"

            if http_code.startswith(("4", "5")):
                # Got error response but file is large - might be error page
                filepath.unlink()
                return False, f"HTTP {http_code}"

            downloads[task_id].update({
                "status": "completed",
                "file_size": file_size,
                "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(),
                "progress": "100%",
            })
            return True, None
        else:
            error = stderr_text or stdout_text or f"curl exit code: {process.returncode}"
            return False, error

    except FileNotFoundError:
        return False, "curl_chrome not found. Install curl-impersonate."
    except Exception as e:
        return False, str(e)


async def download_with_aria2c(task_id: str, url: str, headers: dict, filename: str, connections: int):
    """Download using aria2c (fast multi-connection, no CF bypass)."""
    filepath = DOWNLOAD_DIR / filename

    cmd = [
        "aria2c",
        "-x", str(connections),
        "-s", str(connections),
        "-k", "1M",
        "-m", "5",
        "--retry-wait=3",
        "-t", "60",
        "--connect-timeout=30",
        "-c",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "-d", str(DOWNLOAD_DIR),
        "-o", filename,
        "--console-log-level=info",
        "--summary-interval=3",
        "--file-allocation=none",
    ]

    for key, val in headers.items():
        cmd.extend(["--header", f"{key}: {val}"])

    cmd.append(url)

    print(f"[Download {task_id}] ENGINE=aria2c URL: {url[:100]}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="ignore").strip()
            if decoded:
                output_lines.append(decoded)
                print(f"[Download {task_id}] {decoded}")
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
            return True, None
        else:
            error_lines = [l for l in output_lines if l and "see the log" not in l.lower()]
            error_detail = " | ".join(error_lines[-5:]) if error_lines else "Unknown error"
            return False, f"aria2c exit={process.returncode}. {error_detail}"

    except FileNotFoundError:
        return False, "aria2c not found."
    except Exception as e:
        return False, str(e)


async def run_download(task_id: str, url: str, headers: dict, filename: str,
                       connections: int, engine: str = "auto"):
    """Main download orchestrator."""
    async with download_semaphore:
        filename = sanitize_filename(filename)
        downloads[task_id]["status"] = "downloading"
        downloads[task_id]["filename"] = filename

        success = False
        error = None

        if engine == "aria2c":
            # Force aria2c
            downloads[task_id]["engine"] = "aria2c"
            success, error = await download_with_aria2c(task_id, url, headers, filename, connections)

        elif engine == "curl":
            # Force curl-impersonate
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl_impersonate(task_id, url, headers, filename)

        else:
            # Auto mode: try curl-impersonate first, fallback to aria2c
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl_impersonate(task_id, url, headers, filename)

            if not success and error and "not found" in error.lower():
                # curl-impersonate not installed, try aria2c
                print(f"[Download {task_id}] curl_chrome not available, falling back to aria2c")
                downloads[task_id]["engine"] = "aria2c"
                success, error = await download_with_aria2c(task_id, url, headers, filename, connections)

        if not success:
            downloads[task_id].update({
                "status": "failed",
                "error": error or "Unknown error",
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
    description="Download files using curl-impersonate (Cloudflare bypass) or aria2c",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Serve Web UI
@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/download", response_model=DownloadStatus)
async def create_download(req: DownloadRequest, _=Depends(verify_api_key)):
    """Submit a new download task.

    Engine options:
    - `auto` (default): try curl-impersonate first, fallback to aria2c
    - `curl`: force curl-impersonate (bypasses Cloudflare)
    - `aria2c`: force aria2c (faster, multi-connection, no CF bypass)
    """

    url = req.url
    headers = req.headers or {}

    # Parse curl command if provided
    if req.curl_command:
        url, parsed_headers = parse_curl_command(req.curl_command)
        headers = {**parsed_headers, **headers}
        if not url:
            raise HTTPException(400, "Could not parse URL from curl command")

    if not url:
        raise HTTPException(400, "URL is required")

    filename = req.filename or get_filename_from_url(url)
    filename = sanitize_filename(filename)
    engine = req.engine or "auto"

    task_id = generate_task_id()
    connections = req.connections or MAX_CONNECTIONS

    downloads[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "filename": filename,
        "url": url,
        "engine": engine,
        "created_at": datetime.now().isoformat(),
    }

    asyncio.create_task(run_download(task_id, url, headers, filename, connections, engine))

    return DownloadStatus(
        task_id=task_id,
        status="queued",
        filename=filename,
        engine=engine,
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
        engine=info.get("engine"),
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

    # Check which engines are available
    engines = {}
    import shutil
    engines["curl_chrome"] = shutil.which("curl_chrome") is not None
    engines["aria2c"] = shutil.which("aria2c") is not None

    return {
        "status": "ok",
        "engines": engines,
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
