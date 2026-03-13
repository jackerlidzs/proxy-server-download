"""
Download Proxy + Media Server v4.0
Features: curl-impersonate, aria2c, auto-extract, video streaming, file management
"""

import os
import re
import uuid
import shlex
import shutil
import asyncio
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

# Config
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
API_KEY = os.getenv("API_KEY", "changeme")
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "16"))
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
SERVER_URL = os.getenv("SERVER_URL", "http://localhost")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
AUDIO_EXTS = {".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"}
ARCHIVE_EXTS = {".rar", ".zip", ".7z", ".tar", ".gz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
RAR_PATTERN = re.compile(r'^(.+?)\.part(\d+)\.rar$', re.IGNORECASE)

downloads: dict = {}
semaphore: asyncio.Semaphore = None


# --- Models ---
class DownloadRequest(BaseModel):
    url: str
    headers: Optional[dict] = None
    filename: Optional[str] = None
    connections: Optional[int] = None
    curl_command: Optional[str] = None
    engine: Optional[str] = "auto"


class RenameRequest(BaseModel):
    new_name: str


class BulkDeleteRequest(BaseModel):
    filenames: list[str]


class CreateFolderRequest(BaseModel):
    name: str


# --- Helpers ---
def parse_curl_command(cmd: str):
    cleaned = cmd.replace("\r\n", "\n").replace("\\\n", " ")
    try:
        parts = shlex.split(cleaned)
    except ValueError:
        parts = cleaned.split()
    url, headers = "", {}
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("curl", "curl.exe", "curl_chrome", "curl-impersonate-chrome"):
            pass
        elif p in ("-H", "--header"):
            i += 1
            if i < len(parts):
                h = parts[i]
                sep = ": " if ": " in h else (":" if ":" in h else None)
                if sep:
                    k, v = h.split(sep, 1)
                    if not k.lower().startswith(("sec-ch-ua", "sec-fetch", "priority")):
                        headers[k.lower()] = v.strip()
        elif p in ("-b", "--cookie"):
            i += 1
            if i < len(parts):
                headers["cookie"] = parts[i]
        elif p in ("-X", "--request", "-o", "--output"):
            i += 1
        elif p in ("-L", "-k", "-s", "-S", "-v", "--location", "--insecure", "--silent", "--show-error", "--verbose", "--compressed"):
            pass
        elif not p.startswith("-") and not url:
            url = p.strip("'\"")
        i += 1
    return url, headers


def filename_from_url(url):
    fn = unquote(urlparse(url).path.split("/")[-1])
    return fn if fn and fn != "/" else f"download_{uuid.uuid4().hex[:8]}"


def sanitize(fn):
    fn = fn.strip().rstrip("^").strip()
    fn = fn.replace("/", "_").replace("\\", "_").replace("..", "_")
    fn = "".join(c for c in fn if c.isprintable() and c not in '<>:"|?*')
    return fn or f"download_{uuid.uuid4().hex[:8]}"


def human_size(b):
    if not b: return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def human_speed(bps):
    return human_size(bps) + "/s" if bps else ""


def file_type(name):
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS: return "video"
    if ext in AUDIO_EXTS: return "audio"
    if ext in SUBTITLE_EXTS: return "subtitle"
    if ext in ARCHIVE_EXTS: return "archive"
    if ext in IMAGE_EXTS: return "image"
    return "file"


def part_group(fn):
    m = RAR_PATTERN.match(fn)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


async def verify_key(authorization: Optional[str] = Header(None)):
    if API_KEY == "public": return True
    if not authorization: raise HTTPException(401, "Missing Authorization")
    if authorization.replace("Bearer ", "").strip() != API_KEY:
        raise HTTPException(403, "Invalid API key")
    return True


# --- Extraction ---
async def try_extract(filename):
    group, part = part_group(filename)
    if not group:
        if filename.lower().endswith('.rar'):
            asyncio.create_task(do_extract(filename))
        return
    existing = []
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file():
            g, p = part_group(f.name)
            if g == group: existing.append(p)
    if existing and set(range(1, max(existing)+1)) == set(existing):
        part1 = f"{group}.part1.rar"
        if (DOWNLOAD_DIR / part1).exists():
            asyncio.create_task(do_extract(part1, group))


async def do_extract(filename, group=""):
    eid = f"ext_{uuid.uuid4().hex[:8]}"
    downloads[eid] = {"task_id": eid, "status": "extracting", "filename": filename,
                      "group": group, "progress": "Extracting...", "percent": 50,
                      "created_at": datetime.now().isoformat()}
    try:
        proc = await asyncio.create_subprocess_exec(
            "unrar", "x", "-o+", "-y", str(DOWNLOAD_DIR / filename), str(DOWNLOAD_DIR) + "/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line: break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                lines.append(dec)
                m = re.search(r'(\d+)%', dec)
                if m: downloads[eid]["percent"] = float(m.group(1))
        await proc.wait()
        if proc.returncode == 0:
            # Delete archives
            if group:
                for f in list(DOWNLOAD_DIR.iterdir()):
                    g, _ = part_group(f.name)
                    if g == group and f.name.endswith('.rar'): f.unlink()
            else:
                (DOWNLOAD_DIR / filename).unlink(missing_ok=True)
            downloads[eid].update({"status": "completed", "percent": 100, "progress": "100%",
                                   "completed_at": datetime.now().isoformat()})
        else:
            downloads[eid].update({"status": "failed", "error": "\n".join(lines[-5:])})
    except FileNotFoundError:
        downloads[eid].update({"status": "failed", "error": "unrar not found"})
    except Exception as e:
        downloads[eid].update({"status": "failed", "error": str(e)})


# --- Download Engines ---
async def monitor_progress(tid, fp, total=0):
    t0 = asyncio.get_event_loop().time()
    last = 0
    while tid in downloads and downloads[tid]["status"] == "downloading":
        try:
            if fp.exists():
                sz = fp.stat().st_size
                el = asyncio.get_event_loop().time() - t0
                spd = (sz - last) / 1.0
                downloads[tid]["downloaded"] = sz
                downloads[tid]["speed"] = human_speed(spd if spd > 0 else sz / max(el, 1))
                if total > 0:
                    pct = min(99.9, sz / total * 100)
                    downloads[tid].update({"percent": round(pct, 1), "progress": f"{pct:.1f}%", "total_size": total})
                else:
                    downloads[tid]["progress"] = human_size(sz)
                last = sz
        except Exception: pass
        await asyncio.sleep(1)


async def dl_curl(tid, url, headers, filename):
    fp = DOWNLOAD_DIR / filename
    total = 0
    try:
        hcmd = ["curl_chrome", "-L", "-s", "-S", "-I", "--max-redirs", "10", "--connect-timeout", "15"]
        for k, v in headers.items(): hcmd.extend(["-H", f"{k}: {v}"])
        hcmd.append(url)
        p = await asyncio.create_subprocess_exec(*hcmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=20)
        for line in out.decode(errors="ignore").lower().split("\n"):
            if "content-length:" in line:
                try: total = int(line.split(":", 1)[1].strip())
                except: pass
    except: pass

    cmd = ["curl_chrome", "-L", "-S", "--max-redirs", "10", "--retry", "3", "--retry-delay", "3",
           "--connect-timeout", "30", "--max-time", "7200", "-o", str(fp)]
    for k, v in headers.items(): cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)

    mon = asyncio.create_task(monitor_progress(tid, fp, total))
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and fp.exists():
            sz = fp.stat().st_size
            if sz < 1000:
                try:
                    c = fp.read_text(errors="ignore")
                    if "<html" in c.lower() or "403" in c: fp.unlink(); return False, f"Error page: {c[:100]}"
                except: pass
            downloads[tid].update({"status": "completed", "file_size": sz, "downloaded": sz, "percent": 100.0,
                                   "download_url": f"{SERVER_URL}/files/{filename}",
                                   "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": ""})
            return True, None
        if fp.exists(): fp.unlink()
        return False, stderr.decode(errors="ignore").strip() or f"curl exit {proc.returncode}"
    except FileNotFoundError: return False, "curl_chrome not found"
    except Exception as e: return False, str(e)
    finally:
        mon.cancel()
        try: await mon
        except asyncio.CancelledError: pass


async def dl_aria2c(tid, url, headers, filename, conns):
    fp = DOWNLOAD_DIR / filename
    cmd = ["aria2c", "-x", str(conns), "-s", str(conns), "-k", "1M", "-m", "5", "--retry-wait=3",
           "-t", "60", "--connect-timeout=30", "-c", "--auto-file-renaming=false", "--allow-overwrite=true",
           "-d", str(DOWNLOAD_DIR), "-o", filename, "--summary-interval=1", "--file-allocation=none"]
    for k, v in headers.items(): cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line: break
            dec = line.decode(errors="ignore").strip()
            if dec:
                lines.append(dec)
                m = re.search(r'(\d+)%', dec)
                sm = re.search(r'DL:(\S+)', dec)
                if m: downloads[tid].update({"percent": float(m.group(1)), "progress": f"{m.group(1)}%"})
                if sm: downloads[tid]["speed"] = sm.group(1) + "/s"
        await proc.wait()
        if proc.returncode == 0 and fp.exists():
            sz = fp.stat().st_size
            downloads[tid].update({"status": "completed", "file_size": sz, "downloaded": sz, "percent": 100.0,
                                   "download_url": f"{SERVER_URL}/files/{filename}",
                                   "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": ""})
            return True, None
        return False, " | ".join(lines[-3:]) or "Unknown error"
    except FileNotFoundError: return False, "aria2c not found"
    except Exception as e: return False, str(e)


async def run_download(tid, url, headers, filename, conns, engine="auto"):
    async with semaphore:
        filename = sanitize(filename)
        downloads[tid].update({"status": "downloading", "filename": filename, "percent": 0, "speed": "", "downloaded": 0})
        ok, err = False, None
        if engine == "aria2c":
            downloads[tid]["engine"] = "aria2c"
            ok, err = await dl_aria2c(tid, url, headers, filename, conns)
        elif engine == "curl":
            downloads[tid]["engine"] = "curl_chrome"
            ok, err = await dl_curl(tid, url, headers, filename)
        else:
            downloads[tid]["engine"] = "curl_chrome"
            ok, err = await dl_curl(tid, url, headers, filename)
            if not ok and err and "not found" in err.lower():
                downloads[tid]["engine"] = "aria2c"
                downloads[tid]["percent"] = 0
                ok, err = await dl_aria2c(tid, url, headers, filename, conns)
        if not ok: downloads[tid].update({"status": "failed", "error": err or "Unknown", "speed": ""})
        else: await try_extract(filename)


async def cleanup():
    while True:
        try:
            cut = datetime.now() - timedelta(hours=CLEANUP_HOURS)
            if DOWNLOAD_DIR.exists():
                for f in DOWNLOAD_DIR.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        if datetime.fromtimestamp(f.stat().st_mtime) < cut: f.unlink()
        except: pass
        await asyncio.sleep(3600)


# --- App ---
@asynccontextmanager
async def lifespan(app):
    global semaphore
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    t = asyncio.create_task(cleanup())
    yield
    t.cancel()

app = FastAPI(title="Download Proxy + Media Server", version="4.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(STATIC_DIR / "index.html")


# --- Download API ---
@app.post("/api/download")
async def create_download(req: DownloadRequest, _=Depends(verify_key)):
    url, headers = req.url, req.headers or {}
    if req.curl_command:
        url, ph = parse_curl_command(req.curl_command)
        headers = {**ph, **headers}
        if not url: raise HTTPException(400, "Could not parse URL")
    if not url: raise HTTPException(400, "URL required")
    fn = sanitize(req.filename or filename_from_url(url))
    tid = uuid.uuid4().hex[:12]
    downloads[tid] = {"task_id": tid, "status": "queued", "filename": fn, "url": url,
                      "engine": req.engine or "auto", "percent": 0, "speed": "",
                      "downloaded": 0, "total_size": 0, "created_at": datetime.now().isoformat()}
    asyncio.create_task(run_download(tid, url, headers, fn, req.connections or MAX_CONNECTIONS, req.engine or "auto"))
    return {"task_id": tid, "status": "queued", "filename": fn, "created_at": downloads[tid]["created_at"]}


@app.get("/api/downloads")
async def list_downloads(_=Depends(verify_key)):
    return {"downloads": list(downloads.values()), "total": len(downloads)}


@app.get("/api/status/{tid}")
async def get_status(tid: str, _=Depends(verify_key)):
    if tid not in downloads: raise HTTPException(404)
    return downloads[tid]


# --- File Management API ---
@app.get("/api/files")
async def list_files(path: str = "", _=Depends(verify_key)):
    target = DOWNLOAD_DIR / path if path else DOWNLOAD_DIR
    if not target.exists(): raise HTTPException(404, "Path not found")
    if not str(target.resolve()).startswith(str(DOWNLOAD_DIR.resolve())):
        raise HTTPException(403, "Access denied")

    items = []
    for f in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if f.name.startswith("."): continue
        stat = f.stat()
        if f.is_dir():
            # Count items in folder
            count = sum(1 for x in f.iterdir() if not x.name.startswith("."))
            size = sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
            items.append({"name": f.name, "type": "folder", "size": size, "size_human": human_size(size),
                          "items": count, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                          "path": str(f.relative_to(DOWNLOAD_DIR))})
        else:
            items.append({"name": f.name, "type": file_type(f.name), "size": stat.st_size,
                          "size_human": human_size(stat.st_size),
                          "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                          "path": str(f.relative_to(DOWNLOAD_DIR)),
                          "download_url": f"{SERVER_URL}/files/{f.relative_to(DOWNLOAD_DIR)}",
                          "stream_url": f"{SERVER_URL}/stream/{f.relative_to(DOWNLOAD_DIR)}" if file_type(f.name) in ("video", "audio") else None,
                          "ext": f.suffix.lower()})
    return {"items": items, "total": len(items), "current_path": path}


@app.post("/api/files/rename/{filename:path}")
async def rename_file(filename: str, req: RenameRequest, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filename
    if not fp.exists(): raise HTTPException(404)
    new = sanitize(req.new_name)
    new_path = fp.parent / new
    if new_path.exists(): raise HTTPException(409, f"'{new}' already exists")
    fp.rename(new_path)
    return {"message": f"Renamed to {new}", "new_name": new}


@app.post("/api/files/mkdir")
async def create_folder(req: CreateFolderRequest, path: str = "", _=Depends(verify_key)):
    target = DOWNLOAD_DIR / path / sanitize(req.name)
    if target.exists(): raise HTTPException(409, "Folder already exists")
    target.mkdir(parents=True)
    return {"message": f"Created folder {req.name}"}


@app.post("/api/files/delete-bulk")
async def bulk_delete(req: BulkDeleteRequest, _=Depends(verify_key)):
    deleted = []
    for fn in req.filenames:
        fp = DOWNLOAD_DIR / fn
        if not str(fp.resolve()).startswith(str(DOWNLOAD_DIR.resolve())): continue
        if fp.is_dir(): shutil.rmtree(fp); deleted.append(fn)
        elif fp.is_file(): fp.unlink(); deleted.append(fn)
    return {"deleted": deleted, "count": len(deleted)}


@app.delete("/api/files/{filename:path}")
async def delete_file(filename: str, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filename
    if not fp.exists(): raise HTTPException(404)
    if fp.is_dir(): shutil.rmtree(fp)
    else: fp.unlink()
    return {"message": f"Deleted {filename}"}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), path: str = Form(""), _=Depends(verify_key)):
    target_dir = DOWNLOAD_DIR / path if path else DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    fp = target_dir / sanitize(file.filename or "uploaded_file")
    with open(fp, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"filename": fp.name, "size": fp.stat().st_size, "size_human": human_size(fp.stat().st_size)}


@app.post("/api/extract/{filename:path}")
async def extract_file(filename: str, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filename
    if not fp.exists(): raise HTTPException(404)
    if not fp.name.endswith('.rar'): raise HTTPException(400, "Not a RAR file")
    await try_extract(fp.name)
    return {"message": f"Extraction started for {filename}"}


# --- Media API ---
@app.get("/api/media")
async def list_media(_=Depends(verify_key)):
    media, subs = [], []
    if DOWNLOAD_DIR.exists():
        for f in sorted(DOWNLOAD_DIR.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                ext = f.suffix.lower()
                if ext in VIDEO_EXTS | AUDIO_EXTS:
                    rel = str(f.relative_to(DOWNLOAD_DIR))
                    media.append({"filename": f.name, "path": rel, "size": f.stat().st_size,
                                  "size_human": human_size(f.stat().st_size),
                                  "stream_url": f"{SERVER_URL}/stream/{rel}",
                                  "download_url": f"{SERVER_URL}/files/{rel}",
                                  "ext": ext, "type": "video" if ext in VIDEO_EXTS else "audio",
                                  "subtitles": []})
                elif ext in SUBTITLE_EXTS:
                    subs.append({"filename": f.name, "path": str(f.relative_to(DOWNLOAD_DIR)), "ext": ext})
    for m in media:
        base = Path(m["filename"]).stem.lower()
        for s in subs:
            sbase = Path(s["filename"]).stem.lower()
            if sbase.startswith(base) or base.startswith(sbase.rsplit(".", 1)[0]):
                m["subtitles"].append({"filename": s["filename"],
                                        "url": f"{SERVER_URL}/files/{s['path']}", "ext": s["ext"]})
    return {"media": media, "total": len(media)}


# --- Streaming ---
@app.get("/stream/{filename:path}")
async def stream(filename: str, request: Request):
    fp = DOWNLOAD_DIR / filename
    if not fp.exists(): raise HTTPException(404)
    sz = fp.stat().st_size
    ct_map = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
              ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".m4v": "video/mp4",
              ".ts": "video/mp2t", ".mp3": "audio/mpeg", ".flac": "audio/flac",
              ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
              ".m4a": "audio/mp4", ".srt": "text/plain", ".vtt": "text/vtt"}
    ct = ct_map.get(fp.suffix.lower(), "application/octet-stream")
    rng = request.headers.get("range")
    if rng:
        m = re.match(r'bytes=(\d+)-(\d*)', rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else sz - 1
            end = min(end, sz - 1)
            length = end - start + 1
            async def gen():
                with open(fp, "rb") as f:
                    f.seek(start)
                    rem = length
                    while rem > 0:
                        d = f.read(min(65536, rem))
                        if not d: break
                        rem -= len(d)
                        yield d
            return StreamingResponse(gen(), status_code=206, headers={
                "Content-Range": f"bytes {start}-{end}/{sz}", "Accept-Ranges": "bytes",
                "Content-Length": str(length), "Content-Type": ct, "Access-Control-Allow-Origin": "*"})
    async def gen_full():
        with open(fp, "rb") as f:
            while d := f.read(65536):
                yield d
    return StreamingResponse(gen_full(), headers={
        "Accept-Ranges": "bytes", "Content-Length": str(sz),
        "Content-Type": ct, "Access-Control-Allow-Origin": "*"})


# --- Health / Disk ---
@app.get("/health")
async def health():
    disk = os.statvfs(str(DOWNLOAD_DIR)) if hasattr(os, "statvfs") else None
    free = (disk.f_bavail * disk.f_frsize / 1073741824) if disk else None
    total_d = (disk.f_blocks * disk.f_frsize / 1073741824) if disk else None
    used = total_d - free if total_d and free else None
    media_c = sum(1 for f in DOWNLOAD_DIR.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS) if DOWNLOAD_DIR.exists() else 0
    return {"status": "ok",
            "engines": {"curl_chrome": shutil.which("curl_chrome") is not None,
                        "aria2c": shutil.which("aria2c") is not None,
                        "unrar": shutil.which("unrar") is not None},
            "downloads_active": sum(1 for d in downloads.values() if d["status"] in ("downloading", "extracting")),
            "files_count": sum(1 for f in DOWNLOAD_DIR.rglob("*") if f.is_file()) if DOWNLOAD_DIR.exists() else 0,
            "media_count": media_c,
            "disk_free_gb": round(free, 2) if free else None,
            "disk_total_gb": round(total_d, 2) if total_d else None,
            "disk_used_gb": round(used, 2) if used else None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
