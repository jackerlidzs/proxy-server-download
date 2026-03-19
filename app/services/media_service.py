"""
Media Service - media metadata extraction, subtitle handling, HLS transcoding
FFmpeg/ffprobe based, adaptive to server specs
"""
import re
import os
import json
import math
import time
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime

from config import (
    DOWNLOAD_DIR, HLS_DIR, REMUX_DIR, THUMBNAILS_DIR,
    VIDEO_EXTS, AUDIO_EXTS, SUBTITLE_EXTS, SERVER_URL, SYSTEM_DIRS,
    MAX_CONCURRENT_TRANSCODE, CPU_CORES,
    FFMPEG_THREADS, FFMPEG_NICE, FFMPEG_PRESET,
    ETA_COPY, ETA_AUDIO_ONLY, ETA_REENCODE,
)

transcode_semaphore: asyncio.Semaphore = None
_active_transcodes: dict = {}

# Video codecs that can be COPIED directly into HLS (H.264 only truly safe)
COPY_VIDEO_CODECS = {'h264', 'avc', 'avc1'}

# Video codecs that need RE-ENCODE to H.264
REENCODE_VIDEO_CODECS = {
    'hevc', 'h265', 'hvc1',        # H.265/HEVC
    'av1',                          # AV1
    'vp9', 'vp8',                   # WebM
    'mpeg4', 'xvid', 'divx',       # DivX/Xvid
    'mpeg2video', 'mpeg1video',    # DVD/broadcast
    'wmv1', 'wmv2', 'wmv3',        # Windows Media
    'vc1',                          # VC-1 (Blu-ray)
    'theora',                       # Ogg/WebM legacy
    'prores', 'dnxhd',             # Editing formats
    'flv1', 'sorenson',            # FLV legacy
    'rv40', 'rv30',                # RealMedia
}

# Audio codecs that can be COPIED
COPY_AUDIO_CODECS = {'aac', 'mp3', 'mp3float', 'opus'}

# Audio codecs that need RE-ENCODE to AAC
REENCODE_AUDIO_CODECS = {
    'eac3', 'ac3',                  # Dolby Digital/Plus
    'dts', 'dca',                   # DTS
    'truehd', 'mlp',               # Dolby TrueHD (Blu-ray)
    'dtshd', 'dts-hd',            # DTS-HD Master Audio
    'flac', 'alac',                # Lossless
    'pcm_s16le', 'pcm_s24le',     # PCM (DVD)
    'pcm_s32le', 'pcm_f32le',
    'pcm_bluray', 'pcm_dvd',
    'pcm_u8',
    'wma', 'wmav1', 'wmav2',      # Windows Media Audio
    'vorbis',                       # Ogg Vorbis
    'mp2', 'mp2float',             # MPEG-1 Audio Layer 2
    'amr_nb', 'amr_wb',           # Mobile audio
    'aiff',                         # Apple AIFF
    'ra_144', 'ra_288',            # RealAudio
}

# HLS profiles optimized per server tier
HLS_PROFILES = [
    {"name": "480p", "height": 480, "bitrate": "1200k", "audio_br": "96k"},
    {"name": "720p", "height": 720, "bitrate": "2500k", "audio_br": "128k"},
    {"name": "1080p", "height": 1080, "bitrate": "3500k", "audio_br": "192k"},
]


def init_transcode_semaphore():
    global transcode_semaphore
    transcode_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSCODE)


def determine_convert_strategy(media_info: dict) -> dict:
    """Determine the optimal HLS conversion strategy based on codecs.
    Handles all common container/codec combinations.
    """
    video_codec = media_info.get("video_codec", "").lower()
    audio_codec = media_info.get("audio_codec", "").lower()
    pix_fmt = media_info.get("pix_fmt", "yuv420p").lower()
    duration = media_info.get("duration", 0)  # seconds

    # Determine video action
    if video_codec in COPY_VIDEO_CODECS:
        video_action = 'copy'
    else:
        # Any codec not in COPY set needs re-encode (known or unknown)
        video_action = 're-encode'

    # Determine audio action
    if audio_codec in COPY_AUDIO_CODECS:
        audio_action = 'copy'
    elif audio_codec == '':
        audio_action = 'copy'  # no audio stream
    else:
        audio_action = 're-encode'

    # Check for HDR (10-bit) content
    is_hdr = '10le' in pix_fmt or '10be' in pix_fmt or 'p010' in pix_fmt

    # Overall strategy
    if video_action == 'copy' and audio_action == 'copy':
        strategy = 'copy'
        eta = duration / 60 * ETA_COPY
    elif video_action == 'copy' and audio_action == 're-encode':
        strategy = 'audio_only'
        eta = duration / 60 * ETA_AUDIO_ONLY
    else:
        strategy = 're-encode'
        multiplier = ETA_REENCODE.get(FFMPEG_PRESET, 0.35)
        if is_hdr:
            multiplier *= 1.3  # HDR tone mapping adds ~30% time
        eta = duration / 60 * multiplier

    return {
        'strategy': strategy,
        'eta_minutes': max(1, round(eta)),
        'video_action': video_action,
        'audio_action': audio_action,
        'video_codec': video_codec,
        'audio_codec': audio_codec,
        'pix_fmt': pix_fmt,
        'is_hdr': is_hdr,
    }


def build_hls_command(input_path: Path, output_dir: Path, strategy_info: dict,
                      profile: dict = None) -> list:
    """Build FFmpeg command for HLS conversion based on strategy.
    Handles HDR tone mapping, all codec types, and adaptive presets.
    """
    cmd = []
    if os.name != 'nt':
        cmd += ['nice', '-n', str(FFMPEG_NICE)]

    cmd += [
        'ffmpeg', '-y',
        '-threads', str(FFMPEG_THREADS),
        '-i', str(input_path),
        '-map', '0:v:0',     # first video stream only
        '-map', '0:a:0?',    # first audio stream only
        '-sn',               # strip subtitle streams
        '-dn',               # strip data streams
    ]

    strategy = strategy_info['strategy']
    is_hdr = strategy_info.get('is_hdr', False)

    if strategy == 'copy':
        cmd += ['-c:v', 'copy', '-c:a', 'copy']

    elif strategy == 'audio_only':
        cmd += [
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-ar', '48000',
        ]

    else:
        # Full re-encode to H.264 + AAC
        # Video filter chain
        vf_parts = []

        if is_hdr:
            # HDR → SDR tone mapping for browser compatibility
            vf_parts.append(
                'zscale=t=linear:npl=100,format=gbrpf32le,'
                'zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
                'zscale=t=bt709:m=bt709:r=tv,format=yuv420p'
            )

        if profile:
            vf_parts.append(f"scale=-2:{profile['height']}")

        encode_args = [
            '-c:v', 'libx264',
            '-preset', FFMPEG_PRESET,
            '-crf', '23',
            '-profile:v', 'high',
            '-level', '4.0',
        ]

        if vf_parts:
            encode_args += ['-vf', ','.join(vf_parts)]

        if not is_hdr:
            encode_args += ['-pix_fmt', 'yuv420p']

        if profile:
            encode_args += [
                '-b:v', profile['bitrate'],
                '-maxrate', profile['bitrate'],
                '-bufsize', f"{int(profile['bitrate'].replace('k', ''))}k",
            ]

        encode_args += [
            '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-ar', '48000',
        ]
        cmd += encode_args

    # HLS output args
    cmd += [
        '-f', 'hls',
        '-hls_time', '6',
        '-hls_list_size', '0',
        '-hls_segment_filename', str(output_dir / 'seg_%04d.ts'),
        '-hls_playlist_type', 'vod',
        str(output_dir / 'index.m3u8'),
    ]

    return cmd


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
            pix_fmt = vs.get("pix_fmt", "yuv420p")
            info.update({
                "video_codec": vs.get("codec_name", ""),
                "width": int(vs.get("width", 0)),
                "height": int(vs.get("height", 0)),
                "resolution": f"{vs.get('width', '?')}x{vs.get('height', '?')}",
                "fps": _parse_fps(vs.get("avg_frame_rate", "0/1")),
                "pix_fmt": pix_fmt,
                "is_hdr": '10le' in pix_fmt or '10be' in pix_fmt or 'p010' in pix_fmt,
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


async def quick_probe_duration(filepath: Path) -> float:
    """Quick probe to get only duration (lightweight, no full metadata scan)."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_entries", "format=duration",
            str(filepath)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return 0
        data = json.loads(out.decode(errors="ignore"))
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
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
    """Get HLS transcoding status with strategy and ETA info."""
    vid_hash = _video_hash(filepath)
    hls_dir = HLS_DIR / vid_hash

    if is_hls_ready(filepath):
        return {
            "status": "ready",
            "master_url": f"{SERVER_URL}/hls/{vid_hash}/master.m3u8",
            "profiles": [p["name"] for p in HLS_PROFILES]
        }

    if vid_hash in _active_transcodes:
        info = _active_transcodes[vid_hash].copy()
        started_at = info.get("started_at", 0)
        elapsed_min = round((time.time() - started_at) / 60, 1) if started_at else 0

        return {
            "status": "transcoding",
            "progress": {"percent": info.get("percent", 0), "profile": info.get("profile", "")},
            "strategy": info.get("strategy", ""),
            "eta_minutes": info.get("eta_minutes", 0),
            "started_at": started_at,
            "elapsed_minutes": elapsed_min,
            "server_info": {
                "cpu_cores": CPU_CORES,
                "preset": FFMPEG_PRESET,
            }
        }

    return {"status": "not_started"}


async def transcode_to_hls(filepath: Path) -> dict:
    """Transcode video to HLS using smart strategy selection."""
    vid_hash = _video_hash(filepath)
    hls_dir = HLS_DIR / vid_hash

    # Already done
    if is_hls_ready(filepath):
        return {"status": "ready", "master_url": f"{SERVER_URL}/hls/{vid_hash}/master.m3u8"}

    # Already in progress
    if vid_hash in _active_transcodes:
        return {"status": "transcoding", "progress": _active_transcodes[vid_hash]}

    # Check if at capacity
    if transcode_semaphore and transcode_semaphore.locked() and MAX_CONCURRENT_TRANSCODE <= 1:
        return {
            "status": "queued",
            "message": "Server đang convert video khác. Sẽ tự động bắt đầu khi có slot.",
        }

    # Get source info and determine strategy
    info = await get_media_info(filepath)
    strategy_info = determine_convert_strategy(info)
    src_height = info.get("height", 1080)

    # For copy/audio_only: single output (no multi-profile needed)
    # For re-encode: multi-profile like before
    if strategy_info['strategy'] == 're-encode':
        profiles = [p for p in HLS_PROFILES if p["height"] <= src_height]
        if not profiles:
            profiles = [HLS_PROFILES[0]]
    else:
        profiles = None  # single output

    _active_transcodes[vid_hash] = {
        "percent": 0,
        "profile": "starting",
        "strategy": strategy_info['strategy'],
        "eta_minutes": strategy_info['eta_minutes'],
        "started_at": time.time(),
    }

    # Run in background with semaphore
    asyncio.create_task(
        _do_hls_transcode(filepath, hls_dir, profiles, vid_hash, info, strategy_info)
    )

    return {
        "status": "started",
        "strategy": strategy_info['strategy'],
        "eta_minutes": strategy_info['eta_minutes'],
        "profiles": [p["name"] for p in profiles] if profiles else ["original"],
        "server_info": {
            "cpu_cores": CPU_CORES,
            "preset": FFMPEG_PRESET,
            "nice_level": FFMPEG_NICE,
            "max_concurrent": MAX_CONCURRENT_TRANSCODE,
        }
    }


async def _do_hls_transcode(filepath: Path, hls_dir: Path, profiles: list,
                             vid_hash: str, info: dict, strategy_info: dict):
    """Background HLS transcoding task with smart strategy."""
    async with transcode_semaphore:
        try:
            hls_dir.mkdir(parents=True, exist_ok=True)
            duration = info.get("duration", 0)
            strategy = strategy_info['strategy']

            if strategy in ('copy', 'audio_only'):
                # Single-pass: output directly to hls_dir
                _active_transcodes[vid_hash].update({
                    "percent": 0,
                    "profile": "original",
                })

                cmd = build_hls_command(filepath, hls_dir, strategy_info)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    dec = line.decode(errors="ignore")
                    m = re.search(r'time=(\d+):(\d+):(\d+)', dec)
                    if m and duration > 0:
                        elapsed = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                        pct = min(99, int(elapsed / duration * 100))
                        _active_transcodes[vid_hash]["percent"] = pct

                await proc.wait()
                if proc.returncode != 0:
                    _active_transcodes[vid_hash] = {"percent": -1, "error": "FFmpeg failed", "strategy": strategy}
                    return

                # Generate simple master playlist pointing to index.m3u8
                _generate_single_master(hls_dir)

            else:
                # Multi-profile re-encode
                for i, profile in enumerate(profiles):
                    pname = profile["name"]
                    pdir = hls_dir / pname
                    pdir.mkdir(exist_ok=True)

                    _active_transcodes[vid_hash].update({
                        "percent": int(i / len(profiles) * 100),
                        "profile": pname,
                    })

                    cmd = build_hls_command(filepath, pdir, strategy_info, profile)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )

                    while True:
                        line = await proc.stderr.readline()
                        if not line:
                            break
                        dec = line.decode(errors="ignore")
                        m = re.search(r'time=(\d+):(\d+):(\d+)', dec)
                        if m and duration > 0:
                            elapsed = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                            profile_pct = min(99, int(elapsed / duration * 100))
                            overall = int((i * 100 + profile_pct) / len(profiles))
                            _active_transcodes[vid_hash].update({"percent": overall, "profile": pname})

                    await proc.wait()
                    if proc.returncode != 0:
                        _active_transcodes[vid_hash] = {"percent": -1, "profile": pname, "error": "FFmpeg failed", "strategy": strategy}
                        return

                # Generate multi-profile master playlist
                _generate_master_playlist(hls_dir, profiles)

            _active_transcodes.pop(vid_hash, None)

        except Exception as e:
            _active_transcodes[vid_hash] = {"percent": -1, "error": str(e), "strategy": strategy_info.get('strategy', '')}


def _generate_single_master(hls_dir: Path):
    """Generate a simple master.m3u8 pointing to single index.m3u8."""
    content = "#EXTM3U\n#EXT-X-VERSION:3\n\n"
    content += '#EXT-X-STREAM-INF:BANDWIDTH=5000000,NAME="original"\n'
    content += "index.m3u8\n"
    with open(hls_dir / "master.m3u8", "w") as f:
        f.write(content)


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


# ===== Remux to MP4 Faststart =====

_active_remuxes: dict = {}

# Browser-native containers that support seeking
BROWSER_NATIVE_CONTAINERS = {".mp4", ".m4v", ".webm"}
# Codecs browsers can play natively
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}


def get_remux_path(filepath: Path) -> Path:
    """Get the cached remuxed MP4 path for a video file."""
    vid_hash = _video_hash(filepath)
    REMUX_DIR.mkdir(parents=True, exist_ok=True)
    return REMUX_DIR / f"{vid_hash}_{filepath.stem}.mp4"


def get_remux_status(filepath: Path) -> dict:
    """Check remux status for a video file."""
    remux_path = get_remux_path(filepath)
    vid_hash = _video_hash(filepath)
    rel_remux = str(remux_path.relative_to(DOWNLOAD_DIR))

    if remux_path.exists() and remux_path.stat().st_size > 0:
        return {
            "status": "ready",
            "remux_url": f"{SERVER_URL}/stream/{rel_remux}",
            "path": str(remux_path),
            "size": remux_path.stat().st_size,
        }

    if vid_hash in _active_remuxes:
        return {"status": "remuxing", "progress": _active_remuxes[vid_hash]}

    return {"status": "not_started"}


async def check_needs_remux(filepath: Path) -> dict:
    """Check if a video file needs remuxing for browser playback.
    Returns info about whether remux is needed and why.
    """
    ext = filepath.suffix.lower()
    result = {
        "needs_remux": False,
        "reason": "",
        "container": ext,
        "codec": "unknown",
        "duration": 0,
    }

    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", "-select_streams", "v:0", str(filepath),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        info = json.loads(out.decode())
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        duration = float(fmt.get("duration", 0))

        if not duration and streams:
            duration = float(streams[0].get("duration", 0))
        result["duration"] = duration

        if not streams:
            return result

        codec = streams[0].get("codec_name", "").lower()
        result["codec"] = codec

        # Check 1: Non-native container (MKV, AVI, etc.)
        if ext not in BROWSER_NATIVE_CONTAINERS:
            if codec in BROWSER_VIDEO_CODECS:
                result["needs_remux"] = True
                result["reason"] = f"Container {ext} not browser-native, but codec {codec} is compatible — remux only"
            else:
                result["needs_remux"] = True
                result["reason"] = f"Container {ext} and codec {codec} not browser-compatible — needs transcode"
                result["needs_transcode"] = True
            return result

        # Check 2: MP4 with non-browser codec (HEVC etc.)
        if codec not in BROWSER_VIDEO_CODECS:
            result["needs_remux"] = True
            result["needs_transcode"] = True
            result["reason"] = f"Codec {codec} not browser-compatible — needs transcode"
            return result

        # Check 3: MP4 — check if moov atom is at start (faststart)
        # If file has faststart, browser can seek immediately
        # We detect this by checking if ffprobe can read duration quickly
        if duration > 0:
            result["needs_remux"] = False
            result["reason"] = "Browser-compatible container and codec with valid duration"
        else:
            result["needs_remux"] = True
            result["reason"] = "MP4 missing duration metadata or moov atom — needs faststart remux"

        return result
    except Exception as e:
        result["reason"] = f"Probe failed: {str(e)}"
        return result


async def remux_to_mp4(filepath: Path) -> dict:
    """Remux video to MP4 with faststart (no re-encoding, very fast).
    For files with incompatible codecs, full transcode is used instead.
    """
    vid_hash = _video_hash(filepath)
    remux_path = get_remux_path(filepath)

    # Already done
    if remux_path.exists() and remux_path.stat().st_size > 0:
        rel_remux = str(remux_path.relative_to(DOWNLOAD_DIR))
        return {"status": "ready", "remux_url": f"{SERVER_URL}/stream/{rel_remux}"}

    # Already in progress
    if vid_hash in _active_remuxes:
        return {"status": "remuxing", "progress": _active_remuxes[vid_hash]}

    # Check what kind of remux is needed
    check = await check_needs_remux(filepath)

    _active_remuxes[vid_hash] = {"percent": 0, "type": "starting"}
    REMUX_DIR.mkdir(parents=True, exist_ok=True)

    # Run in background
    asyncio.create_task(_do_remux(filepath, remux_path, vid_hash, check))

    return {"status": "started", "needs_transcode": check.get("needs_transcode", False)}


async def _do_remux(filepath: Path, remux_path: Path, vid_hash: str, check_info: dict):
    """Background remux task."""
    try:
        duration = check_info.get("duration", 0)
        needs_transcode = check_info.get("needs_transcode", False)
        temp_path = remux_path.with_suffix(".tmp.mp4")

        if needs_transcode:
            # Full transcode: re-encode to H.264+AAC
            _active_remuxes[vid_hash] = {"percent": 0, "type": "transcoding"}
            cmd = [
                "ffmpeg", "-y", "-i", str(filepath),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-threads", "2",
                str(temp_path)
            ]
        else:
            # Fast remux: copy streams, only change container to MP4 faststart
            _active_remuxes[vid_hash] = {"percent": 0, "type": "remuxing"}
            cmd = [
                "ffmpeg", "-y", "-i", str(filepath),
                "-c", "copy",
                "-movflags", "+faststart",
                str(temp_path)
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
            m = re.search(r'time=(\d+):(\d+):(\d+)', dec)
            if m and duration > 0:
                elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                pct = min(99, int(elapsed / duration * 100))
                rtype = "transcoding" if needs_transcode else "remuxing"
                _active_remuxes[vid_hash] = {"percent": pct, "type": rtype}

        await proc.wait()

        if proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
            # Rename temp to final
            temp_path.rename(remux_path)
            _active_remuxes.pop(vid_hash, None)
        else:
            # Clean up on failure
            if temp_path.exists():
                temp_path.unlink()
            _active_remuxes[vid_hash] = {"percent": -1, "type": "failed", "error": "FFmpeg failed"}
    except Exception as e:
        _active_remuxes[vid_hash] = {"percent": -1, "type": "failed", "error": str(e)}


async def cleanup_remux(filepath: Path):
    """Remove cached remuxed file for a video."""
    remux_path = get_remux_path(filepath)
    if remux_path.exists():
        remux_path.unlink()


# ===== Sprite Thumbnails for Seek Preview =====

async def generate_sprite_thumbnails(filepath: Path):
    """Generate sprite sheet + VTT for Plyr seek preview thumbnails.
    Returns (vtt_path, sprite_dir) or None on failure.
    Uses dynamic tile sizing based on video duration.
    """
    vid_hash = _video_hash(filepath)
    thumb_dir = THUMBNAILS_DIR / vid_hash
    sprite_path = thumb_dir / "sprite.jpg"
    vtt_path = thumb_dir / "thumbnails.vtt"

    # Already generated
    if vtt_path.exists() and sprite_path.exists():
        return (vtt_path, thumb_dir)

    # Get duration
    duration = await quick_probe_duration(filepath)
    if duration <= 0:
        return None

    thumb_dir.mkdir(parents=True, exist_ok=True)

    # Dynamic tile sizing
    interval = 10  # 1 frame every 10 seconds
    total_frames = math.ceil(duration / interval)
    if total_frames <= 0:
        return None
    cols = 10
    rows = math.ceil(total_frames / cols)
    thumb_w, thumb_h = 160, 90

    # Generate sprite sheet with ffmpeg
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-vf", f"fps=1/{interval},scale={thumb_w}:{thumb_h},tile={cols}x{rows}",
            "-frames:v", "1",
            "-q:v", "5",
            str(sprite_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0 or not sprite_path.exists():
            return None
    except Exception:
        return None

    # Generate VTT file
    try:
        # Sprite URL relative to /thumbnails mount
        sprite_url = f"/thumbnails/{vid_hash}/sprite.jpg"
        lines = ["WEBVTT", ""]

        for i in range(total_frames):
            start_sec = i * interval
            end_sec = min((i + 1) * interval, duration)

            col = i % cols
            row = i // cols
            x = col * thumb_w
            y = row * thumb_h

            start_ts = _seconds_to_vtt_time(start_sec)
            end_ts = _seconds_to_vtt_time(end_sec)

            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(f"{sprite_url}#xywh={x},{y},{thumb_w},{thumb_h}")
            lines.append("")

        vtt_path.write_text("\n".join(lines), encoding="utf-8")
        return (vtt_path, thumb_dir)
    except Exception:
        return None


def _seconds_to_vtt_time(sec: float) -> str:
    """Convert seconds to VTT timestamp HH:MM:SS.mmm"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def get_thumbnail_dir(filepath: Path) -> Path:
    """Get thumbnail cache directory for a video."""
    return THUMBNAILS_DIR / _video_hash(filepath)


# ===== Subtitle Scanning & Conversion =====

LANGUAGE_LABELS = {
    'vie': 'Tiếng Việt', 'vi': 'Tiếng Việt',
    'eng': 'English',    'en': 'English',
    'zho': '中文',        'zh': '中文',
    'chi': '中文',
    'ara': 'العربية',    'ar': 'العربية',
    'cat': 'Català',     'ca': 'Català',
    'ces': 'Čeština',    'cs': 'Čeština',
    'dan': 'Dansk',      'da': 'Dansk',
    'deu': 'Deutsch',    'de': 'Deutsch',
    'spa': 'Español',    'es': 'Español',
    'fra': 'Français',   'fr': 'Français',
    'ita': 'Italiano',   'it': 'Italiano',
    'jpn': '日本語',      'ja': '日本語',
    'kor': '한국어',      'ko': '한국어',
    'nld': 'Nederlands', 'nl': 'Nederlands',
    'nor': 'Norsk',      'no': 'Norsk',
    'por': 'Português',  'pt': 'Português',
    'ron': 'Română',     'ro': 'Română',
    'rus': 'Русский',    'ru': 'Русский',
    'swe': 'Svenska',    'sv': 'Svenska',
    'tha': 'ภาษาไทย',    'th': 'ภาษาไทย',
    'tur': 'Türkçe',     'tr': 'Türkçe',
    'pol': 'Polski',     'pl': 'Polski',
    'ind': 'Bahasa Indonesia', 'id': 'Bahasa Indonesia',
    'msa': 'Bahasa Melayu',    'ms': 'Bahasa Melayu',
}


def get_language_label(lang_code, title=None):
    """Get human-readable label from language code or track title."""
    if title and title.strip():
        return title.strip()
    if lang_code:
        return LANGUAGE_LABELS.get(lang_code.lower(), lang_code.upper())
    return 'Unknown'


def get_subtitle_cache_dir(filepath: Path) -> Path:
    """Get subtitle cache directory using video hash."""
    vid_hash = _video_hash(filepath)
    cache_dir = THUMBNAILS_DIR / "subs" / vid_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


async def probe_subtitle_streams(filepath: Path) -> list:
    """Probe video file for embedded text-based subtitle streams."""
    SUPPORTED_CODECS = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'srt'}
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 's',
            str(filepath)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(out.decode(errors='ignore'))
        streams = data.get('streams', [])

        results = []
        sub_index = 0  # subtitle-relative index for -map 0:s:{i}
        for s in streams:
            codec = s.get('codec_name', '').lower()
            if codec not in SUPPORTED_CODECS:
                sub_index += 1
                continue
            tags = s.get('tags', {})
            results.append({
                'stream_index': sub_index,
                'codec_name': codec,
                'language': tags.get('language', ''),
                'title': tags.get('title', ''),
            })
            sub_index += 1
        return results
    except Exception:
        return []


async def extract_embedded_subtitle(filepath: Path, stream_index: int, out_path: Path) -> bool:
    """Extract a single subtitle stream to WebVTT file."""
    try:
        cmd = [
            'ffmpeg', '-v', 'quiet',
            '-i', str(filepath),
            '-map', f'0:s:{stream_index}',
            '-c:s', 'webvtt',
            '-y',
            str(out_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        return proc.returncode == 0 and out_path.exists()
    except Exception:
        return False


async def get_embedded_subtitles(filepath: Path) -> list:
    """Extract and cache embedded subtitle tracks from video file."""
    cache_dir = get_subtitle_cache_dir(filepath)
    manifest_path = cache_dir / 'manifest.json'

    # Check cache first
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # Probe for subtitle streams
    streams = await probe_subtitle_streams(filepath)
    if not streams:
        return []

    extracted = []
    for i, stream in enumerate(streams):
        lang = stream.get('language', '') or ''
        title = stream.get('title', '') or ''
        label = get_language_label(lang, title)

        filename = f"{i}_{lang or 'track'}.vtt"
        out_path = cache_dir / filename

        if not out_path.exists():
            success = await extract_embedded_subtitle(
                filepath,
                stream['stream_index'],
                out_path
            )
            if not success:
                continue

        extracted.append({
            'label': label,
            'language': lang[:2] if lang else 'un',
            'src': f'/api/media/subtitle-file-cached/{cache_dir.name}/{filename}'
        })

    # Save manifest cache
    if extracted:
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(extracted, f, ensure_ascii=False)
        except Exception:
            pass

    return extracted


def scan_subtitles(filepath: Path) -> list:
    """Scan for external subtitle files (.srt, .vtt) matching the video filename.
    Returns list of {label, language, src}.
    """
    SUPPORTED_SUB_EXTS = {".srt", ".vtt"}
    video_stem = filepath.stem.lower()
    parent = filepath.parent
    results = []

    if not parent.exists():
        return results

    for f in parent.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in SUPPORTED_SUB_EXTS:
            continue

        # Match: video.srt, video.en.srt, video.vi.srt etc.
        sub_stem = f.stem.lower()
        if not sub_stem.startswith(video_stem):
            continue

        # Parse language from filename
        remainder = sub_stem[len(video_stem):]
        if remainder.startswith("."):
            lang = remainder[1:]  # e.g. ".vi" → "vi"
        elif remainder == "":
            lang = "und"  # undefined
        else:
            continue  # not a match (e.g. "video2.srt" for "video.mp4")

        label = get_language_label(lang)

        # Build serve URL via subtitle-file endpoint
        rel_path = str(f.relative_to(DOWNLOAD_DIR)).replace("\\", "/")
        src = f"/api/media/subtitle-file/{rel_path}"

        results.append({
            "label": label,
            "language": lang,
            "src": src,
        })

    # Sort: defined languages first
    results.sort(key=lambda x: (x["language"] == "und", x["language"]))
    return results


async def scan_subtitles_with_embedded(filepath: Path) -> list:
    """Scan for subtitles: external first, fallback to embedded."""
    results = scan_subtitles(filepath)
    if not results:
        results = await get_embedded_subtitles(filepath)
    return results


def srt_to_vtt_content(raw_bytes: bytes) -> str:
    """Convert SRT subtitle content to WebVTT format.
    Handles utf-8 with fallback to latin-1 encoding.
    """
    # Decode with fallback
    try:
        content = raw_bytes.decode("utf-8-sig")  # handles BOM
    except UnicodeDecodeError:
        content = raw_bytes.decode("latin-1")

    lines = content.replace("\r\n", "\n").split("\n")
    vtt_lines = ["WEBVTT", ""]

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip cue numbers (pure digits)
        if line.isdigit():
            i += 1
            continue

        # Fix timestamp separators: "," → "."
        if "-->" in line:
            line = line.replace(",", ".")
            vtt_lines.append(line)
            i += 1
            continue

        # Empty line = cue separator
        if line == "":
            vtt_lines.append("")
            i += 1
            continue

        # Subtitle text
        vtt_lines.append(line)
        i += 1

    return "\n".join(vtt_lines)

