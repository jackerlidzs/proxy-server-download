"""
Media Router - media listing, streaming, metadata, HLS transcoding
"""
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth import verify_key
from config import DOWNLOAD_DIR, VIDEO_EXTS, FFMPEG_PRESET, MAX_CONCURRENT_TRANSCODE, CPU_CORES, MAX_CONCURRENT
from services.media_service import (
    list_media, get_media_info, generate_thumbnail, extract_subtitles,
    convert_srt_to_vtt, get_hls_status, transcode_to_hls, cleanup_hls,
    get_remux_status, check_needs_remux, remux_to_mp4, cleanup_remux,
    generate_sprite_thumbnails, get_thumbnail_dir,
    get_thumbnail_cache_dir, get_thumbnail_vtt_url,
    scan_subtitles, scan_subtitles_with_embedded, srt_to_vtt_content
)

router = APIRouter(tags=["media"])


@router.get("/api/media")
async def api_list_media(_=Depends(verify_key)):
    data = list_media()
    # Enrich with HLS status
    for m in data.get("media", []):
        if m["type"] == "video":
            fp = DOWNLOAD_DIR / m["path"]
            if fp.exists():
                hls = get_hls_status(fp)
                m["hls"] = hls
    return data


@router.get("/api/media/info/{filepath:path}")
async def api_media_info(filepath: str, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    info = await get_media_info(fp)
    if not info:
        raise HTTPException(500, "Could not read media info")
    # Add HLS status
    if fp.suffix.lower() in VIDEO_EXTS:
        info["hls"] = get_hls_status(fp)
    return info


@router.get("/api/media/thumbnail/{filepath:path}")
async def api_thumbnail(filepath: str, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    thumb = await generate_thumbnail(fp)
    if not thumb:
        raise HTTPException(500, "Could not generate thumbnail")
    from fastapi.responses import FileResponse
    return FileResponse(str(thumb), media_type="image/jpeg")


@router.post("/api/media/extract-subs/{filepath:path}")
async def api_extract_subs(filepath: str, track: int = 0, _=Depends(verify_key)):
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    out = await extract_subtitles(fp, track)
    if not out:
        raise HTTPException(500, "Could not extract subtitles")
    return {"message": "Subtitles extracted", "filename": out.name}


@router.post("/api/media/convert-srt/{filepath:path}")
async def api_convert_srt(filepath: str, _=Depends(verify_key)):
    """Convert SRT subtitle to VTT format."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    out = await convert_srt_to_vtt(fp)
    if not out:
        raise HTTPException(500, "Could not convert subtitle")
    return {"message": "Converted to VTT", "filename": out.name}


# --- HLS Transcoding ---
@router.get("/api/media/hls/{filepath:path}")
async def api_hls_status(filepath: str, _=Depends(verify_key)):
    """Get HLS transcoding status for a video."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    return get_hls_status(fp)


@router.post("/api/media/hls/{filepath:path}")
async def api_hls_transcode(filepath: str, _=Depends(verify_key)):
    """Start HLS transcoding for a video."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    if fp.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, "Not a video file")
    result = await transcode_to_hls(fp)
    return result


@router.delete("/api/media/hls/{filepath:path}")
async def api_hls_cleanup(filepath: str, _=Depends(verify_key)):
    """Remove cached HLS segments."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    await cleanup_hls(fp)
    return {"message": "HLS cache removed"}


# --- Streaming endpoint (fallback, nginx should handle most requests) ---
@router.get("/stream/{filename:path}")
async def stream(filename: str, request: Request):
    import re
    fp = DOWNLOAD_DIR / filename
    if not fp.exists():
        raise HTTPException(404)

    sz = fp.stat().st_size
    ct_map = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
        ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".ts": "video/mp2t", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".srt": "text/plain", ".vtt": "text/vtt"
    }
    ct = ct_map.get(fp.suffix.lower(), "application/octet-stream")
    rng = request.headers.get("range")

    if rng:
        m = re.match(r'bytes=(\d+)-(\d*)', rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else sz - 1
            end = min(end, sz - 1)
            length = end - start + 1

            # Use 256KB chunks for better streaming performance (vs 64KB before)
            CHUNK = 262144

            async def gen():
                with open(fp, "rb") as f:
                    f.seek(start)
                    rem = length
                    while rem > 0:
                        d = f.read(min(CHUNK, rem))
                        if not d:
                            break
                        rem -= len(d)
                        yield d

            return StreamingResponse(gen(), status_code=206, headers={
                "Content-Range": f"bytes {start}-{end}/{sz}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": ct,
                "Access-Control-Allow-Origin": "*"
            })

    CHUNK = 262144

    async def gen_full():
        with open(fp, "rb") as f:
            while d := f.read(CHUNK):
                yield d

    return StreamingResponse(gen_full(), headers={
        "Accept-Ranges": "bytes",
        "Content-Length": str(sz),
        "Content-Type": ct,
        "Access-Control-Allow-Origin": "*"
    })


# --- Probe codec for browser compatibility + remux status ---
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1", "avc", "theora"}
BROWSER_CONTAINERS = {".mp4", ".webm", ".mov", ".m4v"}
BROWSER_AUDIO_CODECS = {
    "aac", "mp3", "mp3float",
    "opus", "vorbis", "flac",
    "pcm_s16le", "pcm_s24le", "pcm_u8",
}

@router.get("/api/media/probe/{filepath:path}")
async def probe_compat(filepath: str, _=Depends(verify_key)):
    """Check if video/audio codecs are browser-compatible. Returns duration and remux status."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    try:
        # Use the comprehensive check_needs_remux (video + container check)
        check = await check_needs_remux(fp)
        remux = get_remux_status(fp)

        # Get full media info (includes audio codec)
        media_info = await get_media_info(fp)

        video_codec = check.get("codec", "unknown")
        audio_codec = media_info.get("audio_codec", "").lower()
        container = check.get("container", fp.suffix.lower())
        duration = check.get("duration", 0)
        needs_remux = check.get("needs_remux", False)

        container_ok = container in BROWSER_CONTAINERS
        video_ok = video_codec in BROWSER_VIDEO_CODECS
        audio_ok = audio_codec in BROWSER_AUDIO_CODECS or audio_codec == ""

        browser_compatible = container_ok and video_ok and audio_ok and not needs_remux
        needs_transcode = check.get("needs_transcode", False) or (not video_ok) or (not audio_ok and audio_codec != "")

        return {
            "needs_transcode": needs_transcode,
            "needs_remux": needs_remux,
            "codec": video_codec,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "container": container,
            "video_compatible": video_ok,
            "audio_compatible": audio_ok,
            "browser_compatible": browser_compatible,
            "duration": duration,
            "reason": check.get("reason", ""),
            "remux": remux,
        }
    except Exception as e:
        return {"needs_transcode": False, "needs_remux": False, "codec": "unknown", "error": str(e), "duration": 0, "remux": {"status": "not_started"}}


# --- On-the-fly transcoding stream (ffmpeg → H.264+AAC in fragmented MP4) ---
@router.get("/stream-transcode/{filename:path}")
async def stream_transcode(filename: str, request: Request, ss: float = 0):
    """Transcode video on-the-fly to browser-compatible H.264+AAC.
    Use ?ss=seconds to seek to a specific position."""
    import asyncio
    fp = DOWNLOAD_DIR / filename
    if not fp.exists():
        raise HTTPException(404)

    # Quick probe for duration (to pass in headers)
    from services.media_service import quick_probe_duration
    duration = await quick_probe_duration(fp)

    # Build ffmpeg command with optional seek
    cmd = ["ffmpeg"]
    if ss > 0:
        cmd += ["-ss", str(ss)]  # seek BEFORE input for fast seek
    cmd += [
        "-i", str(fp),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "-threads", "1",
        "-y", "pipe:1"
    ]

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            ),
            timeout=10
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "FFmpeg startup timeout")

    async def generate():
        try:
            while True:
                chunk = await proc.stdout.read(262144)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache",
    }
    if duration > 0:
        headers["X-Video-Duration"] = str(duration)

    return StreamingResponse(generate(), media_type="video/mp4", headers=headers)


# --- Remux API ---
@router.post("/api/media/remux/{filepath:path}")
async def api_remux(filepath: str, _=Depends(verify_key)):
    """Trigger background remux of video to MP4 faststart."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    if fp.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, "Not a video file")
    result = await remux_to_mp4(fp)
    return result


@router.get("/api/media/remux-status/{filepath:path}")
async def api_remux_status(filepath: str, _=Depends(verify_key)):
    """Check remux status for a video file."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    return get_remux_status(fp)


@router.delete("/api/media/remux/{filepath:path}")
async def api_remux_cleanup(filepath: str, _=Depends(verify_key)):
    """Remove cached remuxed file."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    await cleanup_remux(fp)
    return {"message": "Remux cache removed"}


# --- Seek Preview Thumbnails ---
@router.get("/api/media/thumbnails/{filepath:path}")
async def api_thumbnails(filepath: str):
    """Get VTT for Plyr seek preview thumbnails.
    No auth required — Plyr fetches without headers.
    Returns 302 redirect to static VTT if ready, 202 if generating."""
    from fastapi.responses import RedirectResponse, JSONResponse
    import asyncio

    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404, "File not found")

    # Check cache
    info = get_thumbnail_vtt_url(fp)

    if info['status'] == 'ready':
        # Redirect to static VTT so Plyr resolves relative sprite.jpg correctly
        return RedirectResponse(url=info['vtt_url'], status_code=302)

    # Dedup guard: skip if already generating for this file
    from services.media_service import _generating_thumbnails
    vid_hash = info.get('hash', '')
    if not vid_hash:
        from services.media_service import get_thumbnail_cache_dir
        vid_hash = get_thumbnail_cache_dir(fp).name
    if vid_hash in _generating_thumbnails:
        return JSONResponse(
            status_code=202,
            content={'status': 'generating', 'message': 'Already generating'}
        )

    # Not generated yet → trigger in background
    asyncio.create_task(generate_sprite_thumbnails(fp))
    return JSONResponse(
        status_code=202,
        content={'status': 'generating', 'message': 'Thumbnail sprites are being generated'}
    )


@router.post("/api/media/thumbnails/pregen-all")
async def api_pregen_all(
    background_tasks: BackgroundTasks,
    _=Depends(verify_key)
):
    """Trigger thumbnail pre-generation for all existing videos without cached thumbnails."""
    from services.media_service import (
        generate_sprite_thumbnails, get_thumbnail_vtt_url
    )
    VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.m4v', '.flv'}
    triggered, skipped = [], []

    for f in DOWNLOAD_DIR.rglob('*'):
        if f.suffix.lower() not in VIDEO_EXTS:
            continue
        info = get_thumbnail_vtt_url(f)
        if info['status'] == 'ready':
            skipped.append(f.name)
        else:
            background_tasks.add_task(generate_sprite_thumbnails, f)
            triggered.append(f.name)

    return {
        "triggered": len(triggered),
        "skipped":   len(skipped),
        "files":     triggered
    }


@router.get("/api/media/subtitles/{filepath:path}")
async def api_subtitles(filepath: str, _=Depends(verify_key)):
    """List available subtitle files for a video (external + embedded)."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    subs = await scan_subtitles_with_embedded(fp)
    return subs


@router.get("/api/media/subtitle-file/{filepath:path}")
async def api_subtitle_file(filepath: str, _=Depends(verify_key)):
    """Serve a subtitle file. SRT files are converted to VTT on-the-fly."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)

    ext = fp.suffix.lower()

    if ext == ".vtt":
        from fastapi.responses import FileResponse
        return FileResponse(str(fp), media_type="text/vtt")

    if ext == ".srt":
        # Convert SRT to VTT on-the-fly
        raw = fp.read_bytes()
        vtt_content = srt_to_vtt_content(raw)
        from fastapi.responses import Response
        return Response(content=vtt_content, media_type="text/vtt")

    raise HTTPException(400, "Unsupported subtitle format")


@router.get("/api/media/subtitle-file-cached/{cache_hash}/{filename}")
async def api_cached_subtitle(cache_hash: str, filename: str):
    """Serve cached extracted subtitle (embedded tracks)."""
    from config import THUMBNAILS_DIR
    from fastapi.responses import FileResponse

    cache_dir = THUMBNAILS_DIR / "subs" / cache_hash
    file_path = cache_dir / filename

    # Validate path (prevent path traversal)
    if not str(file_path.resolve()).startswith(str(THUMBNAILS_DIR.resolve())):
        raise HTTPException(403, "Forbidden")

    if not file_path.exists():
        raise HTTPException(404, "Subtitle not found")

    return FileResponse(
        str(file_path),
        media_type="text/vtt",
        headers={"Cache-Control": "public, max-age=86400"}
    )

@router.get("/api/media/server-status")
async def api_server_status(_=Depends(verify_key)):
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    return {
        "cpu_cores":       CPU_CORES,
        "cpu_percent":     cpu,
        "ram_total_gb":    round(ram.total / 1e9, 1),
        "ram_used_gb":     round(ram.used   / 1e9, 1),
        "ram_free_gb":     round(ram.available / 1e9, 1),
        "ram_percent":     ram.percent,
        "ffmpeg_preset":   FFMPEG_PRESET,
        "max_concurrent":  MAX_CONCURRENT,
        "safe_to_convert": cpu < 80 and ram.percent < 85,
        "recommendation":  (
            "OVERLOADED" if cpu > 80 or ram.percent > 85 else
            "BUSY"       if cpu > 50 or ram.percent > 70 else
            "OK"
        )
    }


@router.get("/server-status")
async def server_status(_=Depends(verify_key)):
    """Server health endpoint — CPU, RAM, conversion readiness."""
    import psutil

    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()

    return {
        "cpu_cores": psutil.cpu_count(),
        "cpu_percent": cpu,
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "ram_used_gb": round(mem.used / 1024**3, 1),
        "ram_free_gb": round(mem.available / 1024**3, 1),
        "ram_percent": mem.percent,
        "safe_to_convert": mem.available > 500 * 1024**2 and cpu < 70,
        "recommendation": (
            "OK" if cpu < 50 and mem.percent < 70 else
            "BUSY" if cpu < 80 and mem.percent < 85 else
            "OVERLOADED"
        ),
        "ffmpeg_preset": FFMPEG_PRESET,
        "max_concurrent": MAX_CONCURRENT_TRANSCODE,
    }

