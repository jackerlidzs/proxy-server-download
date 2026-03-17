"""
Extract Service - archive extraction and compression
Supports: .rar (multi-part), .zip, .7z, .tar.gz
With progress tracking, cancel, ETA, and speed
"""
import re
import uuid
import asyncio
import time
from pathlib import Path
from datetime import datetime

from config import DOWNLOAD_DIR, MAX_CONCURRENT_EXTRACT

extract_semaphore: asyncio.Semaphore = None
RAR_PATTERN = re.compile(r'^(.+?)\.part(\d+)\.rar$', re.IGNORECASE)

# Task tracker and subprocess refs for cancel
extract_tasks: dict = {}
_extract_procs: dict = {}  # eid -> subprocess for cancel


def init_extract_semaphore():
    global extract_semaphore
    extract_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACT)


def part_group(fn: str):
    m = RAR_PATTERN.match(fn)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def human_size(b):
    if not b:
        return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def fmt_time(secs):
    """Format seconds into human-readable string."""
    if secs < 0 or secs > 86400:
        return "--:--"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _calc_eta(task):
    """Calculate speed and ETA based on progress and elapsed time."""
    pct = task.get("percent", 0)
    started = task.get("_started_ts", 0)
    if not started or pct <= 0:
        return
    elapsed = time.time() - started
    task["elapsed"] = fmt_time(elapsed)
    if pct > 0:
        eta_secs = (elapsed / pct) * (100 - pct)
        task["eta"] = fmt_time(eta_secs)
    total_bytes = task.get("total_size", 0)
    if total_bytes > 0 and elapsed > 0:
        processed = total_bytes * (pct / 100)
        speed = processed / elapsed
        task["speed"] = human_size(speed) + "/s"


def check_parts(filename: str, directory: Path) -> dict:
    """Check if all parts of a multi-part RAR are present."""
    group, part = part_group(filename)
    if not group:
        return {"is_multipart": False, "complete": True, "parts": [], "missing": []}

    existing = []
    for f in directory.iterdir():
        if f.is_file():
            g, p = part_group(f.name)
            if g == group:
                existing.append(p)

    existing.sort()
    if not existing:
        return {"is_multipart": True, "complete": False, "parts": [], "missing": []}

    max_part = max(existing)
    expected = list(range(1, max_part + 1))
    missing = [p for p in expected if p not in existing]

    return {
        "is_multipart": True,
        "complete": len(missing) == 0,
        "total_parts": max_part,
        "found_parts": sorted(existing),
        "missing_parts": missing,
        "group": group,
        "missing_files": [f"{group}.part{p}.rar" for p in missing]
    }


def cancel_extract(eid: str):
    """Cancel extraction by killing subprocess."""
    if eid in extract_tasks:
        extract_tasks[eid].update({"status": "cancelled", "speed": "", "eta": ""})
    proc = _extract_procs.pop(eid, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass


def _get_archive_size(fp: Path, group: str) -> int:
    """Get total archive size (sum of all parts for multipart)."""
    if group:
        total = 0
        for f in fp.parent.iterdir():
            g, _ = part_group(f.name)
            if g == group and f.name.lower().endswith('.rar'):
                total += f.stat().st_size
        return total
    return fp.stat().st_size if fp.exists() else 0


async def extract_archive(filename: str, delete_after: bool = False,
                           base_dir: Path = None, destination: str = None) -> dict:
    """Extract an archive file. Extracts to subfolder by default."""
    base_dir = base_dir or DOWNLOAD_DIR
    fp = base_dir / filename

    if not fp.exists():
        return {"success": False, "error": f"File not found: {filename}"}

    ext = fp.suffix.lower()
    name_lower = fp.name.lower()
    eid = f"ext_{uuid.uuid4().hex[:8]}"

    # For multi-part RAR, check all parts first
    group = ""
    if ext == ".rar":
        parts_info = check_parts(fp.name, fp.parent)
        if parts_info["is_multipart"]:
            if not parts_info["complete"]:
                return {
                    "success": False,
                    "error": f"Missing parts: {', '.join(parts_info['missing_files'])}",
                    "missing_files": parts_info["missing_files"],
                    "found_parts": parts_info["found_parts"],
                    "total_parts": parts_info.get("total_parts", 0)
                }
            group = parts_info.get("group", "")
            g, p = part_group(fp.name)
            if g and p != 1:
                part1 = fp.parent / f"{g}.part1.rar"
                if part1.exists():
                    fp = part1
                    filename = str(part1.relative_to(base_dir))

    # Determine output directory
    if destination:
        out_dir = base_dir / destination
    else:
        stem = fp.stem
        if stem.lower().endswith('.tar'):
            stem = stem[:-4]
        if group:
            stem = group
        part_m = re.match(r'^(.+?)\.part\d+$', stem, re.IGNORECASE)
        if part_m:
            stem = part_m.group(1)
        out_dir = fp.parent / stem
        if out_dir.exists() and out_dir.is_file():
            out_dir = fp.parent / f"{stem}_extracted"

    out_dir.mkdir(parents=True, exist_ok=True)

    # Calculate total archive size for ETA
    total_size = _get_archive_size(fp, group)

    extract_tasks[eid] = {
        "task_id": eid, "status": "extracting", "filename": fp.name,
        "group": group, "progress": "Starting...", "percent": 0,
        "destination": str(out_dir.relative_to(base_dir)),
        "total_size": total_size,
        "speed": "", "eta": "", "elapsed": "",
        "_started_ts": time.time(),
        "created_at": datetime.now().isoformat()
    }

    asyncio.create_task(_run_extract(fp, ext, name_lower, out_dir, eid, group, delete_after))

    return {"success": True, "task_id": eid, "message": f"Extracting to {out_dir.name}/",
            "destination": str(out_dir.relative_to(base_dir))}


async def _run_extract(fp: Path, ext: str, name_lower: str, base_dir: Path,
                       eid: str, group: str, delete_after: bool):
    """Background extraction task."""
    async with extract_semaphore:
        try:
            if ext == ".rar" or name_lower.endswith(".rar"):
                result = await _extract_rar(fp, base_dir, eid)
            elif ext == ".zip":
                result = await _extract_zip(fp, base_dir, eid)
            elif ext == ".7z":
                result = await _extract_7z(fp, base_dir, eid)
            elif name_lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
                result = await _extract_tar(fp, base_dir, eid)
            elif ext == ".tar":
                result = await _extract_tar(fp, base_dir, eid)
            elif ext == ".gz" and not name_lower.endswith(".tar.gz"):
                result = await _extract_gz(fp, base_dir, eid)
            elif ext == ".bz2" and not name_lower.endswith(".tar.bz2"):
                result = await _extract_bz2(fp, base_dir, eid)
            else:
                extract_tasks[eid].update({"status": "failed", "error": f"Unsupported: {ext}"})
                return

            if result and delete_after:
                extract_tasks[eid]["progress"] = "Cleaning up archives..."
                await _cleanup_archives(fp, group, base_dir)

        except Exception as e:
            if extract_tasks.get(eid, {}).get("status") != "cancelled":
                extract_tasks[eid].update({"status": "failed", "error": str(e)})


async def _extract_rar(fp: Path, out_dir: Path, eid: str) -> bool:
    try:
        # Use stdbuf for line-buffered output (real-time progress)
        proc = await asyncio.create_subprocess_exec(
            "stdbuf", "-oL", "unrar", "x", "-o+", "-y", str(fp), str(out_dir) + "/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _extract_procs[eid] = proc
        stderr_lines = []

        async def read_stderr():
            async for line in proc.stderr:
                stderr_lines.append(line.decode("utf-8", errors="ignore").strip())

        stderr_task = asyncio.create_task(read_stderr())

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                m = re.search(r'(\d+)%', dec)
                if m:
                    pct = float(m.group(1))
                    extract_tasks[eid]["percent"] = pct
                    _calc_eta(extract_tasks[eid])
                    eta = extract_tasks[eid].get("eta", "")
                    spd = extract_tasks[eid].get("speed", "")
                    parts = [f"{pct:.0f}%"]
                    if spd:
                        parts.append(spd)
                    if eta:
                        parts.append(f"ETA {eta}")
                    extract_tasks[eid]["progress"] = " · ".join(parts)
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                proc.kill()
                break
        await proc.wait()
        await stderr_task
        _extract_procs.pop(eid, None)

        if extract_tasks.get(eid, {}).get("status") == "cancelled":
            return False

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · Done in {elapsed}" if elapsed else "100%",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            # Extract actual error from stderr
            err_msg = "Extraction failed"
            for line in reversed(stderr_lines):
                if line and not line.startswith("UNRAR") and not line.startswith("Extracting"):
                    err_msg = line[:200]
                    break
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        # stdbuf not available, try without it
        try:
            proc = await asyncio.create_subprocess_exec(
                "unrar", "x", "-o+", "-y", str(fp), str(out_dir) + "/",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _extract_procs[eid] = proc
            stderr_data = []

            async def read_err():
                async for line in proc.stderr:
                    stderr_data.append(line.decode("utf-8", errors="ignore").strip())

            err_t = asyncio.create_task(read_err())
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                dec = line.decode("utf-8", errors="ignore").strip()
                if dec:
                    m = re.search(r'(\d+)%', dec)
                    if m:
                        pct = float(m.group(1))
                        extract_tasks[eid]["percent"] = pct
                        _calc_eta(extract_tasks[eid])
                        eta = extract_tasks[eid].get("eta", "")
                        spd = extract_tasks[eid].get("speed", "")
                        parts_l = [f"{pct:.0f}%"]
                        if spd: parts_l.append(spd)
                        if eta: parts_l.append(f"ETA {eta}")
                        extract_tasks[eid]["progress"] = " · ".join(parts_l)
                if extract_tasks.get(eid, {}).get("status") == "cancelled":
                    proc.kill()
                    break
            await proc.wait()
            await err_t
            _extract_procs.pop(eid, None)
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                return False
            if proc.returncode == 0:
                elapsed = extract_tasks[eid].get("elapsed", "")
                extract_tasks[eid].update({
                    "status": "completed", "percent": 100,
                    "progress": f"100% · Done in {elapsed}" if elapsed else "100%",
                    "speed": "", "eta": "",
                    "completed_at": datetime.now().isoformat()
                })
                return True
            else:
                err_msg = "Extraction failed"
                for line in reversed(stderr_data):
                    if line: err_msg = line[:200]; break
                extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
                return False
        except FileNotFoundError:
            _extract_procs.pop(eid, None)
            extract_tasks[eid].update({"status": "failed", "error": "unrar not found — install it on server"})
            return False


async def _extract_zip(fp: Path, out_dir: Path, eid: str) -> bool:
    try:
        # Count total files for progress
        total_files = 0
        try:
            lp = await asyncio.create_subprocess_exec(
                "unzip", "-l", str(fp),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, _ = await asyncio.wait_for(lp.communicate(), timeout=30)
            for line in out.decode(errors="ignore").split("\n"):
                line = line.strip()
                if line and not line.startswith("---") and not line.startswith("Archive") and not line.startswith("Length"):
                    m = re.match(r'^\s*\d+', line)
                    if m:
                        total_files += 1
        except Exception:
            pass

        proc = await asyncio.create_subprocess_exec(
            "unzip", "-o", str(fp), "-d", str(out_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _extract_procs[eid] = proc
        extracted = 0
        stderr_lines = []

        async def read_stderr():
            async for line in proc.stderr:
                stderr_lines.append(line.decode("utf-8", errors="ignore").strip())

        stderr_task = asyncio.create_task(read_stderr())

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec and ("inflating:" in dec or "extracting:" in dec):
                extracted += 1
                if total_files > 0:
                    pct = min(99, extracted / total_files * 100)
                    extract_tasks[eid]["percent"] = pct
                    _calc_eta(extract_tasks[eid])
                    eta = extract_tasks[eid].get("eta", "")
                    spd = extract_tasks[eid].get("speed", "")
                    parts = [f"{pct:.0f}%", f"{extracted}/{total_files} files"]
                    if spd:
                        parts.append(spd)
                    if eta:
                        parts.append(f"ETA {eta}")
                    extract_tasks[eid]["progress"] = " · ".join(parts)
                else:
                    extract_tasks[eid]["progress"] = f"{extracted} files extracted"
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                proc.kill()
                break
        await proc.wait()
        await stderr_task
        _extract_procs.pop(eid, None)

        if extract_tasks.get(eid, {}).get("status") == "cancelled":
            return False

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · {extracted} files · Done in {elapsed}" if elapsed else f"100% · {extracted} files",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            err_msg = "Extraction failed"
            for line in reversed(stderr_lines):
                if line: err_msg = line[:200]; break
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        _extract_procs.pop(eid, None)
        extract_tasks[eid].update({"status": "failed", "error": "unzip not found — install it on server"})
        return False


async def _extract_7z(fp: Path, out_dir: Path, eid: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "stdbuf", "-oL", "7z", "x", "-y", f"-o{out_dir}", str(fp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _extract_procs[eid] = proc
        stderr_lines = []

        async def read_stderr():
            async for line in proc.stderr:
                stderr_lines.append(line.decode("utf-8", errors="ignore").strip())

        stderr_task = asyncio.create_task(read_stderr())

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                m = re.search(r'(\d+)%', dec)
                if m:
                    pct = float(m.group(1))
                    extract_tasks[eid]["percent"] = pct
                    _calc_eta(extract_tasks[eid])
                    eta = extract_tasks[eid].get("eta", "")
                    spd = extract_tasks[eid].get("speed", "")
                    parts = [f"{pct:.0f}%"]
                    if spd:
                        parts.append(spd)
                    if eta:
                        parts.append(f"ETA {eta}")
                    extract_tasks[eid]["progress"] = " · ".join(parts)
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                proc.kill()
                break
        await proc.wait()
        await stderr_task
        _extract_procs.pop(eid, None)

        if extract_tasks.get(eid, {}).get("status") == "cancelled":
            return False

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · Done in {elapsed}" if elapsed else "100%",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            err_msg = "Extraction failed"
            for line in reversed(stderr_lines):
                if line: err_msg = line[:200]; break
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        # stdbuf not available, try without it
        try:
            proc = await asyncio.create_subprocess_exec(
                "7z", "x", "-y", f"-o{out_dir}", str(fp),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _extract_procs[eid] = proc
            stderr_data = []

            async def read_err():
                async for line in proc.stderr:
                    stderr_data.append(line.decode("utf-8", errors="ignore").strip())

            err_t = asyncio.create_task(read_err())
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                dec = line.decode("utf-8", errors="ignore").strip()
                if dec:
                    m = re.search(r'(\d+)%', dec)
                    if m:
                        pct = float(m.group(1))
                        extract_tasks[eid]["percent"] = pct
                        _calc_eta(extract_tasks[eid])
                        eta = extract_tasks[eid].get("eta", "")
                        spd = extract_tasks[eid].get("speed", "")
                        parts_l = [f"{pct:.0f}%"]
                        if spd: parts_l.append(spd)
                        if eta: parts_l.append(f"ETA {eta}")
                        extract_tasks[eid]["progress"] = " · ".join(parts_l)
                if extract_tasks.get(eid, {}).get("status") == "cancelled":
                    proc.kill()
                    break
            await proc.wait()
            await err_t
            _extract_procs.pop(eid, None)
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                return False
            if proc.returncode == 0:
                elapsed = extract_tasks[eid].get("elapsed", "")
                extract_tasks[eid].update({
                    "status": "completed", "percent": 100,
                    "progress": f"100% · Done in {elapsed}" if elapsed else "100%",
                    "speed": "", "eta": "",
                    "completed_at": datetime.now().isoformat()
                })
                return True
            else:
                err_msg = "Extraction failed"
                for line in reversed(stderr_data):
                    if line: err_msg = line[:200]; break
                extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
                return False
        except FileNotFoundError:
            _extract_procs.pop(eid, None)
            extract_tasks[eid].update({"status": "failed", "error": "7z not found — install p7zip-full on server"})
            return False


async def _extract_gz(fp: Path, out_dir: Path, eid: str) -> bool:
    """Extract a single .gz file using gunzip."""
    try:
        import shutil as shutil_mod
        # Copy .gz to out_dir and decompress there
        dest_gz = out_dir / fp.name
        shutil_mod.copy2(str(fp), str(dest_gz))
        cmd = ["gunzip", "-f", str(dest_gz)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _extract_procs[eid] = proc
        extract_tasks[eid].update({"percent": 50, "progress": "Decompressing..."})
        _, stderr = await proc.communicate()
        _extract_procs.pop(eid, None)

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · Done{' in ' + elapsed if elapsed else ''}",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            err_msg = stderr.decode("utf-8", errors="ignore").strip() if stderr else "Decompression failed"
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        _extract_procs.pop(eid, None)
        extract_tasks[eid].update({"status": "failed", "error": "gunzip not found — install gzip on server"})
        return False


async def _extract_bz2(fp: Path, out_dir: Path, eid: str) -> bool:
    """Extract a single .bz2 file using bunzip2."""
    try:
        import shutil as shutil_mod
        dest_bz = out_dir / fp.name
        shutil_mod.copy2(str(fp), str(dest_bz))
        cmd = ["bunzip2", "-f", str(dest_bz)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _extract_procs[eid] = proc
        extract_tasks[eid].update({"percent": 50, "progress": "Decompressing..."})
        _, stderr = await proc.communicate()
        _extract_procs.pop(eid, None)

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · Done{' in ' + elapsed if elapsed else ''}",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            err_msg = stderr.decode("utf-8", errors="ignore").strip() if stderr else "Decompression failed"
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        _extract_procs.pop(eid, None)
        extract_tasks[eid].update({"status": "failed", "error": "bunzip2 not found — install bzip2 on server"})
        return False


async def _extract_tar(fp: Path, out_dir: Path, eid: str) -> bool:
    try:
        # Use -v to track extracted files
        cmd = ["tar", "-xvf", str(fp), "-C", str(out_dir)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        _extract_procs[eid] = proc
        total_size = fp.stat().st_size if fp.exists() else 0
        extracted_files = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                extracted_files += 1
                # Estimate progress based on output dir size vs archive size
                if total_size > 0 and extracted_files % 10 == 0:
                    try:
                        out_size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
                        pct = min(95, out_size / total_size * 100)
                        extract_tasks[eid]["percent"] = pct
                        _calc_eta(extract_tasks[eid])
                    except Exception:
                        pass
                eta = extract_tasks[eid].get("eta", "")
                extract_tasks[eid]["progress"] = f"{extracted_files} files" + (f" · ETA {eta}" if eta else "")
            if extract_tasks.get(eid, {}).get("status") == "cancelled":
                proc.kill()
                break
        await proc.wait()
        _extract_procs.pop(eid, None)

        if extract_tasks.get(eid, {}).get("status") == "cancelled":
            return False

        if proc.returncode == 0:
            elapsed = extract_tasks[eid].get("elapsed", "")
            extract_tasks[eid].update({
                "status": "completed", "percent": 100,
                "progress": f"100% · {extracted_files} files · Done in {elapsed}" if elapsed else f"100% · {extracted_files} files",
                "speed": "", "eta": "",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            extract_tasks[eid].update({"status": "failed", "error": "Extraction failed", "speed": "", "eta": ""})
            return False
    except FileNotFoundError:
        _extract_procs.pop(eid, None)
        extract_tasks[eid].update({"status": "failed", "error": "tar not found"})
        return False


async def _cleanup_archives(fp: Path, group: str, base_dir: Path):
    """Delete archive files after successful extraction."""
    if group:
        for f in list(base_dir.iterdir()):
            g, _ = part_group(f.name)
            if g == group and f.name.lower().endswith('.rar'):
                f.unlink(missing_ok=True)
    else:
        fp.unlink(missing_ok=True)


async def compress_files(filenames: list[str], archive_name: str, fmt: str,
                          downloads_dict: dict, base_dir: Path = None) -> dict:
    """Compress files into an archive."""
    base_dir = base_dir or DOWNLOAD_DIR
    cid = f"cmp_{uuid.uuid4().hex[:8]}"

    downloads_dict[cid] = {
        "task_id": cid, "status": "compressing", "filename": archive_name,
        "progress": "Compressing...", "percent": 0,
        "created_at": datetime.now().isoformat()
    }

    try:
        out_path = base_dir / archive_name
        files = [str(base_dir / fn) for fn in filenames if (base_dir / fn).exists()]

        if not files:
            downloads_dict[cid].update({"status": "failed", "error": "No valid files"})
            return {"success": False, "error": "No valid files", "task_id": cid}

        if fmt == "zip":
            cmd = ["zip", "-r", str(out_path)] + files
        elif fmt in ("tar.gz", "tgz"):
            if not archive_name.endswith((".tar.gz", ".tgz")):
                archive_name += ".tar.gz"
                out_path = base_dir / archive_name
            cmd = ["tar", "-czf", str(out_path)] + [f"-C{base_dir}"] + filenames
        elif fmt == "gzip":
            # gzip single file — copies first then compresses
            if len(files) != 1:
                downloads_dict[cid].update({"status": "failed", "error": "Gzip only supports single file"})
                return {"success": False, "error": "Gzip only supports single file", "task_id": cid}
            import shutil as shutil_mod
            gz_src = out_path.parent / Path(files[0]).name
            if str(gz_src) != files[0]:
                shutil_mod.copy2(files[0], str(gz_src))
            cmd = ["gzip", "-f", str(gz_src)]
        elif fmt == "bzip2":
            if len(files) != 1:
                downloads_dict[cid].update({"status": "failed", "error": "Bzip2 only supports single file"})
                return {"success": False, "error": "Bzip2 only supports single file", "task_id": cid}
            import shutil as shutil_mod
            bz_src = out_path.parent / Path(files[0]).name
            if str(bz_src) != files[0]:
                shutil_mod.copy2(files[0], str(bz_src))
            cmd = ["bzip2", "-f", str(bz_src)]
        elif fmt in ("tar.bz2", "tbz2"):
            if not archive_name.endswith((".tar.bz2", ".tbz2")):
                archive_name += ".tar.bz2"
                out_path = base_dir / archive_name
            cmd = ["tar", "-cjf", str(out_path)] + [f"-C{base_dir}"] + filenames
        else:
            downloads_dict[cid].update({"status": "failed", "error": f"Unsupported format: {fmt}"})
            return {"success": False, "error": f"Unsupported format: {fmt}", "task_id": cid}

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        downloads_dict[cid].update({"percent": 50})
        await proc.communicate()

        if proc.returncode == 0 and out_path.exists():
            downloads_dict[cid].update({
                "status": "completed", "percent": 100, "progress": "100%",
                "file_size": out_path.stat().st_size,
                "completed_at": datetime.now().isoformat()
            })
            return {"success": True, "task_id": cid, "filename": archive_name}
        else:
            downloads_dict[cid].update({"status": "failed", "error": "Compression failed"})
            return {"success": False, "error": "Compression failed", "task_id": cid}

    except Exception as e:
        downloads_dict[cid].update({"status": "failed", "error": str(e)})
        return {"success": False, "error": str(e), "task_id": cid}
