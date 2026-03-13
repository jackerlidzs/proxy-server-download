"""
Share Router - public file sharing with expiry and optional password
"""
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from auth import verify_key, hash_password, verify_password
from models import ShareLinkRequest
from config import DOWNLOAD_DIR, SERVER_URL
from database import get_db

router = APIRouter(tags=["share"])


@router.post("/api/share")
async def create_share(req: ShareLinkRequest, user=Depends(verify_key)):
    """Create a public share link for a file."""
    fp = DOWNLOAD_DIR / req.filepath
    if not fp.exists():
        raise HTTPException(404, "File not found")

    db = await get_db()
    token = uuid.uuid4().hex[:16]
    expires = None
    if req.expire_hours:
        expires = (datetime.now() + timedelta(hours=req.expire_hours)).isoformat()

    pw_hash = hash_password(req.password) if req.password else None

    await db.execute("""
        INSERT INTO share_links (token, file_path, created_by, expires_at, password_hash, max_downloads, download_count)
        VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (token, req.filepath, user["username"], expires, pw_hash, req.max_downloads or 0))
    await db.commit()

    url = f"{SERVER_URL}/s/{token}"
    return {
        "token": token, "url": url, "filepath": req.filepath,
        "expires_at": expires,
        "password_protected": bool(req.password),
        "max_downloads": req.max_downloads
    }


@router.get("/api/shares")
async def list_shares(_=Depends(verify_key)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM share_links ORDER BY created_at DESC"
    )
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "token": r["token"],
            "file_path": r["file_path"],
            "url": f"{SERVER_URL}/s/{r['token']}",
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "password_protected": bool(r["password_hash"]),
            "max_downloads": r["max_downloads"],
            "download_count": r["download_count"]
        })
    return {"shares": items, "total": len(items)}


@router.delete("/api/share/{token}")
async def delete_share(token: str, _=Depends(verify_key)):
    db = await get_db()
    await db.execute("DELETE FROM share_links WHERE token=?", (token,))
    await db.commit()
    return {"message": "Share link deleted"}


# --- Public access (no auth required) ---
@router.get("/s/{token}")
async def access_share(token: str, request: Request, password: str = None):
    """Public access to shared file."""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM share_links WHERE token=?", (token,))
    if not rows:
        raise HTTPException(404, "Share link not found or expired")

    share = rows[0]

    # Check expiry
    if share["expires_at"]:
        if datetime.fromisoformat(share["expires_at"]) < datetime.now():
            await db.execute("DELETE FROM share_links WHERE token=?", (token,))
            await db.commit()
            raise HTTPException(410, "Share link has expired")

    # Check max downloads
    if share["max_downloads"] and share["max_downloads"] > 0:
        if share["download_count"] >= share["max_downloads"]:
            raise HTTPException(410, "Download limit reached")

    # Check password
    if share["password_hash"]:
        if not password:
            raise HTTPException(401, "Password required. Use ?password=xxx")
        if not verify_password(password, share["password_hash"]):
            raise HTTPException(403, "Wrong password")

    # Serve file
    fp = DOWNLOAD_DIR / share["file_path"]
    if not fp.exists():
        raise HTTPException(404, "File no longer exists")

    # Increment download count
    await db.execute(
        "UPDATE share_links SET download_count = download_count + 1 WHERE token=?",
        (token,)
    )
    await db.commit()

    # Stream with byte-range support
    import re
    sz = fp.stat().st_size
    import mimetypes
    ct = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
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
                        d = f.read(min(262144, rem))
                        if not d:
                            break
                        rem -= len(d)
                        yield d

            return StreamingResponse(gen(), status_code=206, headers={
                "Content-Range": f"bytes {start}-{end}/{sz}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": ct,
                "Content-Disposition": f'attachment; filename="{fp.name}"',
                "Access-Control-Allow-Origin": "*"
            })

    return FileResponse(
        str(fp),
        media_type=ct,
        filename=fp.name,
        headers={"Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*"}
    )
