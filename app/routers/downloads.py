"""
Downloads Router - download management API
"""
import uuid
import asyncio
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException

from auth import verify_key
from models import DownloadRequest
from config import MAX_CONNECTIONS
from services.download_service import (
    downloads, run_download, parse_curl_command,
    filename_from_url, sanitize, cancel_download
)

router = APIRouter(prefix="/api", tags=["downloads"])


@router.post("/download")
async def create_download(req: DownloadRequest, _=Depends(verify_key)):
    url, headers = req.url, req.headers or {}
    if req.curl_command:
        url, ph = parse_curl_command(req.curl_command)
        headers = {**ph, **headers}
        if not url:
            raise HTTPException(400, "Could not parse URL")
    if not url:
        raise HTTPException(400, "URL required")

    fn = sanitize(req.filename or filename_from_url(url))
    tid = uuid.uuid4().hex[:12]
    downloads[tid] = {
        "task_id": tid, "status": "queued", "filename": fn, "url": url,
        "engine": req.engine or "auto", "percent": 0, "speed": "",
        "downloaded": 0, "total_size": 0,
        "_headers": headers,
        "created_at": datetime.now().isoformat()
    }
    asyncio.create_task(run_download(
        tid, url, headers, fn,
        req.connections or MAX_CONNECTIONS,
        req.engine or "auto"
    ))
    return {"task_id": tid, "status": "queued", "filename": fn,
            "created_at": downloads[tid]["created_at"]}


@router.get("/downloads")
async def list_downloads(_=Depends(verify_key)):
    return {"downloads": list(downloads.values()), "total": len(downloads)}


@router.get("/status/{tid}")
async def get_status(tid: str, _=Depends(verify_key)):
    if tid not in downloads:
        raise HTTPException(404)
    return downloads[tid]


@router.delete("/downloads/{tid}")
async def api_cancel_download(tid: str, _=Depends(verify_key)):
    if tid not in downloads:
        raise HTTPException(404)
    cancel_download(tid)
    return {"message": f"Cancelled {tid}"}


@router.post("/downloads/{tid}/resume")
async def resume_download(tid: str, _=Depends(verify_key)):
    if tid not in downloads:
        raise HTTPException(404)
    d = downloads[tid]
    if d["status"] not in ("cancelled", "failed"):
        raise HTTPException(400, "Can only resume cancelled or failed downloads")
    url = d.get("url", "")
    if not url:
        raise HTTPException(400, "No URL to resume")
    # Reset status
    d.update({"status": "queued", "percent": 0, "speed": "", "error": ""})
    # Re-use stored headers from original request
    headers = d.get("_headers", {})
    engine = d.get("engine", "auto")
    fn = d.get("filename", "")
    asyncio.create_task(run_download(
        tid, url, headers, fn,
        MAX_CONNECTIONS, engine, resume=True
    ))
    return {"message": f"Resuming {fn}", "task_id": tid}


@router.delete("/downloads")
async def clear_completed(_=Depends(verify_key)):
    to_remove = [k for k, v in downloads.items()
                 if v["status"] in ("completed", "failed", "cancelled")]
    for k in to_remove:
        del downloads[k]
    return {"cleared": len(to_remove)}
