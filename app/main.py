"""
Download Proxy Server v3.0
Features: curl-impersonate (CF bypass), aria2c, auto-extract RAR, video streaming
"""

import os
import re
import uuid
import shlex
import shutil
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

# Configuration
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
API_KEY = os.getenv("API_KEY", "changeme")
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "16"))
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "48"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
SERVER_URL = os.getenv("SERVER_URL", "http://localhost")

# Media extensions
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
AUDIO_EXTS = {".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"}
RAR_PATTERN = re.compile(r'^(.+?)\.part(\d+)\.rar$', re.IGNORECASE)
SINGLE_RAR = re.compile(r'^(.+?)\.rar$', re.IGNORECASE)

# State
downloads: dict = {}
download_semaphore: asyncio.Semaphore = None
extract_queue: dict = {}  # group_name -> {total_parts, completed_parts, filenames}


# --- Models ---
class DownloadRequest(BaseModel):
    url: str
    headers: Optional[dict] = None
    filename: Optional[str] = None
    connections: Optional[int] = None
    curl_command: Optional[str] = None
    engine: Optional[str] = "auto"
    group: Optional[str] = None  # group name for multi-part files


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
    group: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


# --- Helpers ---
def parse_curl_command(curl_cmd: str) -> tuple[str, dict]:
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
    return filename if filename and filename != "/" else f"download_{uuid.uuid4().hex[:8]}"


def sanitize_filename(filename: str) -> str:
    filename = filename.strip().rstrip("^").strip()
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    filename = "".join(c for c in filename if c.isprintable() and c not in '<>:"|?*')
    return filename or f"download_{uuid.uuid4().hex[:8]}"


def human_size(size_bytes) -> str:
    if not size_bytes:
        return "0 B"
    size_bytes = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def human_speed(bps) -> str:
    return human_size(bps) + "/s" if bps else "0 B/s"


def generate_task_id() -> str:
    return uuid.uuid4().hex[:12]


def get_part_group(filename: str) -> tuple[str, int]:
    """Extract group name and part number from multi-part RAR filename."""
    m = RAR_PATTERN.match(filename)
    if m:
        return m.group(1), int(m.group(2))
    return "", 0


def is_media_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in VIDEO_EXTS | AUDIO_EXTS


def is_subtitle_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUBTITLE_EXTS


async def verify_api_key(authorization: Optional[str] = Header(None)):
    if API_KEY == "public":
        return True
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(403, "Invalid API key")
    return True


# --- Extraction ---
async def try_extract(filename: str):
    """Check if all parts are ready and extract."""
    group_name, part_num = get_part_group(filename)

    if not group_name:
        # Single RAR file
        if filename.lower().endswith('.rar'):
            asyncio.create_task(extract_rar(filename))
        return

    # Multi-part: check if all parts exist
    existing_parts = []
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file():
            g, p = get_part_group(f.name)
            if g == group_name:
                existing_parts.append(p)

    if not existing_parts:
        return

    max_part = max(existing_parts)
    all_parts = set(range(1, max_part + 1))

    if all_parts == set(existing_parts):
        # All parts present - find part1 to extract
        part1_name = f"{group_name}.part1.rar"
        if (DOWNLOAD_DIR / part1_name).exists():
            print(f"[Extract] All {max_part} parts ready for '{group_name}', starting extraction...")
            asyncio.create_task(extract_rar(part1_name, group_name=group_name))
    else:
        missing = all_parts - set(existing_parts)
        print(f"[Extract] '{group_name}': have parts {sorted(existing_parts)}, missing {sorted(missing)}")


async def extract_rar(filename: str, group_name: str = ""):
    """Extract a RAR file and delete archives after success."""
    filepath = DOWNLOAD_DIR / filename
    extract_id = f"extract_{generate_task_id()}"

    downloads[extract_id] = {
        "task_id": extract_id,
        "status": "extracting",
        "filename": filename,
        "group": group_name,
        "progress": "Extracting...",
        "percent": 50,
        "created_at": datetime.now().isoformat(),
    }

    try:
        cmd = ["unrar", "x", "-o+", "-y", str(filepath), str(DOWNLOAD_DIR) + "/"]
        print(f"[Extract {extract_id}] CMD: {' '.join(cmd)}")

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
                # Parse percentage
                pct_match = re.search(r'(\d+)%', decoded)
                if pct_match:
                    downloads[extract_id]["percent"] = float(pct_match.group(1))
                    downloads[extract_id]["progress"] = f"Extracting {pct_match.group(1)}%"
                elif "Extracting" in decoded or "extracting" in decoded:
                    downloads[extract_id]["progress"] = decoded[:60]

        await process.wait()

        if process.returncode == 0:
            # Find extracted files
            extracted = []
            for f in DOWNLOAD_DIR.iterdir():
                if f.is_file() and not f.name.endswith('.rar') and not f.name.startswith('.'):
                    if is_media_file(f.name) or is_subtitle_file(f.name):
                        extracted.append(f.name)

            # Delete RAR files
            if group_name:
                for f in list(DOWNLOAD_DIR.iterdir()):
                    g, _ = get_part_group(f.name)
                    if g == group_name and f.name.endswith('.rar'):
                        f.unlink()
                        print(f"[Extract] Deleted {f.name}")
            else:
                filepath.unlink()
                print(f"[Extract] Deleted {filename}")

            downloads[extract_id].update({
                "status": "completed",
                "progress": "100%",
                "percent": 100,
                "completed_at": datetime.now().isoformat(),
                "filename": ", ".join(extracted[:3]) if extracted else filename,
            })
            print(f"[Extract] ✅ Done! Extracted: {extracted}")
        else:
            error = "\n".join(output_lines[-5:])
            downloads[extract_id].update({
                "status": "failed",
                "error": f"unrar exit {process.returncode}: {error}",
            })
            print(f"[Extract] ❌ Failed: {error}")

    except FileNotFoundError:
        downloads[extract_id].update({
            "status": "failed",
            "error": "unrar not found. Install unrar package.",
        })
    except Exception as e:
        downloads[extract_id].update({
            "status": "failed",
            "error": str(e),
        })


# --- Download Engines ---
async def monitor_file_progress(task_id: str, filepath: Path, total_size: int = 0):
    start_time = asyncio.get_event_loop().time()
    last_size = 0
    while task_id in downloads and downloads[task_id]["status"] == "downloading":
        try:
            if filepath.exists():
                current_size = filepath.stat().st_size
                now = asyncio.get_event_loop().time()
                elapsed = now - start_time
                speed = current_size / elapsed if elapsed > 0 else 0
                interval_speed = (current_size - last_size) / 1.0
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


async def download_with_curl(task_id, url, headers, filename):
    filepath = DOWNLOAD_DIR / filename
    # HEAD request for size
    head_cmd = ["curl_chrome", "-L", "-s", "-S", "-I", "--max-redirs", "10", "--connect-timeout", "15"]
    for k, v in headers.items():
        head_cmd.extend(["-H", f"{k}: {v}"])
    head_cmd.append(url)

    total_size = 0
    try:
        proc = await asyncio.create_subprocess_exec(*head_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        for line in stdout.decode("utf-8", errors="ignore").lower().split("\n"):
            if "content-length:" in line:
                try:
                    total_size = int(line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

    cmd = ["curl_chrome", "-L", "-S", "--max-redirs", "10", "--retry", "3",
           "--retry-delay", "3", "--connect-timeout", "30", "--max-time", "7200",
           "-o", str(filepath)]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)

    monitor = asyncio.create_task(monitor_file_progress(task_id, filepath, total_size))
    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()

        if process.returncode == 0 and filepath.exists():
            fsize = filepath.stat().st_size
            if fsize < 1000:
                try:
                    content = filepath.read_text(errors="ignore")
                    if "<html" in content.lower() or "403" in content:
                        filepath.unlink()
                        return False, f"Error page ({fsize}B): {content[:100]}"
                except Exception:
                    pass

            downloads[task_id].update({
                "status": "completed", "file_size": fsize, "downloaded": fsize,
                "percent": 100.0, "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": "",
            })
            return True, None
        else:
            if filepath.exists():
                filepath.unlink()
            return False, stderr.decode("utf-8", errors="ignore").strip() or f"curl exit {process.returncode}"
    except FileNotFoundError:
        return False, "curl_chrome not found"
    except Exception as e:
        return False, str(e)
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass


async def download_with_aria2c(task_id, url, headers, filename, connections):
    filepath = DOWNLOAD_DIR / filename
    cmd = ["aria2c", "-x", str(connections), "-s", str(connections), "-k", "1M",
           "-m", "5", "--retry-wait=3", "-t", "60", "--connect-timeout=30", "-c",
           "--auto-file-renaming=false", "--allow-overwrite=true",
           "-d", str(DOWNLOAD_DIR), "-o", filename,
           "--console-log-level=info", "--summary-interval=1", "--file-allocation=none"]
    for k, v in headers.items():
        cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(url)

    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output_lines = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="ignore").strip()
            if decoded:
                output_lines.append(decoded)
                pct_match = re.search(r'(\d+)%', decoded)
                speed_match = re.search(r'DL:(\S+)', decoded)
                if pct_match:
                    downloads[task_id]["percent"] = float(pct_match.group(1))
                    downloads[task_id]["progress"] = f"{pct_match.group(1)}%"
                if speed_match:
                    downloads[task_id]["speed"] = speed_match.group(1) + "/s"
        await process.wait()

        if process.returncode == 0 and filepath.exists():
            fsize = filepath.stat().st_size
            downloads[task_id].update({
                "status": "completed", "file_size": fsize, "downloaded": fsize,
                "percent": 100.0, "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": "",
            })
            return True, None
        else:
            errs = [l for l in output_lines if "see the log" not in l.lower()]
            return False, " | ".join(errs[-3:]) or "Unknown error"
    except FileNotFoundError:
        return False, "aria2c not found"
    except Exception as e:
        return False, str(e)


async def run_download(task_id, url, headers, filename, connections, engine="auto"):
    async with download_semaphore:
        filename = sanitize_filename(filename)
        downloads[task_id]["status"] = "downloading"
        downloads[task_id]["filename"] = filename
        downloads[task_id]["percent"] = 0
        downloads[task_id]["speed"] = ""
        downloads[task_id]["downloaded"] = 0

        success, error = False, None

        if engine == "aria2c":
            downloads[task_id]["engine"] = "aria2c"
            success, error = await download_with_aria2c(task_id, url, headers, filename, connections)
        elif engine == "curl":
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl(task_id, url, headers, filename)
        else:
            downloads[task_id]["engine"] = "curl_chrome"
            success, error = await download_with_curl(task_id, url, headers, filename)
            if not success and error and "not found" in error.lower():
                downloads[task_id]["engine"] = "aria2c"
                downloads[task_id]["percent"] = 0
                success, error = await download_with_aria2c(task_id, url, headers, filename, connections)

        if not success:
            downloads[task_id].update({"status": "failed", "error": error or "Unknown", "speed": ""})
        else:
            # Check if this is a RAR file → try extraction
            await try_extract(filename)


# --- Cleanup ---
async def cleanup_old_files():
    while True:
        try:
            cutoff = datetime.now() - timedelta(hours=CLEANUP_HOURS)
            if DOWNLOAD_DIR.exists():
                for f in DOWNLOAD_DIR.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                            f.unlink()
                            for tid in [t for t, i in downloads.items() if i.get("filename") == f.name]:
                                del downloads[tid]
        except Exception as e:
            print(f"Cleanup error: {e}")
        await asyncio.sleep(3600)


# --- App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global download_semaphore
    download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(cleanup_old_files())
    yield
    task.cancel()

app = FastAPI(title="Download Proxy + Media Server", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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

    filename = sanitize_filename(req.filename or get_filename_from_url(url))
    engine = req.engine or "auto"
    task_id = generate_task_id()
    connections = req.connections or MAX_CONNECTIONS

    # Detect group for multi-part
    group, _ = get_part_group(filename)

    downloads[task_id] = {
        "task_id": task_id, "status": "queued", "filename": filename,
        "url": url, "engine": engine, "group": group,
        "percent": 0, "speed": "", "downloaded": 0, "total_size": 0,
        "created_at": datetime.now().isoformat(),
    }

    asyncio.create_task(run_download(task_id, url, headers, filename, connections, engine))

    return DownloadStatus(
        task_id=task_id, status="queued", filename=filename,
        engine=engine, group=group, created_at=downloads[task_id]["created_at"],
    )


@app.get("/api/status/{task_id}", response_model=DownloadStatus)
async def get_status(task_id: str, _=Depends(verify_api_key)):
    if task_id not in downloads:
        raise HTTPException(404, "Task not found")
    info = downloads[task_id]
    return DownloadStatus(**{k: info.get(k) for k in DownloadStatus.model_fields if k in info})


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
                    "is_media": is_media_file(f.name),
                    "is_subtitle": is_subtitle_file(f.name),
                    "is_rar": f.name.endswith('.rar'),
                })
    return {"files": files, "total": len(files)}


@app.get("/api/media")
async def list_media(_=Depends(verify_api_key)):
    """List media files (video/audio) with matching subtitles."""
    media = []
    subtitles = []

    if DOWNLOAD_DIR.exists():
        for f in sorted(DOWNLOAD_DIR.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                if is_media_file(f.name):
                    stat = f.stat()
                    media.append({
                        "filename": f.name,
                        "size": stat.st_size,
                        "size_human": human_size(stat.st_size),
                        "stream_url": f"{SERVER_URL}/stream/{f.name}",
                        "download_url": f"{SERVER_URL}/files/{f.name}",
                        "ext": Path(f.name).suffix.lower(),
                        "subtitles": [],
                    })
                elif is_subtitle_file(f.name):
                    subtitles.append(f.name)

    # Match subtitles to media
    for m in media:
        base = Path(m["filename"]).stem.lower()
        for sub in subtitles:
            sub_base = Path(sub).stem.lower()
            # Match if subtitle name starts with video name
            if sub_base.startswith(base) or base.startswith(sub_base.rsplit('.', 1)[0]):
                m["subtitles"].append({
                    "filename": sub,
                    "url": f"{SERVER_URL}/files/{sub}",
                    "ext": Path(sub).suffix.lower(),
                })

    return {"media": media, "total": len(media)}


@app.get("/stream/{filename}")
async def stream_file(filename: str, request: Request):
    """Stream a media file with range request support."""
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")

    file_size = filepath.stat().st_size
    ext = Path(filename).suffix.lower()

    content_types = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
        ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".ts": "video/mp2t", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
        ".mp3": "audio/mpeg", ".flac": "audio/flac", ".aac": "audio/aac",
        ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
        ".srt": "text/plain", ".vtt": "text/vtt", ".ass": "text/plain",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    # Handle Range requests
    range_header = request.headers.get("range")

    if range_header:
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            async def range_generator():
                with open(filepath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk_size = min(65536, remaining)
                        data = f.read(chunk_size)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            return StreamingResponse(
                range_generator(),
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Type": content_type,
                    "Access-Control-Allow-Origin": "*",
                },
            )

    # Full file
    async def file_generator():
        with open(filepath, "rb") as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                yield data

    return StreamingResponse(
        file_generator(),
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": content_type,
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.delete("/api/files/{filename}")
async def delete_file(filename: str, _=Depends(verify_api_key)):
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    filepath.unlink()
    return {"message": f"Deleted {filename}"}


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_api_key)):
    return {"downloads": list(downloads.values()), "total": len(downloads)}


@app.post("/api/extract/{filename}")
async def manual_extract(filename: str, _=Depends(verify_api_key)):
    """Manually trigger extraction of a RAR file."""
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    if not filename.endswith('.rar'):
        raise HTTPException(400, "Not a RAR file")
    await try_extract(filename)
    return {"message": f"Extraction started for {filename}"}


@app.get("/health")
async def health():
    disk = os.statvfs(str(DOWNLOAD_DIR)) if hasattr(os, 'statvfs') else None
    free_gb = (disk.f_bavail * disk.f_frsize / (1024**3)) if disk else None
    engines = {"curl_chrome": shutil.which("curl_chrome") is not None,
               "aria2c": shutil.which("aria2c") is not None,
               "unrar": shutil.which("unrar") is not None}
    media_count = sum(1 for f in DOWNLOAD_DIR.iterdir() if f.is_file() and is_media_file(f.name)) if DOWNLOAD_DIR.exists() else 0

    return {
        "status": "ok", "engines": engines,
        "downloads_active": sum(1 for d in downloads.values() if d["status"] in ("downloading", "extracting")),
        "downloads_queued": sum(1 for d in downloads.values() if d["status"] == "queued"),
        "files_count": sum(1 for f in DOWNLOAD_DIR.iterdir() if f.is_file() and not f.name.startswith(".")) if DOWNLOAD_DIR.exists() else 0,
        "media_count": media_count,
        "disk_free_gb": round(free_gb, 2) if free_gb else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
