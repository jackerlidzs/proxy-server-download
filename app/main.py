"""
Download Proxy Server - FastAPI Service v2.1
Downloads files using curl-impersonate (bypass Cloudflare) or aria2c (fast multi-connection)
Features: Real-time progress tracking, dual engine support
"""

import os
import re
import uuid
import shlex
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

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
    engine: Optional[str] = "auto"


class DownloadStatus(BaseModel):
    task_id: str
    status: str
    filename: Optional[str] = None
    file_size: Optional[int] = None
    download_url: Optional[str] = None
    progress: Optional[str] = None
    percent: Optional[float] = None
    speed: Optional[str] = None
    downloaded: Optional[int] = None
    total_size: Optional[int] = None
    error: Optional[str] = None
    engine: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


# --- Helpers ---
def parse_curl_command(curl_cmd: str) -> tuple[str, dict]:
    """Parse a curl command string and extract URL + headers."""
    cleaned = curl_cmd.replace("\r\n", "\n").replace("\\\n", " ")
    try:
        parts = shlex.split(cleaned)
    except ValueError:
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
            i += 1
        elif part in ("-L", "--location", "-k", "--insecure", "-s", "--silent",
                       "-S", "--show-error", "-v", "--verbose", "--compressed"):
            pass
        elif not part.startswith("-") and not url:
            url = part.strip("'\"")
        i += 1

    return url, headers


def get_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    filename = unquote(path.split("/")[-1])
    if not filename or filename == "/":
        filename = f"download_{uuid.uuid4().hex[:8]}"
    return filename


def sanitize_filename(filename: str) -> str:
    filename = filename.strip().rstrip("^").strip()
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    filename = "".join(c for c in filename if c.isprintable() and c not in '<>:"|?*')
    return filename or f"download_{uuid.uuid4().hex[:8]}"


def human_size(size_bytes) -> str:
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    size_bytes = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def human_speed(bytes_per_sec) -> str:
    if bytes_per_sec is None or bytes_per_sec == 0:
        return "0 B/s"
    return human_size(bytes_per_sec) + "/s"


def generate_task_id() -> str:
    return uuid.uuid4().hex[:12]


async def verify_api_key(authorization: Optional[str] = Header(None)):
    if API_KEY == "public":
        return True
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return True


async def cleanup_old_files():
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


async def monitor_file_progress(task_id: str, filepath: Path, total_size: int = 0):
    """Monitor download progress by checking file size."""
    start_time = asyncio.get_event_loop().time()
    last_size = 0

    while task_id in downloads and downloads[task_id]["status"] == "downloading":
        try:
            if filepath.exists():
                current_size = filepath.stat().st_size
                now = asyncio.get_event_loop().time()
                elapsed = now - start_time

                # Calculate speed (bytes per second)
                speed = current_size / elapsed if elapsed > 0 else 0

                # Recent speed (last check interval)
                interval_speed = (current_size - last_size) / 1.0  # 1 second interval

                downloads[task_id]["downloaded"] = current_size
                downloads[task_id]["speed"] = human_speed(interval_speed if interval_speed > 0 else speed)

                if total_size > 0:
                    pct = min(99.9, (current_size / total_size) * 100)
                    downloads[task_id]["percent"] = round(pct, 1)
                    downloads[task_id]["progress"] = f"{pct:.1f}%"
                    downloads[task_id]["total_size"] = total_size
                else:
                    downloads[task_id]["progress"] = human_size(current_size)

                last_size = current_size
        except Exception:
            pass

        await asyncio.sleep(1)


# --- Download Engines ---

async def download_with_curl_impersonate(task_id: str, url: str, headers: dict, filename: str):
    """Download using curl-impersonate (bypasses Cloudflare)."""
    filepath = DOWNLOAD_DIR / filename

    # Step 1: HEAD request to get total size
    head_cmd = [
        "curl_chrome", "-L", "-s", "-S", "-I",
        "--max-redirs", "10",
        "--connect-timeout", "15",
    ]
    for key, val in headers.items():
        head_cmd.extend(["-H", f"{key}: {val}"])
    head_cmd.append(url)

    total_size = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *head_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        head_text = stdout.decode("utf-8", errors="ignore").lower()
        for line in head_text.split("\n"):
            if "content-length:" in line:
                try:
                    total_size = int(line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
        print(f"[Download {task_id}] HEAD content-length: {total_size}")
    except Exception as e:
        print(f"[Download {task_id}] HEAD request failed: {e}")

    # Step 2: Download file
    cmd = [
        "curl_chrome",
        "-L",
        "-S",
        "--max-redirs", "10",
        "--retry", "3",
        "--retry-delay", "3",
        "--connect-timeout", "30",
        "--max-time", "7200",    # 2 hours max
        "-o", str(filepath),
    ]

    for key, val in headers.items():
        cmd.extend(["-H", f"{key}: {val}"])

    cmd.append(url)

    print(f"[Download {task_id}] ENGINE=curl_chrome URL: {url[:100]}")

    # Start file size monitor
    monitor_task = asyncio.create_task(
        monitor_file_progress(task_id, filepath, total_size)
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        stderr_text = stderr.decode("utf-8", errors="ignore").strip()
        print(f"[Download {task_id}] curl exit={process.returncode}")

        if process.returncode == 0 and filepath.exists():
            file_size = filepath.stat().st_size

            # Check for error pages (small HTML files)
            if file_size < 1000:
                try:
                    content = filepath.read_text(errors="ignore")
                    if "<html" in content.lower() or "403" in content or "forbidden" in content.lower():
                        filepath.unlink()
                        return False, f"Got error page ({file_size} bytes): {content[:100]}"
                except Exception:
                    pass

            downloads[task_id].update({
                "status": "completed",
                "file_size": file_size,
                "downloaded": file_size,
                "percent": 100.0,
                "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(),
                "progress": "100%",
                "speed": "",
            })
            return True, None
        else:
            if filepath.exists():
                filepath.unlink()
            error = stderr_text or f"curl exit code: {process.returncode}"
            return False, error

    except FileNotFoundError:
        return False, "curl_chrome not found. Install curl-impersonate."
    except Exception as e:
        return False, str(e)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


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
        "--summary-interval=1",
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
                # Parse aria2c progress: [#id SIZE/TOTAL(XX%) CN:N DL:SPEED]
                pct_match = re.search(r'(\d+)%', decoded)
                speed_match = re.search(r'DL:(\S+)', decoded)
                size_match = re.search(r'SIZE:(\S+)/(\S+)', decoded)

                if pct_match:
                    pct = float(pct_match.group(1))
                    downloads[task_id]["percent"] = pct
                    downloads[task_id]["progress"] = f"{pct:.0f}%"

                if speed_match:
                    downloads[task_id]["speed"] = speed_match.group(1) + "/s"

        await process.wait()

        if process.returncode == 0 and filepath.exists():
            file_size = filepath.stat().st_size
            downloads[task_id].update({
                "status": "completed",
                "file_size": file_size,
                "downloaded": file_size,
                "percent": 100.0,
                "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(),
                "progress": "100%",
                "speed": "",
            })
            return True, None
        else:
            error_lines = [l for l in output_lines if l and "see the log" not in l.lower()]
            error_detail = " | ".join(error_lines[-3:]) if error_lines else "Unknown error"
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
        downloads[task_id]["percent"] = 0
        downloads[task_id]["speed"] = ""
        downloads[task_id]["downloaded"] = 0

        success = False
        error = None

        if engine == "aria2c":
            downloads[task_id]["engine"] = "aria2c"
            success, error = await download_with_aria2c(task_id, url, headers, filename, connections)

        elif engine == "curl":
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl_impersonate(task_id, url, headers, filename)

        else:
            # Auto: try curl first, fallback to aria2c
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl_impersonate(task_id, url, headers, filename)

            if not success and error and "not found" in error.lower():
                print(f"[Download {task_id}] curl_chrome unavailable, fallback to aria2c")
                downloads[task_id]["engine"] = "aria2c"
                downloads[task_id]["percent"] = 0
                success, error = await download_with_aria2c(task_id, url, headers, filename, connections)

        if not success:
            downloads[task_id].update({
                "status": "failed",
                "error": error or "Unknown error",
                "speed": "",
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
    description="Download files with curl-impersonate (CF bypass) or aria2c",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/download", response_model=DownloadStatus)
async def create_download(req: DownloadRequest, _=Depends(verify_api_key)):
    url = req.url
    headers = req.headers or {}

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
        "percent": 0,
        "speed": "",
        "downloaded": 0,
        "total_size": 0,
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
        percent=info.get("percent"),
        speed=info.get("speed"),
        downloaded=info.get("downloaded"),
        total_size=info.get("total_size"),
        error=info.get("error"),
        engine=info.get("engine"),
        created_at=info.get("created_at", ""),
        completed_at=info.get("completed_at"),
    )


@app.get("/api/files")
async def list_files(_=Depends(verify_api_key)):
    files = []
    if DOWNLOAD_DIR.exists():
        for f in sorted(DOWNLOAD_DIR.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "size": stat.st_size,
                    "size_human": human_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "download_url": f"{SERVER_URL}/files/{f.name}",
                })
    return {"files": files, "total": len(files)}


@app.delete("/api/files/{filename}")
async def delete_file(filename: str, _=Depends(verify_api_key)):
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    filepath.unlink()
    return {"message": f"Deleted {filename}"}


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_api_key)):
    return {
        "downloads": list(downloads.values()),
        "total": len(downloads),
    }


@app.get("/health")
async def health():
    disk = os.statvfs(str(DOWNLOAD_DIR)) if hasattr(os, 'statvfs') else None
    free_gb = (disk.f_bavail * disk.f_frsize / (1024**3)) if disk else None

    import shutil
    engines = {
        "curl_chrome": shutil.which("curl_chrome") is not None,
        "aria2c": shutil.which("aria2c") is not None,
    }

    return {
        "status": "ok",
        "engines": engines,
        "downloads_active": sum(1 for d in downloads.values() if d["status"] == "downloading"),
        "downloads_queued": sum(1 for d in downloads.values() if d["status"] == "queued"),
        "files_count": sum(1 for f in DOWNLOAD_DIR.iterdir() if f.is_file() and not f.name.startswith(".")) if DOWNLOAD_DIR.exists() else 0,
        "disk_free_gb": round(free_gb, 2) if free_gb else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
