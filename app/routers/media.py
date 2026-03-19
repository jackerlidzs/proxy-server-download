"""
Media Router - media listing, streaming, metadata, HLS transcoding
"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth import verify_key
from config import DOWNLOAD_DIR, VIDEO_EXTS
from services.media_service import (
    list_media, get_media_info, generate_thumbnail, extract_subtitles,
    convert_srt_to_vtt, get_hls_status, transcode_to_hls, cleanup_hls,
    get_remux_status, check_needs_remux, remux_to_mp4, cleanup_remux,
    generate_sprite_thumbnails, get_thumbnail_dir,
    scan_subtitles, srt_to_vtt_content
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
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_CONTAINERS = {".mp4", ".webm", ".mov", ".m4v"}

@router.get("/api/media/probe/{filepath:path}")
async def probe_compat(filepath: str, _=Depends(verify_key)):
    """Check if video codec is browser-compatible. Returns duration and remux status."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    try:
        # Use the comprehensive check_needs_remux
        check = await check_needs_remux(fp)
        remux = get_remux_status(fp)

        codec = check.get("codec", "unknown")
        container = check.get("container", fp.suffix.lower())
        duration = check.get("duration", 0)
        needs_remux = check.get("needs_remux", False)
        needs_transcode = check.get("needs_transcode", False)

        container_ok = container in BROWSER_CONTAINERS
        codec_ok = codec in BROWSER_VIDEO_CODECS

        return {
            "needs_transcode": needs_transcode or (not container_ok and not codec_ok),
            "needs_remux": needs_remux,
            "codec": codec,
            "container": container,
            "browser_compatible": container_ok and codec_ok and not needs_remux,
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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

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
async def api_thumbnails(filepath: str, _=Depends(verify_key)):
    """Get VTT file for Plyr seek preview thumbnails.
    Returns VTT if already generated, 202 if generating, 404 on error."""
    import asyncio
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)

    thumb_dir = get_thumbnail_dir(fp)
    vtt_path = thumb_dir / "thumbnails.vtt"

    # Already generated → serve VTT
    if vtt_path.exists():
        from fastapi.responses import Response
        content = vtt_path.read_text(encoding="utf-8")
        return Response(content=content, media_type="text/vtt")

    # Trigger generation in background
    asyncio.create_task(generate_sprite_thumbnails(fp))
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content={"status": "generating", "message": "Thumbnail sprites are being generated"}
    )


# --- Subtitle Endpoints ---
@router.get("/api/media/subtitles/{filepath:path}")
async def api_subtitles(filepath: str, _=Depends(verify_key)):
    """List available external subtitle files for a video."""
    fp = DOWNLOAD_DIR / filepath
    if not fp.exists():
        raise HTTPException(404)
    subs = scan_subtitles(fp)
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

