"""
Download Service - curl-impersonate & aria2c engines
With cancel (kill subprocess) and resume support
"""
import re
import uuid
import shlex
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, unquote

from config import DOWNLOAD_DIR, MAX_CONNECTIONS, MAX_CONCURRENT, SERVER_URL

downloads: dict = {}
_processes: dict = {}  # tid -> subprocess for cancel/kill
semaphore: asyncio.Semaphore = None


def init_semaphore():
    global semaphore
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)


def human_size(b):
    if not b: return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def human_speed(bps):
    return human_size(bps) + "/s" if bps else ""


def sanitize(fn):
    fn = fn.strip().rstrip("^").strip()
    fn = fn.replace("/", "_").replace("\\", "_").replace("..", "_")
    fn = "".join(c for c in fn if c.isprintable() and c not in '<>:"|?*')
    return fn or f"download_{uuid.uuid4().hex[:8]}"


def filename_from_url(url):
    fn = unquote(urlparse(url).path.split("/")[-1])
    return fn if fn and fn != "/" else f"download_{uuid.uuid4().hex[:8]}"


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
        elif p in ("-L", "-k", "-s", "-S", "-v", "--location", "--insecure",
                    "--silent", "--show-error", "--verbose", "--compressed"):
            pass
        elif not p.startswith("-") and not url:
            url = p.strip("'\"")
        i += 1
    return url, headers


def cancel_download(tid: str):
    """Cancel a download by killing its subprocess."""
    if tid in downloads:
        downloads[tid]["status"] = "cancelled"
        downloads[tid]["speed"] = ""
    proc = _processes.pop(tid, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass


async def monitor_progress(tid, fp, total=0):
    t0 = asyncio.get_event_loop().time()
    last_sz, last_t = 0, t0
    while tid in downloads and downloads[tid]["status"] == "downloading":
        try:
            if fp.exists():
                sz = fp.stat().st_size
                now = asyncio.get_event_loop().time()
                dt = now - last_t
                if dt > 0:
                    spd = (sz - last_sz) / dt
                    downloads[tid]["speed"] = human_speed(spd) if spd > 0 else ""
                    last_sz, last_t = sz, now
                downloads[tid]["downloaded"] = sz
                if total > 0:
                    pct = min(99.9, sz / total * 100)
                    downloads[tid].update({"percent": round(pct, 1), "progress": f"{pct:.1f}%", "total_size": total})
                else:
                    downloads[tid]["progress"] = human_size(sz)
        except Exception:
            pass
        await asyncio.sleep(1)


async def dl_curl(tid, url, headers, filename, resume=False):
    fp = DOWNLOAD_DIR / filename
    total = 0

    # Get content-length and content-type for progress + validation
    ct_type = ""
    try:
        hcmd = ["curl_chrome", "-L", "-s", "-S", "-I", "--max-redirs", "10", "--connect-timeout", "15"]
        for k, v in headers.items():
            hcmd.extend(["-H", f"{k}: {v}"])
        hcmd.append(url)
        p = await asyncio.create_subprocess_exec(*hcmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=20)
        for line in out.decode(errors="ignore").split("\n"):
            ll = line.lower().strip()
            if "content-length:" in ll:
                try:
                    total = int(ll.split(":", 1)[1].strip())
                except:
                    pass
            if "content-type:" in ll:
                ct_type = ll.split(":", 1)[1].strip()
    except:
        pass

    cmd = ["curl_chrome", "-L", "-S", "--max-redirs", "10", "--retry", "3", "--retry-delay", "3",
           "--connect-timeout", "30", "--max-time", "7200", "-o", str(fp)]
    # Resume support
    if resume and fp.exists():
        cmd.extend(["-C", "-"])
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)

    mon = asyncio.create_task(monitor_progress(tid, fp, total))
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _processes[tid] = proc
        _, stderr = await proc.communicate()
        _processes.pop(tid, None)

        # Check if cancelled during download
        if downloads.get(tid, {}).get("status") == "cancelled":
            return False, "Cancelled"

        if proc.returncode == 0 and fp.exists():
            sz = fp.stat().st_size
            # Check if we got an HTML page instead of the actual file
            is_html = False
            if sz < 100_000:  # Check files under 100KB for HTML content
                try:
                    c = fp.read_text(errors="ignore")[:2000]
                    cl = c.lower()
                    if any(tag in cl for tag in ["<html", "<!doctype", "<head>", "<body", "403 forbidden", "404 not found", "access denied"]):
                        is_html = True
                except:
                    pass
            # Also check if content-type from HEAD was text/html
            if ct_type and "text/html" in ct_type:
                is_html = True
            if is_html:
                fp.unlink(missing_ok=True)
                return False, f"Got HTML page instead of file ({human_size(sz)}). The URL may require a browser to download. Try copying the direct download link."
            downloads[tid].update({
                "status": "completed", "file_size": sz, "downloaded": sz, "percent": 100.0,
                "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": ""
            })
            return True, None
        if proc.returncode == -9 or downloads.get(tid, {}).get("status") == "cancelled":
            return False, "Cancelled"
        return False, stderr.decode(errors="ignore").strip() or f"curl exit {proc.returncode}"
    except FileNotFoundError:
        _processes.pop(tid, None)
        return False, "curl_chrome not found"
    except Exception as e:
        _processes.pop(tid, None)
        return False, str(e)
    finally:
        mon.cancel()
        try:
            await mon
        except asyncio.CancelledError:
            pass


async def dl_aria2c(tid, url, headers, filename, conns, resume=False):
    fp = DOWNLOAD_DIR / filename
    cmd = ["aria2c", "-x", str(conns), "-s", str(conns), "-k", "1M", "-m", "5", "--retry-wait=3",
           "-t", "60", "--connect-timeout=30", "-c", "--auto-file-renaming=false", "--allow-overwrite=true",
           "-d", str(DOWNLOAD_DIR), "-o", filename, "--summary-interval=1", "--file-allocation=none"]
    for k, v in headers.items():
        cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        _processes[tid] = proc
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode(errors="ignore").strip()
            if dec:
                lines.append(dec)
                m = re.search(r'(\d+)%', dec)
                sm = re.search(r'DL:(\S+)', dec)
                if m:
                    downloads[tid].update({"percent": float(m.group(1)), "progress": f"{m.group(1)}%"})
                if sm:
                    downloads[tid]["speed"] = sm.group(1) + "/s"
            # Check cancel
            if downloads.get(tid, {}).get("status") == "cancelled":
                proc.kill()
                break
        await proc.wait()
        _processes.pop(tid, None)

        if downloads.get(tid, {}).get("status") == "cancelled":
            return False, "Cancelled"

        if proc.returncode == 0 and fp.exists():
            sz = fp.stat().st_size
            # Check if we got HTML instead of actual file
            if sz < 100_000:
                try:
                    c = fp.read_text(errors="ignore")[:2000]
                    cl = c.lower()
                    if any(tag in cl for tag in ["<html", "<!doctype", "<head>", "<body", "403 forbidden", "404 not found", "access denied"]):
                        fp.unlink(missing_ok=True)
                        return False, f"Got HTML page instead of file ({human_size(sz)}). The URL may require a browser to download."
                except:
                    pass
            downloads[tid].update({
                "status": "completed", "file_size": sz, "downloaded": sz, "percent": 100.0,
                "download_url": f"{SERVER_URL}/files/{filename}",
                "completed_at": datetime.now().isoformat(), "progress": "100%", "speed": ""
            })
            return True, None
        return False, " | ".join(lines[-3:]) or "Unknown error"
    except FileNotFoundError:
        _processes.pop(tid, None)
        return False, "aria2c not found"
    except Exception as e:
        _processes.pop(tid, None)
        return False, str(e)


async def run_download(tid, url, headers, filename, conns, engine="auto", resume=False):
    async with semaphore:
        filename = sanitize(filename)
        downloads[tid].update({"status": "downloading", "filename": filename, "percent": 0, "speed": "", "downloaded": 0})
        ok, err = False, None
        if engine == "aria2c":
            downloads[tid]["engine"] = "aria2c"
            ok, err = await dl_aria2c(tid, url, headers, filename, conns, resume)
        elif engine == "curl":
            downloads[tid]["engine"] = "curl_chrome"
            ok, err = await dl_curl(tid, url, headers, filename, resume)
        else:
            downloads[tid]["engine"] = "curl_chrome"
            ok, err = await dl_curl(tid, url, headers, filename, resume)
            if not ok and err and "not found" in err.lower():
                downloads[tid]["engine"] = "aria2c"
                downloads[tid]["percent"] = 0
                ok, err = await dl_aria2c(tid, url, headers, filename, conns, resume)

        if not ok:
            if downloads.get(tid, {}).get("status") != "cancelled":
                downloads[tid].update({"status": "failed", "error": err or "Unknown", "speed": ""})
        else:
            # Index file metadata (NO auto-extract)
            from services.file_service import index_file
            await index_file(DOWNLOAD_DIR / filename)
