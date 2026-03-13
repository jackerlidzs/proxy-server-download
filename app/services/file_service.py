"""
File Service - metadata, versioning, recycle bin operations
"""
import os
import hashlib
import mimetypes
import json
from pathlib import Path
from datetime import datetime, timedelta

from config import (
    DOWNLOAD_DIR, TRASH_DIR, VERSIONS_DIR, SYSTEM_DIRS,
    MAX_HASH_CHUNK, MAX_VERSIONS, TRASH_EXPIRE_DAYS,
    VIDEO_EXTS, AUDIO_EXTS, SUBTITLE_EXTS, ARCHIVE_EXTS, IMAGE_EXTS, TEXT_EXTS, SERVER_URL
)
from database import get_db


def file_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS: return "video"
    if ext in AUDIO_EXTS: return "audio"
    if ext in SUBTITLE_EXTS: return "subtitle"
    if ext in ARCHIVE_EXTS: return "archive"
    if ext in IMAGE_EXTS: return "image"
    return "file"


def human_size(b) -> str:
    if not b: return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def compute_md5(filepath: Path) -> str:
    """Stream-based MD5 to avoid loading entire file into memory."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(MAX_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


async def index_file(path: Path):
    """Index a single file's metadata into DB."""
    if not path.is_file():
        return
    db = await get_db()
    rel = str(path.relative_to(DOWNLOAD_DIR))
    stat = path.stat()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    await db.execute("""
        INSERT OR REPLACE INTO file_metadata (path, mime_type, size, created_at, modified_at)
        VALUES (?, ?, ?, ?, ?)
    """, (rel, mime, stat.st_size,
          datetime.fromtimestamp(stat.st_ctime).isoformat(),
          datetime.fromtimestamp(stat.st_mtime).isoformat()))
    await db.commit()


async def index_file_with_hash(path: Path):
    """Index file with MD5 hash (for dedup). Run in background."""
    if not path.is_file():
        return
    db = await get_db()
    rel = str(path.relative_to(DOWNLOAD_DIR))
    md5 = compute_md5(path)
    await db.execute("UPDATE file_metadata SET hash_md5=? WHERE path=?", (md5, rel))
    await db.commit()


async def get_file_info(rel_path: str) -> dict:
    """Get detailed file metadata."""
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM file_metadata WHERE path=?", (rel_path,))
    if row:
        r = row[0]
        return {
            "path": r["path"], "mime_type": r["mime_type"], "size": r["size"],
            "hash_md5": r["hash_md5"], "created_at": r["created_at"],
            "modified_at": r["modified_at"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "description": r["description"] or ""
        }
    # Fallback: read from filesystem
    fp = DOWNLOAD_DIR / rel_path
    if not fp.exists():
        return None
    stat = fp.stat()
    return {
        "path": rel_path,
        "mime_type": mimetypes.guess_type(fp.name)[0] or "application/octet-stream",
        "size": stat.st_size,
        "hash_md5": None,
        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "tags": [], "description": ""
    }


async def update_tags(rel_path: str, tags: list[str]):
    db = await get_db()
    await db.execute("UPDATE file_metadata SET tags=? WHERE path=?", (json.dumps(tags), rel_path))
    await db.commit()


async def update_description(rel_path: str, desc: str):
    db = await get_db()
    await db.execute("UPDATE file_metadata SET description=? WHERE path=?", (desc, rel_path))
    await db.commit()


# --- Recycle Bin ---
async def soft_delete(rel_path: str) -> bool:
    """Move file/folder to trash instead of permanent delete."""
    fp = DOWNLOAD_DIR / rel_path
    if not fp.exists():
        return False

    TRASH_DIR.mkdir(parents=True, exist_ok=True)

    # Create unique trash name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_name = f"{ts}_{fp.name}"
    trash_path = TRASH_DIR / trash_name

    import shutil
    if fp.is_dir():
        shutil.move(str(fp), str(trash_path))
        size = sum(f.stat().st_size for f in trash_path.rglob("*") if f.is_file())
    else:
        size = fp.stat().st_size
        shutil.move(str(fp), str(trash_path))

    db = await get_db()
    expires = datetime.now() + timedelta(days=TRASH_EXPIRE_DAYS)
    await db.execute("""
        INSERT INTO recycle_bin (original_path, trash_path, filename, size, deleted_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (rel_path, str(trash_path.relative_to(DOWNLOAD_DIR)),
          fp.name, size, datetime.now().isoformat(), expires.isoformat()))
    await db.commit()

    # Remove from metadata
    await db.execute("DELETE FROM file_metadata WHERE path=? OR path LIKE ?",
                     (rel_path, rel_path + "/%"))
    await db.commit()
    return True


async def list_trash() -> list:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM recycle_bin ORDER BY deleted_at DESC"
    )
    return [dict(r) for r in rows]


async def restore_from_trash(item_id: int) -> bool:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM recycle_bin WHERE id=?", (item_id,))
    if not rows:
        return False

    r = rows[0]
    trash_fp = DOWNLOAD_DIR / r["trash_path"]
    orig_fp = DOWNLOAD_DIR / r["original_path"]

    if not trash_fp.exists():
        await db.execute("DELETE FROM recycle_bin WHERE id=?", (item_id,))
        await db.commit()
        return False

    # Restore
    import shutil
    orig_fp.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(trash_fp), str(orig_fp))
    await db.execute("DELETE FROM recycle_bin WHERE id=?", (item_id,))
    await db.commit()

    # Re-index
    if orig_fp.is_file():
        await index_file(orig_fp)
    return True


async def purge_trash(item_id: int = None):
    """Permanently delete from trash."""
    import shutil
    db = await get_db()

    if item_id:
        rows = await db.execute_fetchall("SELECT * FROM recycle_bin WHERE id=?", (item_id,))
    else:
        rows = await db.execute_fetchall("SELECT * FROM recycle_bin")

    for r in rows:
        tp = DOWNLOAD_DIR / r["trash_path"]
        if tp.exists():
            if tp.is_dir():
                shutil.rmtree(tp)
            else:
                tp.unlink()
        await db.execute("DELETE FROM recycle_bin WHERE id=?", (r["id"],))

    await db.commit()


async def auto_purge_expired():
    """Remove expired trash items."""
    import shutil
    db = await get_db()
    now = datetime.now().isoformat()
    rows = await db.execute_fetchall(
        "SELECT * FROM recycle_bin WHERE expires_at < ?", (now,)
    )
    for r in rows:
        tp = DOWNLOAD_DIR / r["trash_path"]
        if tp.exists():
            if tp.is_dir():
                shutil.rmtree(tp)
            else:
                tp.unlink()
        await db.execute("DELETE FROM recycle_bin WHERE id=?", (r["id"],))
    await db.commit()


# --- Versioning ---
async def create_version(rel_path: str):
    """Save current version before overwrite."""
    fp = DOWNLOAD_DIR / rel_path
    if not fp.exists() or not fp.is_file():
        return

    import shutil
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    db = await get_db()
    # Get current version number
    rows = await db.execute_fetchall(
        "SELECT MAX(version) as max_v FROM file_versions WHERE path=?", (rel_path,)
    )
    next_v = (rows[0]["max_v"] or 0) + 1

    # Save version
    ver_dir = VERSIONS_DIR / rel_path.replace("/", "_")
    ver_dir.mkdir(parents=True, exist_ok=True)
    ver_path = ver_dir / f"v{next_v}_{fp.name}"
    shutil.copy2(str(fp), str(ver_path))

    md5 = compute_md5(ver_path)
    await db.execute("""
        INSERT INTO file_versions (path, version_path, version, size, hash_md5, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (rel_path, str(ver_path.relative_to(DOWNLOAD_DIR)),
          next_v, fp.stat().st_size, md5, datetime.now().isoformat()))
    await db.commit()

    # Prune old versions
    rows = await db.execute_fetchall(
        "SELECT * FROM file_versions WHERE path=? ORDER BY version DESC", (rel_path,)
    )
    if len(rows) > MAX_VERSIONS:
        for old in rows[MAX_VERSIONS:]:
            old_fp = DOWNLOAD_DIR / old["version_path"]
            if old_fp.exists():
                old_fp.unlink()
            await db.execute("DELETE FROM file_versions WHERE id=?", (old["id"],))
        await db.commit()


async def list_versions(rel_path: str) -> list:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM file_versions WHERE path=? ORDER BY version DESC", (rel_path,)
    )
    return [dict(r) for r in rows]


async def restore_version(rel_path: str, version: int) -> bool:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM file_versions WHERE path=? AND version=?", (rel_path, version)
    )
    if not rows:
        return False

    import shutil
    r = rows[0]
    ver_fp = DOWNLOAD_DIR / r["version_path"]
    target_fp = DOWNLOAD_DIR / rel_path

    if not ver_fp.exists():
        return False

    # Save current as new version before restoring
    if target_fp.exists():
        await create_version(rel_path)

    shutil.copy2(str(ver_fp), str(target_fp))
    await index_file(target_fp)
    return True


def list_dir_items(target: Path, base: Path, downloading: set = None) -> list:
    """List directory contents with metadata. Filters out active downloads and .aria2 files."""
    items = []
    downloading = downloading or set()
    for f in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if f.name.startswith("."):
            continue
        if f.name in SYSTEM_DIRS:
            continue
        # Hide .aria2 control files
        if f.name.endswith(".aria2"):
            continue
        # Hide files that are actively downloading
        if f.is_file() and f.name in downloading:
            continue

        stat = f.stat()
        if f.is_dir():
            count = sum(1 for x in f.iterdir() if not x.name.startswith(".") and x.name not in SYSTEM_DIRS)
            size = sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
            items.append({
                "name": f.name, "type": "folder", "size": size,
                "size_human": human_size(size), "items": count,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "path": str(f.relative_to(base))
            })
        else:
            ft = file_type(f.name)
            rel = str(f.relative_to(base))
            mime = mimetypes.guess_type(f.name)[0] or ""
            items.append({
                "name": f.name, "type": ft, "size": stat.st_size,
                "size_human": human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "path": rel,
                "mime_type": mime,
                "download_url": f"{SERVER_URL}/files/{rel}",
                "stream_url": f"{SERVER_URL}/stream/{rel}" if ft in ("video", "audio") else None,
                "ext": f.suffix.lower()
            })
    return items


# --- Copy ---
async def copy_item(rel_path: str, dest_path: str = "") -> dict:
    """Copy a file or folder. If dest is empty, copy to same dir with ' (copy)' suffix."""
    import shutil
    fp = DOWNLOAD_DIR / rel_path
    if not fp.exists():
        return {"success": False, "error": "Source not found"}

    if dest_path:
        dest = DOWNLOAD_DIR / dest_path / fp.name
    else:
        # Auto-name: file (copy).ext or file (copy 2).ext
        stem, ext = fp.stem, fp.suffix
        parent = fp.parent
        dest = parent / f"{stem} (copy){ext}"
        i = 2
        while dest.exists():
            dest = parent / f"{stem} (copy {i}){ext}"
            i += 1

    if not str(dest.resolve()).startswith(str(DOWNLOAD_DIR.resolve())):
        return {"success": False, "error": "Access denied"}

    dest.parent.mkdir(parents=True, exist_ok=True)
    if fp.is_dir():
        shutil.copytree(str(fp), str(dest))
    else:
        shutil.copy2(str(fp), str(dest))
        await index_file(dest)

    return {
        "success": True,
        "new_path": str(dest.relative_to(DOWNLOAD_DIR)),
        "new_name": dest.name
    }


# --- Text File Read/Edit ---
def read_text_file(rel_path: str) -> dict:
    """Read text file content with encoding detection."""
    fp = DOWNLOAD_DIR / rel_path
    if not fp.exists():
        return {"error": "File not found"}

    sz = fp.stat().st_size
    if sz > 1024 * 1024:  # 1MB limit
        return {"error": "File too large to edit (max 1MB)", "size": sz}

    ext = fp.suffix.lower()
    if ext not in TEXT_EXTS and not _is_likely_text(fp):
        return {"error": f"Not a text file: {ext}"}

    # Try common encodings
    content = None
    encoding = "utf-8"
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252", "shift_jis"]:
        try:
            content = fp.read_text(encoding=enc)
            encoding = enc
            break
        except (UnicodeDecodeError, ValueError):
            continue

    if content is None:
        return {"error": "Cannot decode file"}

    # Detect language for syntax highlighting
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".html": "html", ".css": "css", ".json": "json",
        ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
        ".md": "markdown", ".sh": "bash", ".bash": "bash",
        ".sql": "sql", ".go": "go", ".rs": "rust",
        ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
        ".rb": "ruby", ".php": "php", ".jsx": "jsx", ".tsx": "tsx",
        ".toml": "toml", ".ini": "ini", ".conf": "nginx",
    }

    return {
        "content": content,
        "encoding": encoding,
        "size": sz,
        "language": lang_map.get(ext, "text"),
        "lines": content.count("\n") + 1,
        "writable": True
    }


async def save_text_file(rel_path: str, content: str) -> dict:
    """Save text content to file. Auto-versions before overwrite."""
    fp = DOWNLOAD_DIR / rel_path
    if not str(fp.resolve()).startswith(str(DOWNLOAD_DIR.resolve())):
        return {"error": "Access denied"}

    # Version existing file
    if fp.exists():
        await create_version(rel_path)

    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    await index_file(fp)

    return {"success": True, "size": fp.stat().st_size, "path": rel_path}


def _is_likely_text(fp: Path) -> bool:
    """Heuristic: check if first 8KB contains mostly printable chars."""
    try:
        with open(fp, "rb") as f:
            chunk = f.read(8192)
        if not chunk:
            return True
        # If >10% non-text bytes, probably binary
        text_chars = set(range(32, 127)) | {9, 10, 13}  # printable + tab/newline/cr
        non_text = sum(1 for b in chunk if b not in text_chars)
        return non_text / len(chunk) < 0.1
    except Exception:
        return False

