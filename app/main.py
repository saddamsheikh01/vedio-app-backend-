import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video-extractor-api")

app = FastAPI(title="Video Extractor API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _is_http_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.IGNORECASE))


def _looks_like_direct_media_url(url: str) -> bool:
    return bool(re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov|wav)(?:\?|$)", url, re.IGNORECASE))


def _normalize_url(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = cleaned.split("#", 1)[0]
    return cleaned


def _base_ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "ignoreerrors": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 25,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Mobile) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            )
        },
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


def _extract_with_fallback(url: str, format_name: str) -> tuple[dict[str, Any], str]:
    tried: list[str] = []
    fallback_order = [format_name]
    if format_name == "mp4_hd":
        fallback_order.extend(["mp4", "mp3"])
    elif format_name == "mp4":
        fallback_order.append("mp3")
    elif format_name == "mp3":
        fallback_order.append("mp4")

    last_error: Exception | None = None
    for candidate in fallback_order:
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            return _extract(url, candidate), candidate
        except HTTPException as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(status_code=422, detail="Failed to extract media metadata.")


def _resolve_media_url(info: dict[str, Any], format_name: str) -> tuple[str, str]:
    direct_url = info.get("url")
    ext = (info.get("ext") or "").lower()
    requested = info.get("requested_formats")
    formats = info.get("formats")

    if requested and isinstance(requested, list):
        if format_name == "mp3":
            for fmt in requested:
                fmt_url = fmt.get("url")
                if fmt_url:
                    return fmt_url, (fmt.get("ext") or "m4a").lower()
        for fmt in requested:
            if fmt.get("vcodec") != "none" and fmt.get("url"):
                return fmt["url"], (fmt.get("ext") or "mp4").lower()

    if formats and isinstance(formats, list):
        candidates: list[dict[str, Any]] = [f for f in formats if isinstance(f, dict) and f.get("url")]
        if format_name == "mp3":
            audio_only = [
                f
                for f in candidates
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            ]
            if audio_only:
                best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
                return best_audio["url"], (best_audio.get("ext") or "m4a").lower()
        else:
            video_candidates = [f for f in candidates if f.get("vcodec") not in (None, "none")]
            if format_name == "mp4_hd":
                hd_first = [f for f in video_candidates if (f.get("height") or 0) >= 720]
                pool = hd_first or video_candidates
            else:
                pool = video_candidates
            if pool:
                best_video = max(
                    pool,
                    key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0), (f.get("filesize") or 0)),
                )
                return best_video["url"], (best_video.get("ext") or "mp4").lower()

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
        source_url = _normalize_url(request.url)
        if not _is_http_url(source_url):
            raise HTTPException(status_code=422, detail="Only HTTP/HTTPS URLs are supported.")

        if _looks_like_direct_media_url(source_url):
            guessed_ext = (
                re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov|wav)(?:\?|$)", source_url, re.IGNORECASE)
                .group(1)
                .lower()
            )
            return ResolveResponse(
                source_url=source_url,
                title="direct_media",
                media_url=source_url,
                media_ext=guessed_ext,
                thumbnail=None,
                duration=None,
                extractor="direct",
                requested_format=request.format,
                headers={},
            )

        info, resolved_format = _extract_with_fallback(source_url, request.format)
        media_url, media_ext = _resolve_media_url(info, resolved_format)
        headers = info.get("http_headers") or {}
        if not isinstance(headers, dict):
            headers = {}

        return ResolveResponse(
            source_url=source_url,
            title=(info.get("title") or "video").strip(),
            media_url=media_url,
            media_ext=media_ext,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            extractor=info.get("extractor_key") or info.get("extractor"),
            requested_format=resolved_format,
            headers={str(k): str(v) for k, v in headers.items()},
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve failed")
        raise HTTPException(status_code=500, detail=f"Resolver error: {exc}") from exc
