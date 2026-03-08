import logging
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urlparse

import fastapi
import yt_dlp.version
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

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

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "si",
    "feature",
    "ref",
    "refsrc",
    "spm",
}

app = FastAPI(title="Video Extractor API", version="2.0.0")
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
    include_alternatives: bool = True


class MediaAlternative(BaseModel):
    url: str
    ext: str
    quality: str | None = None
    width: int | None = None
    height: int | None = None
    tbr: int | None = None


class AppDownloadSpec(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    filename: str
    mime_type: str


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
    filename: str
    mime_type: str
    platform: str | None = None
    alternatives: list[MediaAlternative] = Field(default_factory=list)
    app_download: AppDownloadSpec
    warnings: list[str] = Field(default_factory=list)


def _is_http_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.IGNORECASE))


def _looks_like_direct_media_url(url: str) -> bool:
    return bool(re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov|wav|m3u8)(?:\?|$)", url, re.IGNORECASE))


def _remove_tracking_params(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = []
    for key, value in pairs:
        lowered = key.lower()
        if lowered in TRACKING_QUERY_KEYS or any(lowered.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        filtered.append((key, value))
    new_query = urllib.parse.urlencode(filtered, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0].strip()


def _normalize_url(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("www."):
        cleaned = f"https://{cleaned}"
    cleaned = _without_fragment(cleaned)
    if _is_http_url(cleaned):
        cleaned = _remove_tracking_params(cleaned)
    return cleaned


def _expand_short_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host not in SHORTENER_HOSTS:
        return url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        )
    }

    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                redirected = response.geturl().strip()
                if redirected:
                    cleaned = _normalize_url(redirected)
                    return cleaned or url
        except (urllib.error.URLError, ValueError):
            continue
    return url


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


def _youtube_shorts_to_watch(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    match = re.match(r"^/shorts/([^/?#]+)", parsed.path)
    if not match:
        return None
    video_id = match.group(1)
    return f"https://www.youtube.com/watch?v={video_id}"


def _candidate_source_urls(raw_url: str) -> list[str]:
    candidates: list[str] = []

    def _push(value: str | None) -> None:
        if not value:
            return
        cleaned = _normalize_url(value)
        cleaned = _without_fragment(cleaned)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    normalized = _normalize_url(raw_url)
    _push(normalized)
    _push(_youtube_shorts_to_watch(normalized))
    _push(_tiktok_short_to_canonical(normalized))

    expanded = _expand_short_url(normalized)
    if expanded != normalized:
        _push(expanded)
        _push(_youtube_shorts_to_watch(expanded))
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
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 35,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "cachedir": False,
    }


def _http_headers_mobile() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def _http_headers_desktop() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def _format_selector(format_name: str) -> str:
    if format_name == "mp3":
        return "bestaudio/best"
    if format_name == "mp4_hd":
        return (
            "bv*[height>=720][ext=mp4]+ba[ext=m4a]/"
            "b[height>=720][ext=mp4]/"
            "bv*[height>=720]+ba/"
            "b[height>=720]/"
            "best[ext=mp4]/best"
        )
    return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best[ext=mp4]/best"


def _extract(url: str, format_name: str, *, relaxed: bool, profile: str) -> dict[str, Any]:
    ydl_opts = _base_ydl_opts()
    ydl_opts["format"] = "best" if relaxed else _format_selector(format_name)

    if profile == "mobile":
        ydl_opts["http_headers"] = _http_headers_mobile()
    elif profile == "desktop":
        ydl_opts["http_headers"] = _http_headers_desktop()

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if isinstance(info, dict) and "entries" in info and isinstance(info["entries"], list) and info["entries"]:
            info = info["entries"][0]
        if not info or not isinstance(info, dict):
            raise HTTPException(status_code=422, detail="Failed to extract media metadata.")
        return info


def _extract_with_fallback(url: str, format_name: str) -> tuple[dict[str, Any], str]:
    tried: set[tuple[str, bool, str]] = set()
    fallback_order = [format_name]
    if format_name == "mp4_hd":
        fallback_order.extend(["mp4", "mp3"])
    elif format_name == "mp4":
        fallback_order.append("mp3")
    elif format_name == "mp3":
        fallback_order.append("mp4")

    profiles = ["mobile", "desktop", "default"]
    last_error_message = "Failed to extract media metadata."
    for candidate_format in fallback_order:
        for relaxed in (False, True):
            for profile in profiles:
                key = (candidate_format, relaxed, profile)
                if key in tried:
                    continue
                tried.add(key)
                try:
                    return _extract(url, candidate_format, relaxed=relaxed, profile=profile), candidate_format
                except HTTPException as exc:
                    detail = str(exc.detail) if getattr(exc, "detail", None) else str(exc)
                    last_error_message = detail
                except DownloadError as exc:
                    last_error_message = str(exc)
                except Exception as exc:  # noqa: BLE001
                    last_error_message = str(exc)
    raise HTTPException(status_code=422, detail=last_error_message)


def _protocol_rank(fmt: dict[str, Any]) -> int:
    protocol = str(fmt.get("protocol") or "").lower()
    if protocol.startswith("https"):
        return 4
    if protocol.startswith("http"):
        return 3
    if "m3u8" in protocol:
        return 1
    if "dash" in protocol:
        return 1
    if protocol:
        return 2
    return 0


def _score_audio_format(fmt: dict[str, Any]) -> tuple[int, int, int, int]:
    ext = str(fmt.get("ext") or "").lower()
    vcodec = str(fmt.get("vcodec") or "none").lower()
    acodec = str(fmt.get("acodec") or "none").lower()
    abr = int(fmt.get("abr") or 0)
    ext_rank = 2 if ext in {"m4a", "mp3", "aac", "opus"} else (1 if ext else 0)
    audio_only_rank = 2 if vcodec in {"none", ""} and acodec not in {"none", ""} else 0
    return (audio_only_rank, ext_rank, _protocol_rank(fmt), abr)


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


def _dedupe_url_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


def _collect_format_items(info: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    requested = info.get("requested_formats")
    formats = info.get("formats")
    direct_url = info.get("url")
    ext = str(info.get("ext") or "").lower()

    if isinstance(requested, list):
        items.extend([f for f in requested if isinstance(f, dict) and f.get("url")])
    if isinstance(formats, list):
        items.extend([f for f in formats if isinstance(f, dict) and f.get("url")])
    if isinstance(direct_url, str) and direct_url:
        items.append(
            {
                "url": direct_url,
                "ext": ext,
                "height": int(info.get("height") or 0),
                "width": int(info.get("width") or 0),
                "tbr": int(info.get("tbr") or 0),
                "format_note": info.get("format_note"),
                "vcodec": info.get("vcodec"),
                "acodec": info.get("acodec"),
                "protocol": info.get("protocol"),
            }
        )
    return _dedupe_url_items(items)


def _build_alternative(fmt: dict[str, Any]) -> MediaAlternative:
    return MediaAlternative(
        url=str(fmt.get("url")),
        ext=(str(fmt.get("ext") or "").lower() or "bin"),
        quality=str(fmt.get("format_note") or "") or None,
        width=int(fmt.get("width") or 0) or None,
        height=int(fmt.get("height") or 0) or None,
        tbr=int(fmt.get("tbr") or 0) or None,
    )


def _resolve_media_url(
    info: dict[str, Any],
    format_name: str,
    *,
    include_alternatives: bool,
) -> tuple[str, str, list[MediaAlternative]]:
    candidates = _collect_format_items(info)
    if not candidates:
        raise HTTPException(status_code=422, detail="No direct media URL found for this link.")

    chosen: dict[str, Any] | None = None
    if format_name == "mp3":
        audio_only = [f for f in candidates if str(f.get("vcodec") or "none").lower() in {"none", ""}]
        with_audio = [f for f in audio_only if str(f.get("acodec") or "none").lower() not in {"none", ""}]
        ranked = with_audio or audio_only or candidates
        chosen = max(ranked, key=_score_audio_format)
    else:
        progressive = [
            f
            for f in candidates
            if str(f.get("vcodec") or "none").lower() not in {"none", ""}
            and str(f.get("acodec") or "none").lower() not in {"none", ""}
        ]
        video_only = [f for f in candidates if str(f.get("vcodec") or "none").lower() not in {"none", ""}]
        ranked = progressive or video_only or candidates
        chosen = max(ranked, key=lambda f: _score_video_format(f, format_name))

    media_url = str(chosen.get("url") or "")
    if not media_url:
        raise HTTPException(status_code=422, detail="No direct media URL found for this link.")

    media_ext = str(chosen.get("ext") or "").lower()
    if not media_ext:
        guess = re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov|wav|m3u8)(?:\?|$)", media_url, re.IGNORECASE)
        media_ext = guess.group(1).lower() if guess else ("mp3" if format_name == "mp3" else "mp4")

    alternatives: list[MediaAlternative] = []
    if include_alternatives:
        pool = [f for f in candidates if str(f.get("url")) != media_url]
        if format_name == "mp3":
            pool = sorted(pool, key=_score_audio_format, reverse=True)
        else:
            pool = sorted(pool, key=lambda f: _score_video_format(f, format_name), reverse=True)
        alternatives = [_build_alternative(f) for f in pool[:8]]
    return media_url, media_ext, alternatives


def _safe_filename(title: str, ext: str) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()
    base = re.sub(r"\s+", " ", base)
    if not base:
        base = "video"
    base = base[:120].rstrip(" .")
    extension = ext.lower().strip(".") or "bin"
    return f"{base}.{extension}"


def _mime_for_ext(ext: str) -> str:
    lowered = f".{ext.lower().strip('.')}"
    guessed = mimetypes.types_map.get(lowered) or mimetypes.guess_type(f"x{lowered}")[0]
    return guessed or "application/octet-stream"


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "api_version": app.version,
        "fastapi_version": fastapi.__version__,
        "yt_dlp_version": yt_dlp.version.__version__,
    }


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
                guessed = re.search(r"\.(mp4|m4a|mp3|webm|mkv|mov|wav|m3u8)(?:\?|$)", source_url, re.IGNORECASE)
                guessed_ext = guessed.group(1).lower() if guessed else ("mp3" if request.format == "mp3" else "mp4")
                title = "direct_media"
                filename = _safe_filename(title, guessed_ext)
                mime_type = _mime_for_ext(guessed_ext)
                headers: dict[str, str] = {}
                return ResolveResponse(
                    source_url=source_url,
                    title=title,
                    media_url=source_url,
                    media_ext=guessed_ext,
                    thumbnail=None,
                    duration=None,
                    extractor="direct",
                    requested_format=request.format,
                    headers=headers,
                    filename=filename,
                    mime_type=mime_type,
                    platform=urlparse(source_url).hostname,
                    alternatives=[],
                    app_download=AppDownloadSpec(
                        url=source_url,
                        headers=headers,
                        filename=filename,
                        mime_type=mime_type,
                    ),
                    warnings=[],
                )

            try:
                info, resolved_format = _extract_with_fallback(source_url, request.format)
                media_url, media_ext, alternatives = _resolve_media_url(
                    info,
                    resolved_format,
                    include_alternatives=request.include_alternatives,
                )

                headers = info.get("http_headers") or {}
                if not isinstance(headers, dict):
                    headers = {}

                title = (str(info.get("title") or "video")).strip() or "video"
                filename = _safe_filename(title, media_ext)
                mime_type = _mime_for_ext(media_ext)
                warnings: list[str] = []
                if request.format != resolved_format:
                    warnings.append(
                        f"Requested format '{request.format}' was not available. Used '{resolved_format}' fallback."
                    )

                extractor_name = str(info.get("extractor_key") or info.get("extractor") or "")
                platform = str(urlparse(source_url).hostname or "").lower() or None

                return ResolveResponse(
                    source_url=source_url,
                    title=title,
                    media_url=media_url,
                    media_ext=media_ext,
                    thumbnail=info.get("thumbnail"),
                    duration=info.get("duration"),
                    extractor=extractor_name or None,
                    requested_format=resolved_format,
                    headers={str(k): str(v) for k, v in headers.items()},
                    filename=filename,
                    mime_type=mime_type,
                    platform=platform,
                    alternatives=alternatives,
                    app_download=AppDownloadSpec(
                        url=media_url,
                        headers={str(k): str(v) for k, v in headers.items()},
                        filename=filename,
                        mime_type=mime_type,
                    ),
                    warnings=warnings,
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
