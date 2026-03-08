import logging
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video-extractor-api")

SHORTENER_HOSTS = {
    "vt.tiktok.com",
    "vm.tiktok.com",
    "t.co",
    "bit.ly",
    "tinyurl.com",
    "rb.gy",
    "shorturl.at",
    "instagram.com",
    "www.instagram.com",
    "l.instagram.com",
    "fb.watch",
    "youtu.be",
}

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


def _expand_short_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host not in SHORTENER_HOSTS:
        return url

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Mobile) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            redirected = response.geturl().strip()
            if not redirected:
                return url
            cleaned = redirected.split("#", 1)[0].strip()
            return cleaned or url
    except (urllib.error.URLError, ValueError):
        return url


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0].strip()


def _tiktok_short_to_canonical(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"vt.tiktok.com", "vm.tiktok.com"}:
        return None
    path = parsed.path.strip("/")
    if not path:
        return None
    short_code = path.split("/", 1)[0]
    if not short_code:
        return None
    return f"https://www.tiktok.com/t/{short_code}/"


def _candidate_source_urls(raw_url: str) -> list[str]:
    candidates: list[str] = []

    def _push(value: str | None) -> None:
        if not value:
            return
        cleaned = _without_fragment(value)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    normalized = _normalize_url(raw_url)
    _push(normalized)
    _push(_tiktok_short_to_canonical(normalized))

    expanded = _expand_short_url(normalized)
    if expanded != normalized:
        _push(expanded)
        _push(_tiktok_short_to_canonical(expanded))

    return candidates


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


def _extract(url: str, format_name: str, *, use_relaxed_profile: bool = False) -> dict[str, Any]:
    ydl_opts = _base_ydl_opts()
    ydl_opts["format"] = "best" if use_relaxed_profile else _format_selector(format_name)

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

    last_error_message = "Failed to extract media metadata."
    for candidate in fallback_order:
        if candidate in tried:
            continue
        tried.append(candidate)
        for relaxed in (False, True):
            try:
                return _extract(url, candidate, use_relaxed_profile=relaxed), candidate
            except HTTPException as exc:
                last_error_message = str(exc.detail) if getattr(exc, "detail", None) else str(exc)
                continue
            except Exception as exc:  # noqa: BLE001
                last_error_message = str(exc)
                continue

    raise HTTPException(status_code=422, detail=last_error_message)


def _protocol_rank(fmt: dict[str, Any]) -> int:
    protocol = str(fmt.get("protocol") or "").lower()
    if protocol.startswith("http") and "m3u8" not in protocol and "dash" not in protocol:
        return 3
    if protocol.startswith("https"):
        return 3
    if "m3u8" in protocol:
        return 1
    if "dash" in protocol:
        return 1
    if protocol:
        return 2
    return 0


def _score_audio_format(fmt: dict[str, Any]) -> tuple[int, int, int]:
    ext = str(fmt.get("ext") or "").lower()
    vcodec = str(fmt.get("vcodec") or "none").lower()
    acodec = str(fmt.get("acodec") or "none").lower()
    abr = int(fmt.get("abr") or 0)
    ext_rank = 2 if ext in {"m4a", "mp3"} else (1 if ext else 0)
    audio_only_rank = 2 if vcodec in {"none", ""} and acodec not in {"none", ""} else 0
    return (audio_only_rank, ext_rank, abr)


def _score_video_format(fmt: dict[str, Any], format_name: str) -> tuple[int, int, int, int, int]:
    ext = str(fmt.get("ext") or "").lower()
    vcodec = str(fmt.get("vcodec") or "none").lower()
    acodec = str(fmt.get("acodec") or "none").lower()
    height = int(fmt.get("height") or 0)
    tbr = int(fmt.get("tbr") or 0)

    has_video = 1 if vcodec not in {"none", ""} else 0
    has_audio = 1 if acodec not in {"none", ""} else 0
    ext_rank = 3 if ext == "mp4" else (2 if ext in {"mkv", "webm", "mov"} else (1 if ext else 0))
    hd_rank = 1 if (format_name == "mp4_hd" and height >= 720) else 0
    return (has_video, has_audio, hd_rank, ext_rank, _protocol_rank(fmt) * 100000 + height * 100 + tbr)


def _resolve_media_url(info: dict[str, Any], format_name: str) -> tuple[str, str]:
    direct_url = info.get("url")
    ext = (info.get("ext") or "").lower()
    requested = info.get("requested_formats")
    formats = info.get("formats")

    if requested and isinstance(requested, list):
        requested_candidates = [f for f in requested if isinstance(f, dict) and f.get("url")]
        if format_name == "mp3" and requested_candidates:
            best_requested_audio = max(requested_candidates, key=_score_audio_format)
            return best_requested_audio["url"], (best_requested_audio.get("ext") or "m4a").lower()
        if requested_candidates:
            requested_progressive = [
                f
                for f in requested_candidates
                if str(f.get("vcodec") or "none").lower() not in {"none", ""}
                and str(f.get("acodec") or "none").lower() not in {"none", ""}
            ]
            if requested_progressive:
                best_requested_video = max(
                    requested_progressive,
                    key=lambda f: _score_video_format(f, format_name),
                )
                return best_requested_video["url"], (best_requested_video.get("ext") or "mp4").lower()

            requested_video_only = [
                f for f in requested_candidates if str(f.get("vcodec") or "none").lower() not in {"none", ""}
            ]
            if requested_video_only:
                best_requested_video = max(
                    requested_video_only,
                    key=lambda f: _score_video_format(f, format_name),
                )
                return best_requested_video["url"], (best_requested_video.get("ext") or "mp4").lower()

    if formats and isinstance(formats, list):
        candidates: list[dict[str, Any]] = [f for f in formats if isinstance(f, dict) and f.get("url")]
        if format_name == "mp3":
            audio_only = [
                f
                for f in candidates
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            ]
            if audio_only:
                best_audio = max(audio_only, key=_score_audio_format)
                return best_audio["url"], (best_audio.get("ext") or "m4a").lower()
        else:
            video_candidates = [f for f in candidates if f.get("vcodec") not in (None, "none")]
            if video_candidates:
                best_video = max(video_candidates, key=lambda f: _score_video_format(f, format_name))
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
        normalized_input = _normalize_url(request.url)
        if not _is_http_url(normalized_input):
            raise HTTPException(status_code=422, detail="Only HTTP/HTTPS URLs are supported.")
        source_candidates = _candidate_source_urls(normalized_input)
        extraction_errors: list[str] = []

        for source_url in source_candidates:
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

            try:
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
            except HTTPException as exc:
                detail = str(exc.detail) if getattr(exc, "detail", None) else str(exc)
                extraction_errors.append(f"{source_url} -> {detail}")
                continue
            except Exception as exc:  # noqa: BLE001
                extraction_errors.append(f"{source_url} -> {exc}")
                continue

        detail_message = "Failed to resolve media URL from all candidate links."
        if extraction_errors:
            detail_message = f"{detail_message} Attempts: {' | '.join(extraction_errors)}"
        raise HTTPException(status_code=422, detail=detail_message)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve failed")
        raise HTTPException(status_code=500, detail=f"Resolver error: {exc}") from exc
