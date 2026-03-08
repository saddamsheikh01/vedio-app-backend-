# Video Extractor Backend (yt-dlp)

This backend resolves most public social/video page links (TikTok, Instagram, Facebook, YouTube, etc.) into a direct media URL.

## API

### `POST /api/resolve`

Request body:

```json
{
  "url": "https://www.tiktok.com/@user/video/123",
  "format": "mp4_hd"
}
```

`format` values:
- `mp3`
- `mp4`
- `mp4_hd`

Behavior:
- Accepts normal page links and direct media links.
- For difficult links, automatically falls back between formats (`mp4_hd -> mp4 -> mp3`).
- Returns required request `headers` when platforms need Referer/User-Agent.

Response example:

```json
{
  "source_url": "https://www.tiktok.com/@user/video/123",
  "title": "My Video",
  "media_url": "https://....mp4",
  "media_ext": "mp4",
  "thumbnail": "https://....jpg",
  "duration": 12,
  "extractor": "TikTok",
  "requested_format": "mp4_hd",
  "headers": {
    "User-Agent": "...",
    "Referer": "..."
  }
}
```

### `GET /health`

Returns:

```json
{"status":"ok"}
```

## Local Run

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Docs:
- `http://localhost:8000/docs`

## Docker Run

```bash
cd backend
docker build -t video-extractor-api .
docker run --rm -p 8000:8000 video-extractor-api
```

## Railway Deploy

This repo already includes [railway.toml](../railway.toml) at root with:
- Nixpacks builder
- health check: `/health`

It also includes [nixpacks.toml](../nixpacks.toml) at root to force a Python-only build plan.
This avoids Railway auto-running `dart pub get` from the Flutter root.

Steps:

1. Push this project to GitHub.
2. In Railway, create a new project from that GitHub repo.
3. Railway will read `railway.toml` automatically.
4. Deploy.

After deploy:
- Open generated domain and test:
  - `GET /health`
  - `POST /api/resolve`

## Flutter Integration

1. App sends original URL + selected format to `/api/resolve`.
2. Backend returns `media_url`.
3. App downloads `media_url` directly.
4. Optionally pass returned `headers` in download request.

## Notes

- Some platforms may require frequent `yt-dlp` updates.
- Private/login-only content cannot be extracted without cookies/auth setup.
- DRM-protected videos cannot be resolved as direct downloadable URLs.
- Respect each platform's terms and local laws.
