"""
Extract Service - archive extraction and compression
Supports: .rar (multi-part), .zip, .7z, .tar.gz, .gz, .bz2
All extraction via 7z (p7zip-full) with realtime progress tracking
"""
import re
import uuid
import time
import json
import shutil
import asyncio
from pathlib import Path
from datetime import datetime

from config import DOWNLOAD_DIR, MAX_CONCURRENT_EXTRACT

extract_semaphore: asyncio.Semaphore = None
RAR_PATTERN = re.compile(r'^(.+?)\.part(\d+)\.rar$', re.IGNORECASE)
# Old RAR: name.rar, name.r00, name.r01, ...
OLD_RAR_PATTERN = re.compile(r'^(.+?)\.r(\d{2,})$', re.IGNORECASE)
# Split formats: name.zip.001, name.7z.001, etc.
SPLIT_PATTERN = re.compile(r'^(.+?\.(zip|7z))\.(\d{3,})$', re.IGNORECASE)

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
    """Check if all parts of a multi-part archive are present.
    Supports: .partN.rar, old RAR (.rar/.r00/.r01), split (.zip.001/.7z.001)
    Also returns disk space info for the frontend.
    """
    # --- New .partN.rar format ---
    group, part = part_group(filename)
    if group:
        result = _check_rar_parts(group, directory)
    elif SPLIT_PATTERN.match(filename):
        base_archive = SPLIT_PATTERN.match(filename).group(1)
        result = _check_split_parts(base_archive, directory)
    else:
        fn_lower = filename.lower()
        m_old = OLD_RAR_PATTERN.match(filename)
        if m_old or fn_lower.endswith('.rar'):
            base_name = m_old.group(1) if m_old else filename[:-4]
            result = _check_old_rar_parts(base_name, directory)
        else:
            result = {"is_multipart": False, "complete": True, "parts": [], "missing": []}

    # Add disk space info
    try:
        fp = directory / filename
        archive_size = fp.stat().st_size if fp.exists() else 0
        usage = shutil.disk_usage(str(directory))
        result["disk_free_gb"] = round(usage.free / 1e9, 1)
        result["disk_required_gb"] = round(archive_size * 1.1 / 1e9, 1)
        result["disk_enough"] = usage.free > archive_size * 1.1
    except Exception:
        result["disk_enough"] = True  # don't block on failure

    return result


def _check_rar_parts(group: str, directory: Path) -> dict:
    """Check .partN.rar multipart."""
    existing = []
    zero_byte_parts = []
    for f in directory.iterdir():
        if f.is_file():
            g, p = part_group(f.name)
            if g == group:
                existing.append(p)
                if f.stat().st_size == 0:
                    zero_byte_parts.append(f.name)

    existing.sort()
    if not existing:
        return {"is_multipart": True, "complete": False, "parts": [], "missing": []}

    max_part = max(existing)
    expected = list(range(1, max_part + 1))
    missing = [p for p in expected if p not in existing]

    result = {
        "is_multipart": True,
        "complete": len(missing) == 0 and len(zero_byte_parts) == 0,
        "total_parts": max_part,
        "found_parts": sorted(existing),
        "missing_parts": missing,
        "group": group,
        "missing_files": [f"{group}.part{p}.rar" for p in missing],
        "format": "partN.rar"
    }
    if zero_byte_parts:
        result["complete"] = False
        result["error"] = f"Empty (0 byte) parts: {', '.join(zero_byte_parts)}"
        result["zero_byte_parts"] = zero_byte_parts
    if 1 in missing:
        result["error"] = f"Missing first part: {group}.part1.rar — cannot extract without part1"
    return result


def _check_old_rar_parts(base_name: str, directory: Path) -> dict:
    """Check old RAR format: name.rar + name.r00 + name.r01 + ..."""
    main_rar = None
    volumes = []
    zero_byte = []
    for f in directory.iterdir():
        if not f.is_file():
            continue
        fn = f.name
        fn_lower = fn.lower()
        base_lower = base_name.lower()
        # Main file: base.rar
        if fn_lower == base_lower + '.rar':
            main_rar = fn
            if f.stat().st_size == 0:
                zero_byte.append(fn)
        # Volumes: base.r00, base.r01, ...
        m = OLD_RAR_PATTERN.match(fn)
        if m and m.group(1).lower() == base_lower:
            volumes.append((int(m.group(2)), fn))
            if f.stat().st_size == 0:
                zero_byte.append(fn)

    if not volumes:
        # Single .rar, not multipart
        return {"is_multipart": False, "complete": True, "parts": [], "missing": []}

    # Old format IS multipart
    volumes.sort()
    max_vol = max(v[0] for v in volumes)
    existing_nums = {v[0] for v in volumes}
    missing_vols = []
    for i in range(0, max_vol + 1):
        if i not in existing_nums:
            missing_vols.append(f"{base_name}.r{i:02d}")

    missing_files = list(missing_vols)
    if not main_rar:
        missing_files.insert(0, f"{base_name}.rar")

    result = {
        "is_multipart": True,
        "complete": len(missing_files) == 0 and len(zero_byte) == 0,
        "total_parts": max_vol + 2,  # volumes + main .rar
        "found_parts": [0] + [v[0] + 1 for v in volumes] if main_rar else [v[0] + 1 for v in volumes],
        "missing_parts": [],
        "group": base_name,
        "missing_files": missing_files,
        "format": "old_rar",
        "main_rar": main_rar
    }
    if zero_byte:
        result["complete"] = False
        result["error"] = f"Empty (0 byte) parts: {', '.join(zero_byte)}"
    if not main_rar:
        result["error"] = f"Missing main archive: {base_name}.rar"
    return result


def _check_split_parts(base_archive: str, directory: Path) -> dict:
    """Check split format: name.zip.001, name.zip.002, ..."""
    existing = []
    zero_byte = []
    base_lower = base_archive.lower()
    for f in directory.iterdir():
        if not f.is_file():
            continue
        m = SPLIT_PATTERN.match(f.name)
        if m and m.group(1).lower() == base_lower:
            num = int(m.group(3))
            existing.append(num)
            if f.stat().st_size == 0:
                zero_byte.append(f.name)

    if not existing:
        return {"is_multipart": False, "complete": True, "parts": [], "missing": []}

    existing.sort()
    max_part = max(existing)
    expected = list(range(1, max_part + 1))
    missing = [p for p in expected if p not in existing]

    result = {
        "is_multipart": True,
        "complete": len(missing) == 0 and len(zero_byte) == 0,
        "total_parts": max_part,
        "found_parts": sorted(existing),
        "missing_parts": missing,
        "group": base_archive,
        "missing_files": [f"{base_archive}.{p:03d}" for p in missing],
        "format": "split"
    }
    if zero_byte:
        result["complete"] = False
        result["error"] = f"Empty (0 byte) parts: {', '.join(zero_byte)}"
    if 1 in missing:
        result["error"] = f"Missing first part: {base_archive}.001 — cannot extract"
    return result


def cancel_extract(eid: str):
    """Cancel extraction by killing subprocess."""
    task = extract_tasks.get(eid)
    if not task:
        return False
    # Guard: do not cancel if already completed
    if task.get("status") in ("completed", "failed"):
        return False
    task.update({"status": "cancelled", "speed": "", "eta": ""})
    proc = _extract_procs.pop(eid, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
    return True


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


def _schedule_job_cleanup(eid: str, delay: int = 3600):
    """Auto-delete finished job from extract_tasks after delay seconds."""
    try:
        loop = asyncio.get_event_loop()
        loop.call_later(delay, lambda: extract_tasks.pop(eid, None))
    except Exception:
        pass


async def stream_job(eid: str):
    """Async generator yielding SSE events for an extract job."""
    while True:
        job = extract_tasks.get(eid)
        if not job:
            yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
            break

        # Filter out internal fields
        event = {k: v for k, v in job.items() if not k.startswith('_')}
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        if job.get("status") in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)


async def extract_archive(filename: str, delete_after: bool = False,
                           base_dir: Path = None, destination: str = None,
                           password: str = None) -> dict:
    """Extract an archive file. Extracts to subfolder by default."""
    base_dir = base_dir or DOWNLOAD_DIR
    fp = base_dir / filename

    if not fp.exists():
        return {"success": False, "error": f"File not found: {filename}"}

    ext = fp.suffix.lower()
    name_lower = fp.name.lower()
    eid = f"ext_{uuid.uuid4().hex[:8]}"

    # Check multi-part for all formats
    group = ""
    archive_format = ""
    parts_info = check_parts(fp.name, fp.parent)

    if parts_info["is_multipart"]:
        if not parts_info["complete"]:
            err = parts_info.get("error", f"Missing parts: {', '.join(parts_info['missing_files'])}")
            return {
                "success": False,
                "error": err,
                "missing_files": parts_info.get("missing_files", []),
                "found_parts": parts_info.get("found_parts", []),
                "total_parts": parts_info.get("total_parts", 0),
                "zero_byte_parts": parts_info.get("zero_byte_parts", [])
            }
        group = parts_info.get("group", "")
        archive_format = parts_info.get("format", "")

        # Redirect to correct first file
        if archive_format == "partN.rar":
            g, p = part_group(fp.name)
            if g and p != 1:
                part1 = fp.parent / f"{g}.part1.rar"
                if part1.exists():
                    fp = part1
                    filename = str(part1.relative_to(base_dir))
        elif archive_format == "old_rar":
            # Use main .rar file for old format
            main_rar = parts_info.get("main_rar")
            if main_rar:
                fp = fp.parent / main_rar
                filename = str(fp.relative_to(base_dir))
                ext = ".rar"
                name_lower = fp.name.lower()
        elif archive_format == "split":
            # Use .001 first file
            first_file = fp.parent / f"{group}.001"
            if first_file.exists():
                fp = first_file
                filename = str(fp.relative_to(base_dir))
                ext = fp.suffix.lower()
                name_lower = fp.name.lower()

    # Determine output directory (default = same dir as archive)
    if destination:
        out_dir = base_dir / destination
    else:
        stem = fp.stem
        if stem.lower().endswith('.tar'):
            stem = stem[:-4]
        if group:
            stem = group
            # Strip extension from split format groups (file.zip → file)
            if archive_format == "split":
                stem = Path(group).stem
        part_m = re.match(r'^(.+?)\.part\d+$', stem, re.IGNORECASE)
        if part_m:
            stem = part_m.group(1)
        out_dir = fp.parent / stem
        if out_dir.exists() and out_dir.is_file():
            out_dir = fp.parent / f"{stem}_extracted"

    out_dir.mkdir(parents=True, exist_ok=True)

    # Calculate total archive size for ETA
    total_size = _get_archive_size(fp, group)

    # --- Disk space pre-check ---
    try:
        disk = shutil.disk_usage(str(base_dir))
        free_space = disk.free
        estimated_size = int(total_size * 1.1) if total_size > 0 else 0
        recoverable = total_size if delete_after else 0
        needed = estimated_size - recoverable
        if needed > 0 and needed > free_space:
            return {
                "success": False,
                "error": f"Not enough disk space. Need ~{human_size(needed)}, only {human_size(free_space)} free. "
                         f"Archive size: {human_size(total_size)}. "
                         f"Consider enabling 'Delete after extract' or freeing up space.",
                "disk_free": free_space,
                "estimated_size": estimated_size
            }
    except Exception:
        pass  # Don't block extraction if disk check fails

    extract_tasks[eid] = {
        "task_id": eid, "status": "extracting", "filename": fp.name,
        "group": group, "progress": "Starting...", "percent": 0,
        "destination": str(out_dir.relative_to(base_dir)),
        "total_size": total_size,
        "speed": "", "eta": "", "elapsed": "", "current_file": "",
        "_started_ts": time.time(),
        "created_at": datetime.now().isoformat()
    }

    asyncio.create_task(_run_extract(fp, ext, name_lower, out_dir, eid, group,
                                      delete_after, password, archive_format))

    return {"success": True, "task_id": eid, "message": f"Extracting to {out_dir.name}/",
            "destination": str(out_dir.relative_to(base_dir))}


async def _run_extract(fp: Path, ext: str, name_lower: str, base_dir: Path,
                       eid: str, group: str, delete_after: bool,
                       password: str = None, archive_format: str = ""):
    """Background extraction task — complete routing table."""
    async with extract_semaphore:
        try:
            name = fp.name.lower()
            exts = ''.join(fp.suffixes).lower()  # e.g. ".tar.gz"

            # ── RAR → 7z (best support for modern/multipart RAR) ────
            if (ext == '.rar'
                    or re.search(r'\.part\d+\.rar$', name)
                    or re.search(r'\.r\d{2,3}$', name)):
                result = await _extract_7z(fp, base_dir, eid, password)

            # ── TAR variants → tar CLI (preserves permissions/symlinks) ──
            elif (exts in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst')
                  or ext in ('.tgz', '.tbz2', '.txz', '.tar')):
                result = await _extract_tar(fp, base_dir, eid)

            # ── Standalone compressed → native tools ────────────────
            elif ext == '.gz' and '.tar' not in name:
                result = await _extract_gz(fp, base_dir, eid)
            elif ext == '.bz2' and '.tar' not in name:
                result = await _extract_bz2(fp, base_dir, eid)
            elif ext == '.xz' and '.tar' not in name:
                result = await _extract_xz(fp, base_dir, eid)

            # ── ZIP / 7z → 7z (most reliable for these) ────────────
            elif ext in ('.zip', '.7z'):
                result = await _extract_7z(fp, base_dir, eid, password)

            # ── Generic split (.001 .002 ...) → 7z with fallback ───
            elif re.search(r'\.\d{3}$', name):
                result = await _extract_split_safe(fp, base_dir, eid, password)

            # ── Unsupported ─────────────────────────────────────────
            else:
                extract_tasks[eid].update({"status": "failed", "error": f"Unsupported format: {ext}"})
                result = False

            if result and delete_after:
                extract_tasks[eid]["progress"] = "Cleaning up archives..."
                await _cleanup_archives(fp, group, base_dir)

        except Exception as e:
            if extract_tasks.get(eid, {}).get("status") != "cancelled":
                extract_tasks[eid].update({"status": "failed", "error": str(e)})
        finally:
            # Schedule auto-cleanup of finished job after 1 hour
            _schedule_job_cleanup(eid, 3600)


async def _extract_7z(fp: Path, out_dir: Path, eid: str, password: str = None) -> bool:
    """Unified extraction via 7z — handles .rar, .zip, .7z, split archives.
    7z auto-detects format and joins multipart archives from part1.
    """
    try:
        cmd = ["stdbuf", "-oL", "7z", "x", "-y", f"-o{out_dir}"]
        if password:
            cmd.append(f"-p{password}")
        cmd.append(str(fp))

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
            if not dec:
                continue

            # Parse 7z output — try patterns in order
            # Pattern 1: "45% - filename.mkv"
            m = re.search(r'(\d+)%\s*-\s*(.+)', dec)
            if m:
                pct = float(m.group(1))
                current_file = m.group(2).strip()
                extract_tasks[eid]["percent"] = pct
                extract_tasks[eid]["current_file"] = current_file
                _calc_eta(extract_tasks[eid])
                eta = extract_tasks[eid].get("eta", "")
                spd = extract_tasks[eid].get("speed", "")
                parts = [f"{pct:.0f}%"]
                if spd:
                    parts.append(spd)
                if eta:
                    parts.append(f"ETA {eta}")
                extract_tasks[eid]["progress"] = " · ".join(parts)
                continue

            # Pattern 2: "45%" (percent only)
            m2 = re.search(r'(\d+)%', dec)
            if m2:
                pct = float(m2.group(1))
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
                continue

            # Pattern 3: "Everything is Ok" → done
            if "everything is ok" in dec.lower():
                extract_tasks[eid]["percent"] = 100
                continue

            # Skip unrecognized lines

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
            # Check for known error patterns in stderr
            err_msg = "Extraction failed"
            for line in reversed(stderr_lines):
                if not line:
                    continue
                ll = line.lower()
                if "wrong password" in ll or "password" in ll:
                    err_msg = "Wrong password or archive is password-protected"
                    break
                if "error" in ll:
                    err_msg = line[:200]
                    break
                if line:
                    err_msg = line[:200]
                    break
            extract_tasks[eid].update({"status": "failed", "error": err_msg, "speed": "", "eta": ""})
            return False

    except FileNotFoundError:
        # stdbuf not available, try without it
        try:
            cmd2 = ["7z", "x", "-y", f"-o{out_dir}"]
            if password:
                cmd2.append(f"-p{password}")
            cmd2.append(str(fp))

            proc = await asyncio.create_subprocess_exec(
                *cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
                    m = re.search(r'(\d+)%\s*-\s*(.+)', dec)
                    if m:
                        pct = float(m.group(1))
                        extract_tasks[eid]["percent"] = pct
                        extract_tasks[eid]["current_file"] = m.group(2).strip()
                        _calc_eta(extract_tasks[eid])
                    else:
                        m2 = re.search(r'(\d+)%', dec)
                        if m2:
                            pct = float(m2.group(1))
                            extract_tasks[eid]["percent"] = pct
                            _calc_eta(extract_tasks[eid])
                    eta = extract_tasks[eid].get("eta", "")
                    spd = extract_tasks[eid].get("speed", "")
                    p = extract_tasks[eid].get("percent", 0)
                    parts_l = [f"{p:.0f}%"]
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
                    if line:
                        ll = line.lower()
                        if "wrong password" in ll or "password" in ll:
                            err_msg = "Wrong password or archive is password-protected"
                            break
                        err_msg = line[:200]
                        break
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


async def _extract_xz(fp: Path, out_dir: Path, eid: str) -> bool:
    """Extract a single .xz file using xz -d."""
    try:
        import shutil as shutil_mod
        dest_xz = out_dir / fp.name
        shutil_mod.copy2(str(fp), str(dest_xz))
        cmd = ["xz", "-d", "-f", str(dest_xz)]
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
        extract_tasks[eid].update({"status": "failed", "error": "xz not found — install xz-utils on server"})
        return False


async def _extract_split_safe(fp: Path, out_dir: Path, eid: str, password: str = None) -> bool:
    """Try 7z first for split archives, fallback to cat join for unknown split formats."""
    # Try 7z first
    success = await _extract_7z(fp, out_dir, eid, password)
    if success:
        return True

    # Fallback: cat all parts → single file
    extract_tasks[eid]["error"] = None
    extract_tasks[eid]["status"] = "extracting"

    stem = re.sub(r'\.\d{3}$', '', fp.name)
    pattern = re.sub(r'\d{3}$', '*', fp.name)
    parts = sorted(fp.parent.glob(pattern))
    if not parts:
        extract_tasks[eid].update({"status": "failed", "error": "No split parts found"})
        return False

    output_file = out_dir / stem
    try:
        with open(str(output_file), 'wb') as out:
            for i, part in enumerate(parts):
                pct = int((i / len(parts)) * 100)
                extract_tasks[eid]["percent"] = pct
                extract_tasks[eid]["current_file"] = part.name
                extract_tasks[eid]["progress"] = f"{pct}% · Joining {part.name}"
                with open(str(part), 'rb') as f:
                    while True:
                        chunk = f.read(1024 * 1024)  # 1MB chunks
                        if not chunk:
                            break
                        out.write(chunk)
                if extract_tasks.get(eid, {}).get("status") == "cancelled":
                    return False

        elapsed = extract_tasks[eid].get("elapsed", "")
        extract_tasks[eid].update({
            "status": "completed", "percent": 100,
            "progress": f"100% · Joined {len(parts)} parts" + (f" · Done in {elapsed}" if elapsed else ""),
            "speed": "", "eta": "",
            "completed_at": datetime.now().isoformat()
        })
        return True
    except Exception as e:
        extract_tasks[eid].update({"status": "failed", "error": str(e)})
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
