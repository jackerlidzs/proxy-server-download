"""
Configuration & Constants - Download Proxy + Media Server v5.0
Auto-detects server resources for adaptive performance
"""
import os
from pathlib import Path

try:
    import psutil
    RAM_GB = psutil.virtual_memory().total / (1024**3)
except ImportError:
    RAM_GB = 2.0  # fallback

# Auto-detect server resources
CPU_CORES = os.cpu_count() or 2

# Adaptive FFmpeg settings based on CPU cores
if CPU_CORES >= 8:
    FFMPEG_THREADS = 6
    FFMPEG_NICE = 5
    FFMPEG_PRESET = 'medium'
    MAX_CONCURRENT_TRANSCODE = 3
elif CPU_CORES >= 4:
    FFMPEG_THREADS = 4
    FFMPEG_NICE = 10
    FFMPEG_PRESET = 'fast'
    MAX_CONCURRENT_TRANSCODE = 2
else:
    # 2 core (current server)
    FFMPEG_THREADS = 2
    FFMPEG_NICE = 19
    FFMPEG_PRESET = 'ultrafast'
    MAX_CONCURRENT_TRANSCODE = 1

# ETA multipliers (ratio of encode time to video duration)
ETA_COPY = 0.02         # copy streams: ~2% of video duration
ETA_AUDIO_ONLY = 0.05   # re-encode audio only: ~5%
ETA_REENCODE = {
    'ultrafast': 0.20,  # 2 core: 1h video → ~12 min
    'fast': 0.35,       # 4 core: 1h video → ~21 min
    'medium': 0.60,     # 8 core: 1h video → ~36 min
}

# Directories
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
TRASH_DIR = DOWNLOAD_DIR / ".trash"
VERSIONS_DIR = DOWNLOAD_DIR / ".versions"
HLS_DIR = DOWNLOAD_DIR / ".hls"
REMUX_DIR = DOWNLOAD_DIR / ".remux"
THUMBNAILS_DIR = DOWNLOAD_DIR / ".thumbnails"
TEMP_DIR = DOWNLOAD_DIR / "temp"
DB_PATH = Path(os.getenv("DB_PATH", "/data/proxy.db"))

# Auth
API_KEY = os.getenv("API_KEY", "changeme")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

# Downloads
MAX_CONNECTIONS = int(os.getenv("MAX_CONNECTIONS", "16"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))
CLEANUP_HOURS = int(os.getenv("CLEANUP_HOURS", "48"))
SERVER_URL = os.getenv("SERVER_URL", "http://localhost")

# Recycle bin
TRASH_EXPIRE_DAYS = int(os.getenv("TRASH_EXPIRE_DAYS", "7"))

# Versioning
MAX_VERSIONS = int(os.getenv("MAX_VERSIONS", "5"))

# Resource limits
MAX_HASH_CHUNK = 1024 * 1024  # 1MB chunks for hashing
MAX_UPLOAD_SIZE = 10 * 1024 * 1024 * 1024  # 10GB
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB chunks for upload
MAX_CONCURRENT_EXTRACT = 1  # Only 1 extraction at a time

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
SYSTEM_DIRS = {".trash", ".versions", ".hls", ".remux", ".thumbnails", "temp"}
