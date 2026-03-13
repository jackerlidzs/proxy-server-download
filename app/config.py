"""
Configuration & Constants - Download Proxy + Media Server v5.0
Optimized for: 2-core Xeon, 1.9GB RAM, Debian 12
"""
import os
from pathlib import Path

# Directories
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
TRASH_DIR = DOWNLOAD_DIR / ".trash"
VERSIONS_DIR = DOWNLOAD_DIR / ".versions"
HLS_DIR = DOWNLOAD_DIR / ".hls"
DB_PATH = Path(os.getenv("DB_PATH", "/data/proxy.db"))

# Auth
API_KEY = os.getenv("API_KEY", "changeme")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

# Downloads
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "16"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "48"))
SERVER_URL = os.getenv("SERVER_URL", "http://localhost")

# Recycle bin
TRASH_EXPIRE_DAYS = int(os.getenv("TRASH_EXPIRE_DAYS", "7"))

# Versioning
MAX_VERSIONS = int(os.getenv("MAX_VERSIONS", "5"))

# Resource limits (for 1.9GB RAM server)
MAX_HASH_CHUNK = 1024 * 1024  # 1MB chunks for hashing
MAX_UPLOAD_SIZE = 10 * 1024 * 1024 * 1024  # 10GB
MAX_CONCURRENT_EXTRACT = 1  # Only 1 extraction at a time
MAX_CONCURRENT_TRANSCODE = 1  # Only 1 transcode at a time

# File type extensions
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
AUDIO_EXTS = {".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a"}
ARCHIVE_EXTS = {".rar", ".zip", ".7z", ".tar", ".gz", ".tar.gz", ".tgz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
TEXT_EXTS = {
    ".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".env", ".toml",
    ".py", ".js", ".ts", ".html", ".css", ".jsx", ".tsx",
    ".sh", ".bash", ".bat", ".ps1",
    ".c", ".cpp", ".h", ".java", ".go", ".rs", ".rb", ".php",
    ".sql", ".dockerfile", ".makefile", ".gitignore",
    ".srt", ".vtt", ".ass", ".ssa", ".nfo",
}

# Hidden system directories
SYSTEM_DIRS = {".trash", ".versions", ".hls"}
