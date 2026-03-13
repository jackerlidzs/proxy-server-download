"""
SQLite Database - async with aiosqlite
Tables: file_metadata, recycle_bin, file_versions, users, share_links, activity_log
"""
import aiosqlite
from config import DB_PATH

_db: aiosqlite.Connection = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA synchronous=NORMAL")
        await _db.execute("PRAGMA cache_size=-8000")  # 8MB cache
        await _db.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s if locked
        await migrate(_db)
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


async def migrate(db: aiosqlite.Connection):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS file_metadata (
            path TEXT PRIMARY KEY,
            mime_type TEXT,
            size INTEGER,
            hash_md5 TEXT,
            created_at TEXT,
            modified_at TEXT,
            tags TEXT DEFAULT '[]',
            description TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS recycle_bin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_path TEXT NOT NULL,
            trash_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            size INTEGER,
            deleted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            version_path TEXT NOT NULL,
            version INTEGER NOT NULL,
            size INTEGER,
            hash_md5 TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            quota_bytes INTEGER DEFAULT 0,
            used_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS share_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            file_path TEXT NOT NULL,
            created_by TEXT,
            password_hash TEXT,
            expires_at TEXT,
            max_downloads INTEGER DEFAULT 0,
            download_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            username TEXT,
            detail TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_recycle_expires ON recycle_bin(expires_at);
        CREATE INDEX IF NOT EXISTS idx_versions_path ON file_versions(path);
        CREATE INDEX IF NOT EXISTS idx_share_token ON share_links(token);
        CREATE INDEX IF NOT EXISTS idx_metadata_hash ON file_metadata(hash_md5);
        CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_log(created_at);
    """)

    # Auto-migration: add columns that may not exist on older DBs
    for col_check in [
        ("users", "used_bytes", "ALTER TABLE users ADD COLUMN used_bytes INTEGER DEFAULT 0"),
    ]:
        try:
            await db.execute(col_check[2])
        except Exception:
            pass  # Column already exists

    await db.commit()


async def log_activity(action: str, username: str = "", detail: str = ""):
    """Log an activity event."""
    db = await get_db()
    await db.execute(
        "INSERT INTO activity_log (action, username, detail) VALUES (?, ?, ?)",
        (action, username, detail)
    )
    await db.commit()

