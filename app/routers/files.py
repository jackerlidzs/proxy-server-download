"""
Files Router - file management, metadata, recycle bin, versioning, extract, compress, dedup
"""
import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

from auth import verify_key
from models import (
    RenameRequest, BulkDeleteRequest, CreateFolderRequest,
    CompressRequest, ExtractRequest, TagsRequest, DescriptionRequest, MoveRequest,
    CreateFileRequest
)
from config import DOWNLOAD_DIR, SERVER_URL
from utils import safe_path
from services.file_service import (
    list_dir_items, human_size, get_file_info, update_tags, update_description,
    soft_delete, list_trash, restore_from_trash, purge_trash,
    create_version, list_versions, restore_version, index_file, file_type,
    copy_item, read_text_file, save_text_file
)
from services.extract_service import extract_archive, compress_files, check_parts, cancel_extract
from services.download_service import downloads, sanitize
from services.dedup_service import scan_duplicates, clean_duplicates

router = APIRouter(prefix="/api", tags=["files"])


# --- File Listing ---
@router.get("/files")
async def list_files(path: str = "", _=Depends(verify_key)):
    target = DOWNLOAD_DIR / path if path else DOWNLOAD_DIR
    if not target.exists():
        raise HTTPException(404, "Path not found")
    if not str(target.resolve()).startswith(str(DOWNLOAD_DIR.resolve())):
        raise HTTPException(403, "Access denied")

    # Get set of filenames actively being downloaded
    downloading = set()
    for d in downloads.values():
        if d.get("status") in ("queued", "downloading"):
            fn = d.get("filename", "")
            if fn:
                downloading.add(fn)

    items = list_dir_items(target, DOWNLOAD_DIR, downloading)
    return {"items": items, "total": len(items), "current_path": path}


@router.get("/files/info/{filepath:path}")
async def file_info(filepath: str, _=Depends(verify_key)):
    info = await get_file_info(filepath)
    if not info:
        raise HTTPException(404, "File not found")
    return info


# --- File Operations ---
@router.post("/files/rename/{filename:path}")
async def rename_file(filename: str, req: RenameRequest, _=Depends(verify_key)):
    fp = safe_path(filename)
    if not fp.exists():
        raise HTTPException(404)
    new = sanitize(req.new_name)
    new_path = fp.parent / new
    if new_path.exists():
        raise HTTPException(409, f"'{new}' already exists")
    fp.rename(new_path)
    await index_file(new_path)
    return {"message": f"Renamed to {new}", "new_name": new}


@router.post("/files/move/{filename:path}")
async def move_file(filename: str, req: MoveRequest, _=Depends(verify_key)):
    fp = safe_path(filename)
    if not fp.exists():
        raise HTTPException(404)
    dest = safe_path(req.destination)
    dest.mkdir(parents=True, exist_ok=True)
    new_path = dest / fp.name
    shutil.move(str(fp), str(new_path))
    return {"message": f"Moved to {req.destination}/{fp.name}"}


@router.post("/files/mkdir")
async def create_folder(req: CreateFolderRequest, path: str = "", _=Depends(verify_key)):
    target = DOWNLOAD_DIR / path / sanitize(req.name)
    if target.exists():
        raise HTTPException(409, "Folder already exists")
    target.mkdir(parents=True)
    return {"message": f"Created folder {req.name}"}


@router.post("/files/create")
async def create_file(req: CreateFileRequest, path: str = "", _=Depends(verify_key)):
    """Create a new file with optional content."""
    import re as re_mod
    fn = req.filename.strip()
    if not fn or '..' in fn or '/' in fn or '\\' in fn:
        raise HTTPException(400, "Invalid filename")
    if not re_mod.match(r'^[\w\-. ]+$', fn):
        raise HTTPException(400, "Filename contains invalid characters")
    target_dir = DOWNLOAD_DIR / path if path else DOWNLOAD_DIR
    if not target_dir.exists():
        raise HTTPException(404, "Directory not found")
    fp = target_dir / fn
    if fp.exists():
        raise HTTPException(409, f"File '{fn}' already exists")
    fp.write_text(req.content or "", encoding="utf-8")
    await index_file(fp)
    return {"message": f"Created {fn}", "filename": fn}


@router.delete("/files/{filename:path}")
async def delete_file(filename: str, permanent: bool = False, _=Depends(verify_key)):
    """Soft delete (to recycle bin) by default. Use permanent=true to skip."""
    fp = safe_path(filename)
    if not fp.exists():
        raise HTTPException(404)

    if permanent:
        if fp.is_dir():
            shutil.rmtree(fp)
        else:
            fp.unlink()
    else:
        ok = await soft_delete(filename)
        if not ok:
            raise HTTPException(500, "Failed to move to trash")

    return {"message": f"Deleted {filename}", "permanent": permanent}


@router.post("/files/delete-bulk")
async def bulk_delete(req: BulkDeleteRequest, permanent: bool = False, _=Depends(verify_key)):
    deleted = []
    for fn in req.filenames:
        try:
            fp = safe_path(fn)
        except HTTPException:
            continue
        if not fp.exists():
            continue
        if permanent:
            if fp.is_dir():
                shutil.rmtree(fp)
            else:
                fp.unlink()
        else:
            await soft_delete(fn)
        deleted.append(fn)
    return {"deleted": deleted, "count": len(deleted)}


# --- Upload ---
@router.post("/upload")
async def upload_file(file: UploadFile = File(...), path: str = Form(""), _=Depends(verify_key)):
    target_dir = DOWNLOAD_DIR / path if path else DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    fp = target_dir / sanitize(file.filename or "uploaded_file")

    # Version existing file before overwrite
    if fp.exists():
        rel = str(fp.relative_to(DOWNLOAD_DIR))
        await create_version(rel)

    with open(fp, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    await index_file(fp)
    return {
        "filename": fp.name,
        "size": fp.stat().st_size,
        "size_human": human_size(fp.stat().st_size)
    }


# --- Chunked Upload ---
@router.post("/upload/chunk")
async def upload_chunk(
    chunk: UploadFile = File(...),
    request: Request = None,
    _=Depends(verify_key)
):
    """Receive a single chunk. Auto-merge when all chunks received."""
    from config import TEMP_DIR

    file_id = request.headers.get("X-File-Id", "")
    index = int(request.headers.get("X-Chunk-Index", "0"))
    total = int(request.headers.get("X-Total-Chunks", "1"))
    filename = request.headers.get("X-Filename", "uploaded_file")
    upload_path = request.headers.get("X-Upload-Path", "")

    if not file_id:
        raise HTTPException(400, "Missing X-File-Id header")
    if total < 1:
        raise HTTPException(400, "Invalid X-Total-Chunks")

    # Save chunk to temp/{file_id}/chunk_{index}
    chunk_dir = TEMP_DIR / file_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunk_dir / f"chunk_{index}"

    with open(chunk_path, "wb") as f:
        while data := await chunk.read(1024 * 1024):
            f.write(data)

    # Check if all chunks received
    received = len(list(chunk_dir.glob("chunk_*")))
    if received >= total:
        # Merge all chunks into final file
        target_dir = DOWNLOAD_DIR / upload_path if upload_path else DOWNLOAD_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        fp = target_dir / sanitize(filename)

        # Version existing file before overwrite
        if fp.exists():
            rel = str(fp.relative_to(DOWNLOAD_DIR))
            await create_version(rel)

        with open(fp, "wb") as out:
            for i in range(total):
                cp = chunk_dir / f"chunk_{i}"
                if not cp.exists():
                    raise HTTPException(400, f"Missing chunk {i}")
                with open(cp, "rb") as inp:
                    while block := inp.read(1024 * 1024):
                        out.write(block)

        # Cleanup temp chunks
        shutil.rmtree(chunk_dir, ignore_errors=True)

        await index_file(fp)
        return {
            "status": "ok",
            "filename": fp.name,
            "size": fp.stat().st_size,
            "size_human": human_size(fp.stat().st_size)
        }

    return {"status": "received", "chunk": index}


@router.delete("/upload/chunk/{file_id}")
async def cancel_chunk_upload(file_id: str, _=Depends(verify_key)):
    """Cancel chunked upload — cleanup temp chunks."""
    from config import TEMP_DIR

    chunk_dir = TEMP_DIR / file_id
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)
    return {"status": "cancelled"}


# --- Tags & Description ---
@router.post("/files/tags/{filepath:path}")
async def set_tags(filepath: str, req: TagsRequest, _=Depends(verify_key)):
    await update_tags(filepath, req.tags)
    return {"message": "Tags updated", "tags": req.tags}


@router.post("/files/description/{filepath:path}")
async def set_description(filepath: str, req: DescriptionRequest, _=Depends(verify_key)):
    await update_description(filepath, req.description)
    return {"message": "Description updated"}


# --- Recycle Bin ---
@router.get("/trash")
async def get_trash(_=Depends(verify_key)):
    items = await list_trash()
    return {"items": items, "total": len(items)}


@router.post("/trash/restore/{item_id}")
async def restore_trash(item_id: int, _=Depends(verify_key)):
    ok = await restore_from_trash(item_id)
    if not ok:
        raise HTTPException(404, "Item not found in trash")
    return {"message": "Restored successfully"}


@router.delete("/trash/purge")
async def purge_all_trash(_=Depends(verify_key)):
    await purge_trash()
    return {"message": "Trash purged"}


@router.delete("/trash/{item_id}")
async def purge_single(item_id: int, _=Depends(verify_key)):
    await purge_trash(item_id)
    return {"message": "Item permanently deleted"}


# --- Versioning ---
@router.get("/files/versions/{filepath:path}")
async def get_versions(filepath: str, _=Depends(verify_key)):
    versions = await list_versions(filepath)
    return {"versions": versions, "total": len(versions)}


@router.post("/files/restore-version/{filepath:path}")
async def do_restore_version(filepath: str, version: int, _=Depends(verify_key)):
    ok = await restore_version(filepath, version)
    if not ok:
        raise HTTPException(404, "Version not found")
    return {"message": f"Restored version {version}"}


# --- Extract & Compress ---
@router.post("/extract/{filename:path}")
async def extract_file(filename: str, request: Request, _=Depends(verify_key)):
    fp = safe_path(filename)
    if not fp.exists():
        raise HTTPException(404, "File not found")

    delete_after = False
    password = None
    try:
        body = await request.json()
        delete_after = body.get("delete_after", False)
        password = body.get("password", None)
    except Exception:
        pass

    result = await extract_archive(filename, delete_after=delete_after, password=password)

    if not result["success"]:
        if "missing_files" in result:
            raise HTTPException(400, {
                "error": result["error"],
                "missing_files": result["missing_files"],
                "found_parts": result.get("found_parts", []),
                "total_parts": result.get("total_parts", 0)
            })
        raise HTTPException(400, result.get("error", "Extraction failed"))

    return result


@router.get("/extract/stream/{eid}")
async def extract_stream(eid: str):
    """SSE stream for realtime extract progress. Unauthenticated — UUID is the token."""
    from services.extract_service import extract_tasks, stream_job
    from fastapi.responses import StreamingResponse
    if eid not in extract_tasks:
        raise HTTPException(404, "Extract task not found")
    return StreamingResponse(
        stream_job(eid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/extract-tasks")
async def get_extract_tasks(_=Depends(verify_key)):
    """Get extract task progress (fallback polling endpoint)."""
    from services.extract_service import extract_tasks
    # Filter out internal fields (prefixed with _)
    tasks = [{k: v for k, v in t.items() if not k.startswith('_')} for t in extract_tasks.values()]
    # Clean up old completed/failed/cancelled tasks
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
    to_remove = [k for k, v in extract_tasks.items()
                 if v.get("status") in ("completed", "failed", "cancelled") and v.get("created_at", "") < cutoff]
    for k in to_remove:
        extract_tasks.pop(k, None)
    return {"tasks": tasks}


@router.delete("/extract-tasks/{eid}")
async def cancel_extract_task(eid: str, _=Depends(verify_key)):
    """Cancel an active extraction."""
    from services.extract_service import extract_tasks
    if eid not in extract_tasks:
        raise HTTPException(404, "Extract task not found")
    cancel_extract(eid)
    return {"message": f"Cancelled {eid}"}


@router.post("/extract/check/{filename:path}")
async def check_archive_parts(filename: str, _=Depends(verify_key)):
    """Check if all parts of a multi-part archive are present."""
    fp = DOWNLOAD_DIR / filename
    if not fp.exists():
        raise HTTPException(404)
    return check_parts(fp.name, fp.parent)


@router.post("/compress")
async def compress(req: CompressRequest, _=Depends(verify_key)):
    result = await compress_files(req.filenames, req.archive_name, req.format, downloads)
    if not result["success"]:
        raise HTTPException(400, result.get("error", "Compression failed"))
    return {"message": "Compression started", "task_id": result.get("task_id")}


# --- Deduplication ---
@router.get("/dedup/scan")
async def dedup_scan(_=Depends(verify_key)):
    return await scan_duplicates()


@router.post("/dedup/clean")
async def dedup_clean(strategy: str = "first", _=Depends(verify_key)):
    return await clean_duplicates(strategy)


# --- Copy ---
@router.post("/files/copy/{filepath:path}")
async def copy_file(filepath: str, destination: str = "", _=Depends(verify_key)):
    """Copy a file or folder. Destination is relative path (empty = same dir)."""
    result = await copy_item(filepath, destination)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "Copy failed"))
    return result


# --- File Content (Preview/Edit) ---
@router.get("/files/content/{filepath:path}")
async def get_file_content(filepath: str, _=Depends(verify_key)):
    """Read text file content for preview/editing."""
    result = read_text_file(filepath)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.put("/files/content/{filepath:path}")
async def save_file_content(filepath: str, request: Request, _=Depends(verify_key)):
    """Save edited text file content. Auto-versions before overwrite."""
    body = await request.json()
    content = body.get("content", "")
    result = await save_text_file(filepath, content)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result
