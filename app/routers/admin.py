"""
Admin Router - user management, activity log, search, system info
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import verify_key, require_admin, hash_password, verify_password, create_token, get_user_quota
from models import LoginRequest, CreateUserRequest
from database import get_db, log_activity
from config import DOWNLOAD_DIR, SYSTEM_DIRS

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/login")
async def login(req: LoginRequest):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM users WHERE username=?", (req.username,))
    if not rows:
        raise HTTPException(401, "Invalid credentials")

    user = rows[0]
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    token = create_token(user["username"], user["role"])
    await log_activity("login", user["username"])

    quota = await get_user_quota(user["username"])
    return {
        "token": token,
        "username": user["username"],
        "role": user["role"],
        "quota": quota
    }


@router.post("/users")
async def create_user(req: CreateUserRequest, user=Depends(require_admin)):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash, role, quota_bytes, used_bytes, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (req.username, hash_password(req.password), req.role, req.quota_bytes,
             datetime.now().isoformat())
        )
        await db.commit()
        await log_activity("create_user", user["username"], f"Created {req.username} ({req.role})")
        return {"message": f"User {req.username} created", "role": req.role}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "Username already exists")
        raise HTTPException(500, str(e))


@router.get("/users")
async def list_users(user=Depends(require_admin)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, username, role, quota_bytes, used_bytes, created_at FROM users")
    users = []
    for r in rows:
        quota = r["quota_bytes"] or 0
        used = r["used_bytes"] or 0
        users.append({
            "id": r["id"], "username": r["username"], "role": r["role"],
            "quota_bytes": quota, "used_bytes": used,
            "quota_human": _hs(quota) if quota else "Unlimited",
            "used_human": _hs(used),
            "usage_pct": round(used / quota * 100, 1) if quota > 0 else 0,
            "created_at": r["created_at"]
        })
    return {"users": users}


@router.delete("/users/{username}")
async def delete_user(username: str, user=Depends(require_admin)):
    if username == user["username"]:
        raise HTTPException(400, "Cannot delete yourself")
    db = await get_db()
    await db.execute("DELETE FROM users WHERE username=?", (username,))
    await db.commit()
    await log_activity("delete_user", user["username"], f"Deleted {username}")
    return {"message": f"Deleted user {username}"}


# --- Activity Log ---
@router.get("/activity")
async def get_activity(limit: int = Query(50, le=200), _=Depends(verify_key)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return {"activities": [dict(r) for r in rows]}


# --- Enhanced Search ---
@router.get("/search")
async def search_files(
    q: str = Query("", description="Search query"),
    type: str = Query("", description="File type filter: video, audio, image, archive"),
    min_size: int = Query(0, description="Minimum file size in bytes"),
    max_size: int = Query(0, description="Maximum file size in bytes"),
    tag: str = Query("", description="Filter by tag"),
    _=Depends(verify_key)
):
    """Enhanced file search with filters."""
    results = []

    if not DOWNLOAD_DIR.exists():
        return {"results": [], "total": 0}

    from config import VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS, ARCHIVE_EXTS
    type_map = {
        "video": VIDEO_EXTS, "audio": AUDIO_EXTS,
        "image": IMAGE_EXTS, "archive": ARCHIVE_EXTS
    }

    for f in sorted(DOWNLOAD_DIR.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or any(sd in f.parts for sd in SYSTEM_DIRS):
            continue

        rel = str(f.relative_to(DOWNLOAD_DIR))
        name = f.name.lower()
        ext = f.suffix.lower()

        # Name search
        if q and q.lower() not in name:
            continue

        # Type filter
        if type and type in type_map:
            if ext not in type_map[type]:
                continue

        # Size filter
        sz = f.stat().st_size
        if min_size and sz < min_size:
            continue
        if max_size and sz > max_size:
            continue

        # Tag filter (search DB)
        if tag:
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT tags FROM file_metadata WHERE path=?", (rel,)
            )
            if rows:
                tags = rows[0]["tags"] or "[]"
                if tag.lower() not in tags.lower():
                    continue
            else:
                continue

        results.append({
            "name": f.name, "path": rel, "ext": ext,
            "size": sz, "size_human": _hs(sz),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        })

        if len(results) >= 100:
            break

    return {"results": results, "total": len(results), "query": q}


# --- Dashboard stats ---
@router.get("/stats")
async def dashboard_stats(_=Depends(verify_key)):
    """Get dashboard statistics."""
    import shutil
    db = await get_db()

    # File counts by type
    from config import VIDEO_EXTS, AUDIO_EXTS, ARCHIVE_EXTS

    total_files = 0
    total_size = 0
    type_counts = {"video": 0, "audio": 0, "archive": 0, "other": 0}

    if DOWNLOAD_DIR.exists():
        for f in DOWNLOAD_DIR.rglob("*"):
            if f.is_file() and not f.name.startswith(".") and not any(sd in f.parts for sd in SYSTEM_DIRS):
                total_files += 1
                sz = f.stat().st_size
                total_size += sz
                ext = f.suffix.lower()
                if ext in VIDEO_EXTS:
                    type_counts["video"] += 1
                elif ext in AUDIO_EXTS:
                    type_counts["audio"] += 1
                elif ext in ARCHIVE_EXTS:
                    type_counts["archive"] += 1
                else:
                    type_counts["other"] += 1

    # Trash count
    rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM recycle_bin")
    trash_count = rows[0]["c"] if rows else 0

    # Share count
    rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM share_links")
    share_count = rows[0]["c"] if rows else 0

    # User count
    rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM users")
    user_count = rows[0]["c"] if rows else 0

    # Recent activity
    rows = await db.execute_fetchall(
        "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT 10"
    )
    recent = [dict(r) for r in rows]

    # Disk info
    disk = shutil.disk_usage(str(DOWNLOAD_DIR))

    return {
        "total_files": total_files,
        "total_size": total_size,
        "total_size_human": _hs(total_size),
        "type_counts": type_counts,
        "trash_count": trash_count,
        "share_count": share_count,
        "user_count": user_count,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_pct": round(disk.used / disk.total * 100, 1),
        "recent_activity": recent
    }


def _hs(b):
    if not b: return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"
