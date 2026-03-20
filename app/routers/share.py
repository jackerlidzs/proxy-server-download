"""
Share Router - public file sharing with expiry and optional password
"""
import uuid
import re
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse

from auth import verify_key, hash_password, verify_password
from models import ShareLinkRequest
from config import DOWNLOAD_DIR
from database import get_db

router = APIRouter(tags=["share"])


def get_external_base(request: Request) -> str:
    """Get external base URL using X-Forwarded headers from nginx (Docker)."""
    proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{proto}://{host}"


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    else:
        return f"{size_bytes / 1024 ** 3:.2f} GB"


@router.post("/api/share")
async def create_share(req: ShareLinkRequest, request: Request, user=Depends(verify_key)):
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

    base = get_external_base(request)
    url = f"{base}/s/{token}"
    return {
        "token": token, "url": url, "filepath": req.filepath,
        "expires_at": expires,
        "password_protected": bool(req.password),
        "max_downloads": req.max_downloads
    }


@router.get("/api/shares")
async def list_shares(request: Request, _=Depends(verify_key)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM share_links ORDER BY created_at DESC"
    )
    base = get_external_base(request)
    items = []
    for r in rows:
        # Check if expired
        expired = False
        if r["expires_at"]:
            try:
                expired = datetime.fromisoformat(r["expires_at"]) < datetime.now()
            except Exception:
                pass

        # Check if download limit reached
        limit_reached = False
        if r["max_downloads"] and r["max_downloads"] > 0:
            limit_reached = r["download_count"] >= r["max_downloads"]

        items.append({
            "id": r["id"], "token": r["token"],
            "file_path": r["file_path"],
            "url": f"{base}/s/{r['token']}",
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "password_protected": bool(r["password_hash"]),
            "max_downloads": r["max_downloads"],
            "download_count": r["download_count"],
            "expired": expired,
            "limit_reached": limit_reached,
            "active": not expired and not limit_reached
        })
    return {"shares": items, "total": len(items)}


@router.delete("/api/share/{token}")
async def delete_share(token: str, _=Depends(verify_key)):
    db = await get_db()
    await db.execute("DELETE FROM share_links WHERE token=?", (token,))
    await db.commit()
    return {"message": "Share link deleted"}


# ─── Public access (no auth required) ────────────────────────────

def _share_download_page(share, fp: Path, token: str, error: str = "") -> str:
    """Generate a beautiful HTML download page."""
    filename = fp.name
    size = _human_size(fp.stat().st_size) if fp.exists() else "Unknown"
    has_password = bool(share["password_hash"])

    # File type icon
    ext = fp.suffix.lower()
    vid_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
    aud_exts = {".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"}
    img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    arch_exts = {".rar", ".zip", ".7z", ".tar", ".gz", ".tgz"}

    if ext in vid_exts:
        icon, color, type_label = "🎬", "#a78bfa", "Video"
    elif ext in aud_exts:
        icon, color, type_label = "🎵", "#f472b6", "Audio"
    elif ext in img_exts:
        icon, color, type_label = "🖼", "#34d399", "Image"
    elif ext in arch_exts:
        icon, color, type_label = "📦", "#fbbf24", "Archive"
    else:
        icon, color, type_label = "📄", "#60a5fa", "File"

    # Expiry info
    expiry_html = ""
    if share["expires_at"]:
        try:
            exp = datetime.fromisoformat(share["expires_at"])
            remaining = exp - datetime.now()
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                mins = int((remaining.total_seconds() % 3600) // 60)
                expiry_html = f'<div class="info-row"><span class="label">⏱ Expires</span><span class="value">{hours}h {mins}m remaining</span></div>'
            else:
                expiry_html = '<div class="info-row"><span class="label">⏱ Expired</span><span class="value expired">Link has expired</span></div>'
        except Exception:
            pass

    # Download count
    dl_info = ""
    if share["max_downloads"] and share["max_downloads"] > 0:
        dl_info = f'<div class="info-row"><span class="label">📥 Downloads</span><span class="value">{share["download_count"]} / {share["max_downloads"]}</span></div>'
    else:
        dl_info = f'<div class="info-row"><span class="label">📥 Downloads</span><span class="value">{share["download_count"]}</span></div>'

    error_html = f'<div class="error">❌ {error}</div>' if error else ""

    # Password form or download button
    if has_password:
        action_html = f'''
        <form method="POST" action="/s/{token}" class="pw-form">
            {error_html}
            <div class="input-group">
                <input type="password" name="password" placeholder="Enter password" required autofocus>
            </div>
            <button type="submit" class="btn-download">🔓 Unlock & Download</button>
        </form>'''
    else:
        action_html = f'''
        {error_html}
        <a href="/s/{token}/download" class="btn-download">⬇ Download File</a>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Download: {filename}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',sans-serif;background:#0f0f17;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
.card{{background:#1a1a2e;border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:32px;max-width:420px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.4)}}
.file-icon{{width:64px;height:64px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 16px;background:{color}22;border:1px solid {color}44}}
.file-name{{font-size:16px;font-weight:600;text-align:center;word-break:break-all;margin-bottom:4px;color:#f1f5f9}}
.file-type{{font-size:11px;text-align:center;color:{color};font-weight:600;letter-spacing:.5px;text-transform:uppercase;margin-bottom:20px}}
.info-box{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px 16px;margin-bottom:20px}}
.info-row{{display:flex;justify-content:space-between;align-items:center;padding:6px 0;font-size:13px}}
.info-row+.info-row{{border-top:1px solid rgba(255,255,255,.06)}}
.info-row .label{{color:#94a3b8}}
.info-row .value{{color:#e2e8f0;font-weight:500}}
.info-row .value.expired{{color:#f87171}}
.btn-download{{display:block;width:100%;padding:14px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none;transition:all .2s}}
.btn-download:hover{{transform:translateY(-1px);box-shadow:0 8px 24px rgba(99,102,241,.35)}}
.pw-form .input-group{{margin-bottom:14px}}
.pw-form input{{width:100%;padding:12px 16px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#e2e8f0;font-size:14px;font-family:inherit;outline:none;transition:border-color .2s}}
.pw-form input:focus{{border-color:#6366f1}}
.error{{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.25);color:#f87171;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}}
.footer{{text-align:center;margin-top:20px;font-size:11px;color:#475569}}
.lock-badge{{display:inline-flex;align-items:center;gap:4px;background:rgba(251,191,36,.12);color:#fbbf24;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;margin:0 auto 16px;}}
.lock-badge-wrap{{text-align:center}}
</style>
</head>
<body>
<div class="card">
    <div class="file-icon">{icon}</div>
    <div class="file-name">{filename}</div>
    <div class="file-type">{type_label} · {ext.lstrip('.')}</div>
    {'<div class="lock-badge-wrap"><span class="lock-badge">🔒 Password Protected</span></div>' if has_password else ''}
    <div class="info-box">
        <div class="info-row"><span class="label">📦 Size</span><span class="value">{size}</span></div>
        {expiry_html}
        {dl_info}
    </div>
    {action_html}
    <div class="footer">Shared via Proxy Server</div>
</div>
</body>
</html>"""


async def _validate_share(token: str):
    """Validate share token and return share record or raise HTTPException."""
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

    return share


@router.get("/s/{token}")
async def share_page(token: str, request: Request):
    """Public share page — show file info and download button."""
    share = await _validate_share(token)
    fp = DOWNLOAD_DIR / share["file_path"]
    if not fp.exists():
        raise HTTPException(404, "File no longer exists")

    return HTMLResponse(_share_download_page(share, fp, token))


@router.post("/s/{token}")
async def share_verify_password(token: str, request: Request):
    """Verify password for protected share links."""
    share = await _validate_share(token)
    fp = DOWNLOAD_DIR / share["file_path"]
    if not fp.exists():
        raise HTTPException(404, "File no longer exists")

    # Parse form data
    form = await request.form()
    password = form.get("password", "")

    if not share["password_hash"]:
        # No password needed, redirect to download
        return HTMLResponse(
            f'<html><head><meta http-equiv="refresh" content="0;url=/s/{token}/download"></head></html>'
        )

    if not password or not verify_password(password, share["password_hash"]):
        return HTMLResponse(_share_download_page(share, fp, token, error="Wrong password"))

    # Password correct — redirect to download with temp query param
    # Increment download count
    db = await get_db()
    await db.execute(
        "UPDATE share_links SET download_count = download_count + 1 WHERE token=?",
        (token,)
    )
    await db.commit()

    # Serve file directly after password verification
    return await _serve_file(fp, request)


@router.get("/s/{token}/download")
async def share_download(token: str, request: Request, password: str = None):
    """Download the shared file."""
    share = await _validate_share(token)

    # Check password
    if share["password_hash"]:
        if not password:
            # Redirect to share page for password entry
            return HTMLResponse(
                f'<html><head><meta http-equiv="refresh" content="0;url=/s/{token}"></head></html>'
            )
        if not verify_password(password, share["password_hash"]):
            return HTMLResponse(
                f'<html><head><meta http-equiv="refresh" content="0;url=/s/{token}"></head></html>'
            )

    fp = DOWNLOAD_DIR / share["file_path"]
    if not fp.exists():
        raise HTTPException(404, "File no longer exists")

    # Increment download count
    db = await get_db()
    await db.execute(
        "UPDATE share_links SET download_count = download_count + 1 WHERE token=?",
        (token,)
    )
    await db.commit()

    return await _serve_file(fp, request)


async def _serve_file(fp: Path, request: Request):
    """Serve file with byte-range support."""
    sz = fp.stat().st_size
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
