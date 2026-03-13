"""
Extract Service - archive extraction and compression
Supports: .rar (multi-part), .zip, .7z, .tar.gz
"""
import re
import uuid
import asyncio
from pathlib import Path
from datetime import datetime

from config import DOWNLOAD_DIR, MAX_CONCURRENT_EXTRACT

extract_semaphore: asyncio.Semaphore = None
RAR_PATTERN = re.compile(r'^(.+?)\.part(\d+)\.rar$', re.IGNORECASE)


def init_extract_semaphore():
    global extract_semaphore
    extract_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACT)


def part_group(fn: str):
    m = RAR_PATTERN.match(fn)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


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


async def extract_archive(filename: str, downloads_dict: dict, delete_after: bool = False,
                           base_dir: Path = None) -> dict:
    """Extract an archive file. Returns extraction status."""
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
            # Use part1.rar for extraction
            g, p = part_group(fp.name)
            if g and p != 1:
                part1 = fp.parent / f"{g}.part1.rar"
                if part1.exists():
                    fp = part1
                    filename = str(part1.relative_to(base_dir))

    downloads_dict[eid] = {
        "task_id": eid, "status": "extracting", "filename": fp.name,
        "group": group, "progress": "Starting...", "percent": 0,
        "created_at": datetime.now().isoformat()
    }

    async with extract_semaphore:
        try:
            if ext == ".rar" or name_lower.endswith(".rar"):
                result = await _extract_rar(fp, base_dir, eid, downloads_dict)
            elif ext == ".zip":
                result = await _extract_zip(fp, base_dir, eid, downloads_dict)
            elif ext == ".7z":
                result = await _extract_7z(fp, base_dir, eid, downloads_dict)
            elif name_lower.endswith((".tar.gz", ".tgz")):
                result = await _extract_tar(fp, base_dir, eid, downloads_dict)
            elif ext == ".tar":
                result = await _extract_tar(fp, base_dir, eid, downloads_dict)
            elif ext == ".gz":
                result = await _extract_tar(fp, base_dir, eid, downloads_dict)
            else:
                downloads_dict[eid].update({"status": "failed", "error": f"Unsupported format: {ext}"})
                return {"success": False, "error": f"Unsupported format: {ext}", "task_id": eid}

            if result and delete_after:
                await _cleanup_archives(fp, group, base_dir)

            return {"success": result, "task_id": eid}

        except Exception as e:
            downloads_dict[eid].update({"status": "failed", "error": str(e)})
            return {"success": False, "error": str(e), "task_id": eid}


async def _extract_rar(fp: Path, out_dir: Path, eid: str, downloads_dict: dict) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "unrar", "x", "-o+", "-y", str(fp), str(out_dir) + "/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                lines.append(dec)
                m = re.search(r'(\d+)%', dec)
                if m:
                    downloads_dict[eid]["percent"] = float(m.group(1))
                    downloads_dict[eid]["progress"] = f"{m.group(1)}%"
        await proc.wait()

        if proc.returncode == 0:
            downloads_dict[eid].update({
                "status": "completed", "percent": 100, "progress": "100%",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            downloads_dict[eid].update({"status": "failed", "error": "\n".join(lines[-5:])})
            return False
    except FileNotFoundError:
        downloads_dict[eid].update({"status": "failed", "error": "unrar not found"})
        return False


async def _extract_zip(fp: Path, out_dir: Path, eid: str, downloads_dict: dict) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "unzip", "-o", str(fp), "-d", str(out_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        downloads_dict[eid].update({"percent": 50, "progress": "Extracting..."})
        output, _ = await proc.communicate()

        if proc.returncode == 0:
            downloads_dict[eid].update({
                "status": "completed", "percent": 100, "progress": "100%",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            downloads_dict[eid].update({
                "status": "failed",
                "error": output.decode(errors="ignore")[-200:]
            })
            return False
    except FileNotFoundError:
        downloads_dict[eid].update({"status": "failed", "error": "unzip not found"})
        return False


async def _extract_7z(fp: Path, out_dir: Path, eid: str, downloads_dict: dict) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "7z", "x", "-y", f"-o{out_dir}", str(fp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            dec = line.decode("utf-8", errors="ignore").strip()
            if dec:
                lines.append(dec)
                m = re.search(r'(\d+)%', dec)
                if m:
                    downloads_dict[eid]["percent"] = float(m.group(1))
                    downloads_dict[eid]["progress"] = f"{m.group(1)}%"
        await proc.wait()

        if proc.returncode == 0:
            downloads_dict[eid].update({
                "status": "completed", "percent": 100, "progress": "100%",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            downloads_dict[eid].update({"status": "failed", "error": "\n".join(lines[-5:])})
            return False
    except FileNotFoundError:
        downloads_dict[eid].update({"status": "failed", "error": "7z not found"})
        return False


async def _extract_tar(fp: Path, out_dir: Path, eid: str, downloads_dict: dict) -> bool:
    try:
        cmd = ["tar", "-xf", str(fp), "-C", str(out_dir)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        downloads_dict[eid].update({"percent": 50, "progress": "Extracting..."})
        output, _ = await proc.communicate()

        if proc.returncode == 0:
            downloads_dict[eid].update({
                "status": "completed", "percent": 100, "progress": "100%",
                "completed_at": datetime.now().isoformat()
            })
            return True
        else:
            downloads_dict[eid].update({
                "status": "failed",
                "error": output.decode(errors="ignore")[-200:]
            })
            return False
    except FileNotFoundError:
        downloads_dict[eid].update({"status": "failed", "error": "tar not found"})
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
