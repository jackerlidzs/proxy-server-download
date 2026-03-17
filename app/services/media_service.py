"""
Media Service - media metadata extraction, subtitle handling, HLS transcoding
FFmpeg/ffprobe based for 2-core CPU (no GPU)
"""
import re
import json
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime

from config import DOWNLOAD_DIR, HLS_DIR, VIDEO_EXTS, AUDIO_EXTS, SUBTITLE_EXTS, SERVER_URL, SYSTEM_DIRS, MAX_CONCURRENT_TRANSCODE

transcode_semaphore: asyncio.Semaphore = None
_active_transcodes: dict = {}

# HLS profiles optimized for 2-core Xeon (software encoding only)
HLS_PROFILES = [
    {"name": "480p", "height": 480, "bitrate": "1200k", "audio_br": "96k"},
    {"name": "720p", "height": 720, "bitrate": "2500k", "audio_br": "128k"},
    {"name": "1080p", "height": 1080, "bitrate": "3500k", "audio_br": "192k"},
]


def init_transcode_semaphore():
    global transcode_semaphore
    transcode_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSCODE)


async def get_media_info(filepath: Path) -> dict:
    """Get media metadata using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(filepath)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return {}

        data = json.loads(out.decode(errors="ignore"))
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

        info = {
            "duration": float(fmt.get("duration", 0)),
            "duration_human": _format_duration(float(fmt.get("duration", 0))),
            "bitrate": int(fmt.get("bit_rate", 0)),
            "format": fmt.get("format_name", ""),
        }

        if video_streams:
            vs = video_streams[0]
            info.update({
                "video_codec": vs.get("codec_name", ""),
                "width": int(vs.get("width", 0)),
                "height": int(vs.get("height", 0)),
                "resolution": f"{vs.get('width', '?')}x{vs.get('height', '?')}",
                "fps": _parse_fps(vs.get("avg_frame_rate", "0/1")),
            })

        if audio_streams:
            aus = audio_streams[0]
            info.update({
                "audio_codec": aus.get("codec_name", ""),
                "audio_channels": int(aus.get("channels", 0)),
                "audio_sample_rate": int(aus.get("sample_rate", 0)),
            })

        info["embedded_subtitles"] = len(sub_streams)
        info["subtitle_languages"] = [
            s.get("tags", {}).get("language", f"track_{i}")
            for i, s in enumerate(sub_streams)
        ]

        return info
    except Exception:
        return {}


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_fps(fps_str: str) -> float:
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return round(int(num) / int(den), 2) if int(den) > 0 else 0
        return round(float(fps_str), 2)
    except:
        return 0


async def generate_thumbnail(filepath: Path, output: Path = None, time_offset: int = 30) -> Path:
    """Generate a thumbnail from a video file."""
    if output is None:
        output = filepath.parent / f".thumb_{filepath.stem}.jpg"
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", str(time_offset), "-i", str(filepath),
            "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "5",
            str(output)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if output.exists():
            return output
    except Exception:
        pass
    return None


async def extract_subtitles(filepath: Path, track: int = 0) -> Path:
    """Extract embedded subtitle track to VTT."""
    out = filepath.parent / f"{filepath.stem}.track{track}.vtt"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-map", f"0:s:{track}", "-c:s", "webvtt", str(out)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if out.exists():
            return out
    except Exception:
        pass
    return None


async def convert_srt_to_vtt(srt_path: Path) -> Path:
    """Convert SRT subtitle to WebVTT format."""
    vtt_path = srt_path.with_suffix(".vtt")
    try:
        cmd = ["ffmpeg", "-y", "-i", str(srt_path), str(vtt_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if vtt_path.exists():
            return vtt_path
    except Exception:
        pass
    return None


def list_media(base_dir: Path = None) -> dict:
    """List all media files with subtitle matching."""
    base_dir = base_dir or DOWNLOAD_DIR
    media, subs = [], []

    if not base_dir.exists():
        return {"media": [], "total": 0}

    for f in sorted(base_dir.rglob("*")):
        if f.is_file() and not f.name.startswith(".") and not any(sd in f.parts for sd in SYSTEM_DIRS):
            ext = f.suffix.lower()
            if ext in VIDEO_EXTS | AUDIO_EXTS:
                rel = str(f.relative_to(base_dir))
                media.append({
                    "filename": f.name, "path": rel,
                    "size": f.stat().st_size,
                    "size_human": _human_size(f.stat().st_size),
                    "stream_url": f"{SERVER_URL}/stream/{rel}",
                    "download_url": f"{SERVER_URL}/files/{rel}",
                    "ext": ext,
                    "type": "video" if ext in VIDEO_EXTS else "audio",
                    "subtitles": []
                })
            elif ext in SUBTITLE_EXTS:
                subs.append({
                    "filename": f.name,
                    "path": str(f.relative_to(base_dir)),
                    "ext": ext
                })

    # Match subtitles to media
    for m in media:
        base = Path(m["filename"]).stem.lower()
        for s in subs:
            sbase = Path(s["filename"]).stem.lower()
            if sbase.startswith(base) or base.startswith(sbase.rsplit(".", 1)[0]):
                m["subtitles"].append({
                    "filename": s["filename"],
                    "url": f"{SERVER_URL}/files/{s['path']}",
                    "ext": s["ext"]
                })

    return {"media": media, "total": len(media)}


def _human_size(b):
    if not b: return "0 B"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# ===== HLS Transcoding =====

def _video_hash(filepath: Path) -> str:
    """Quick hash based on filename + size + mtime for cache keying."""
    stat = filepath.stat()
    key = f"{filepath.name}:{stat.st_size}:{stat.st_mtime}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def get_hls_dir(filepath: Path) -> Path:
    """Get HLS output directory for a video."""
    return HLS_DIR / _video_hash(filepath)


def is_hls_ready(filepath: Path) -> bool:
    """Check if HLS segments already exist and are complete."""
    hls_dir = get_hls_dir(filepath)
    master = hls_dir / "master.m3u8"
    return master.exists()


def get_hls_status(filepath: Path) -> dict:
    """Get HLS transcoding status."""
    vid_hash = _video_hash(filepath)
    hls_dir = HLS_DIR / vid_hash

    if is_hls_ready(filepath):
        return {
            "status": "ready",
            "master_url": f"{SERVER_URL}/hls/{vid_hash}/master.m3u8",
            "profiles": [p["name"] for p in HLS_PROFILES]
        }

    if vid_hash in _active_transcodes:
        return {"status": "transcoding", "progress": _active_transcodes[vid_hash]}

    return {"status": "not_started"}


async def transcode_to_hls(filepath: Path) -> dict:
    """Transcode video to HLS with multiple quality profiles."""
    vid_hash = _video_hash(filepath)
    hls_dir = HLS_DIR / vid_hash

    # Already done
    if is_hls_ready(filepath):
        return {"status": "ready", "master_url": f"{SERVER_URL}/hls/{vid_hash}/master.m3u8"}

    # Already in progress
    if vid_hash in _active_transcodes:
        return {"status": "transcoding", "progress": _active_transcodes[vid_hash]}

    # Get source info
    info = await get_media_info(filepath)
    src_height = info.get("height", 1080)

    # Filter profiles that are <= source resolution
    profiles = [p for p in HLS_PROFILES if p["height"] <= src_height]
    if not profiles:
        profiles = [HLS_PROFILES[0]]  # At least 480p

    _active_transcodes[vid_hash] = {"percent": 0, "profile": "starting"}

    # Run in background with semaphore
    asyncio.create_task(_do_hls_transcode(filepath, hls_dir, profiles, vid_hash, info))

    return {"status": "started", "profiles": [p["name"] for p in profiles]}


async def _do_hls_transcode(filepath: Path, hls_dir: Path, profiles: list, vid_hash: str, info: dict):
    """Background HLS transcoding task."""
    async with transcode_semaphore:
        try:
            hls_dir.mkdir(parents=True, exist_ok=True)
            duration = info.get("duration", 0)

            for i, profile in enumerate(profiles):
                pname = profile["name"]
                pdir = hls_dir / pname
                pdir.mkdir(exist_ok=True)

                _active_transcodes[vid_hash] = {
                    "percent": int(i / len(profiles) * 100),
                    "profile": pname
                }

                cmd = [
                    "ffmpeg", "-y",
                    "-threads", "2",
                    "-i", str(filepath),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264",
                    "-preset", "superfast",
                    "-tune", "film",
                    "-vf", f"scale=-2:{profile['height']}",
                    "-b:v", profile["bitrate"],
                    "-maxrate", profile["bitrate"],
                    "-bufsize", f"{int(profile['bitrate'].replace('k',''))}k",
                    "-c:a", "aac", "-b:a", profile["audio_br"],
                    "-ac", "2",
                    "-f", "hls",
                    "-hls_time", "6",
                    "-hls_list_size", "0",
                    "-hls_segment_filename", str(pdir / "seg_%04d.ts"),
                    "-hls_playlist_type", "vod",
                    str(pdir / "index.m3u8")
                ]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                # Read stderr for progress
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    dec = line.decode(errors="ignore")
                    # Parse FFmpeg time progress
                    m = re.search(r'time=(\d+):(\d+):(\d+)', dec)
                    if m and duration > 0:
                        elapsed = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                        profile_pct = min(99, int(elapsed / duration * 100))
                        overall = int((i * 100 + profile_pct) / len(profiles))
                        _active_transcodes[vid_hash] = {"percent": overall, "profile": pname}

                await proc.wait()
                if proc.returncode != 0:
                    _active_transcodes[vid_hash] = {"percent": -1, "profile": pname, "error": "FFmpeg failed"}
                    return

            # Generate master playlist
            _generate_master_playlist(hls_dir, profiles)
            _active_transcodes.pop(vid_hash, None)

        except Exception as e:
            _active_transcodes[vid_hash] = {"percent": -1, "error": str(e)}


def _generate_master_playlist(hls_dir: Path, profiles: list):
    """Generate HLS master.m3u8 with all quality profiles."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    bandwidth_map = {"480p": 1500000, "720p": 3000000, "1080p": 5000000}
    resolution_map = {"480p": "854x480", "720p": "1280x720", "1080p": "1920x1080"}

    for p in profiles:
        name = p["name"]
        bw = bandwidth_map.get(name, 1500000)
        res = resolution_map.get(name, "854x480")
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={res},NAME="{name}"')
        lines.append(f"{name}/index.m3u8")
        lines.append("")

    with open(hls_dir / "master.m3u8", "w") as f:
        f.write("\n".join(lines))


async def cleanup_hls(filepath: Path):
    """Remove cached HLS segments for a video."""
    import shutil
    hls_dir = get_hls_dir(filepath)
    if hls_dir.exists():
        shutil.rmtree(hls_dir)

