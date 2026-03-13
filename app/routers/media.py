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
    convert_srt_to_vtt, get_hls_status, transcode_to_hls, cleanup_hls
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
