import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video-extractor-api")

app = FastAPI(title="Video Extractor API", version="1.0.0")


class ResolveRequest(BaseModel):
    url: str = Field(..., min_length=5)
    format: str = Field(default="mp4", pattern=r"^(mp3|mp4|mp4_hd)$")


class ResolveResponse(BaseModel):
    source_url: str
    title: str
    media_url: str
    media_ext: str
    thumbnail: str | None = None
    duration: int | None = None
    extractor: str | None = None
    requested_format: str
    headers: dict[str, str] = Field(default_factory=dict)


def _base_ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 25,
    }


def _format_selector(format_name: str) -> str:
    if format_name == "mp3":
        return "bestaudio/best"
    if format_name == "mp4_hd":
        return (
            "bv*[height>=720][ext=mp4]+ba[ext=m4a]/"
            "bv*[height>=720]+ba/"
            "b[height>=720][ext=mp4]/"
            "b[height>=720]/"
            "best[ext=mp4]/best"
        )
    return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best[ext=mp4]/best"


def _extract(url: str, format_name: str) -> dict[str, Any]:
    ydl_opts = _base_ydl_opts()
    ydl_opts["format"] = _format_selector(format_name)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
            info = info["entries"][0]
        if not info or not isinstance(info, dict):
            raise HTTPException(status_code=422, detail="Failed to extract media metadata.")
        return info


def _resolve_media_url(info: dict[str, Any], format_name: str) -> tuple[str, str]:
    direct_url = info.get("url")
    ext = (info.get("ext") or "").lower()
    requested = info.get("requested_formats")

    if requested and isinstance(requested, list):
        if format_name == "mp3":
            for fmt in requested:
                fmt_url = fmt.get("url")
                if fmt_url:
                    return fmt_url, (fmt.get("ext") or "m4a").lower()
        for fmt in requested:
            if fmt.get("vcodec") != "none" and fmt.get("url"):
                return fmt["url"], (fmt.get("ext") or "mp4").lower()

    if isinstance(direct_url, str) and direct_url:
        if not ext:
            guess = re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov)(?:\?|$)", direct_url, re.IGNORECASE)
            ext = guess.group(1).lower() if guess else ("mp3" if format_name == "mp3" else "mp4")
        return direct_url, ext

    raise HTTPException(status_code=422, detail="No direct media URL found for this link.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/resolve", response_model=ResolveResponse)
def resolve_media(request: ResolveRequest) -> ResolveResponse:
    try:
        info = _extract(request.url, request.format)
        media_url, media_ext = _resolve_media_url(info, request.format)
        headers = info.get("http_headers") or {}
        if not isinstance(headers, dict):
            headers = {}

        return ResolveResponse(
            source_url=request.url,
            title=(info.get("title") or "video").strip(),
            media_url=media_url,
            media_ext=media_ext,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            extractor=info.get("extractor_key") or info.get("extractor"),
            requested_format=request.format,
            headers={str(k): str(v) for k, v in headers.items()},
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve failed")
        raise HTTPException(status_code=500, detail=f"Resolver error: {exc}") from exc
