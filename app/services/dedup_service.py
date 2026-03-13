"""
Deduplication Service - find and manage duplicate files
Uses streaming MD5 hash for memory efficiency
"""
import asyncio
from pathlib import Path
from datetime import datetime

from config import DOWNLOAD_DIR, SYSTEM_DIRS
from database import get_db
from services.file_service import compute_md5, human_size


async def scan_duplicates(base_dir: Path = None) -> dict:
    """Scan all files and find duplicates by MD5 hash."""
    base_dir = base_dir or DOWNLOAD_DIR
    db = await get_db()

    # First update all hashes
    file_list = []
    for f in base_dir.rglob("*"):
        if f.is_file() and not any(sd in f.parts for sd in SYSTEM_DIRS) and not f.name.startswith("."):
            file_list.append(f)

    # Hash all files that don't have hashes yet
    for fp in file_list:
        rel = str(fp.relative_to(base_dir))
        row = await db.execute_fetchall("SELECT hash_md5 FROM file_metadata WHERE path=?", (rel,))
        if not row or not row[0]["hash_md5"]:
            try:
                md5 = compute_md5(fp)
                await db.execute("""
                    INSERT OR REPLACE INTO file_metadata (path, mime_type, size, hash_md5, modified_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (rel, "", fp.stat().st_size, md5,
                      datetime.fromtimestamp(fp.stat().st_mtime).isoformat()))
            except Exception:
                pass

    await db.commit()

    # Find duplicates
    rows = await db.execute_fetchall("""
        SELECT hash_md5, GROUP_CONCAT(path, '|||') as paths, COUNT(*) as cnt, size
        FROM file_metadata
        WHERE hash_md5 IS NOT NULL AND hash_md5 != ''
        GROUP BY hash_md5
        HAVING cnt > 1
        ORDER BY size DESC
    """)

    groups = []
    total_wasted = 0
    for r in rows:
        paths = r["paths"].split("|||")
        size = r["size"] or 0
        wasted = size * (len(paths) - 1)
        total_wasted += wasted
        groups.append({
            "hash": r["hash_md5"],
            "files": paths,
            "count": r["cnt"],
            "size": size,
            "size_human": human_size(size),
            "wasted_space": wasted,
            "wasted_human": human_size(wasted)
        })

    return {
        "duplicate_groups": groups,
        "total_groups": len(groups),
        "total_wasted": total_wasted,
        "total_wasted_human": human_size(total_wasted)
    }


async def clean_duplicates(keep_strategy: str = "first") -> dict:
    """Remove duplicate files, keeping one copy.
    keep_strategy: 'first' (keep shortest path), 'newest' (keep newest)
    """
    import os
    base_dir = DOWNLOAD_DIR
    scan = await scan_duplicates(base_dir)
    deleted = []
    freed = 0

    db = await get_db()
    for group in scan["duplicate_groups"]:
        paths = group["files"]
        if len(paths) < 2:
            continue

        # Decide which to keep
        if keep_strategy == "newest":
            # Keep the one with latest modification time
            paths_with_mtime = []
            for p in paths:
                fp = base_dir / p
                if fp.exists():
                    paths_with_mtime.append((p, fp.stat().st_mtime))
            paths_with_mtime.sort(key=lambda x: x[1], reverse=True)
            keep = paths_with_mtime[0][0] if paths_with_mtime else paths[0]
        else:
            # Keep shortest path (usually the "original")
            keep = min(paths, key=len)

        for p in paths:
            if p != keep:
                fp = base_dir / p
                if fp.exists():
                    size = fp.stat().st_size
                    fp.unlink()
                    freed += size
                    deleted.append(p)
                    await db.execute("DELETE FROM file_metadata WHERE path=?", (p,))

    await db.commit()

    return {
        "deleted_files": deleted,
        "deleted_count": len(deleted),
        "freed_space": freed,
        "freed_human": human_size(freed)
    }
