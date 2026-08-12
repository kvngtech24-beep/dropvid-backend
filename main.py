import os
import glob
import shutil
import tempfile
import asyncio
from typing import List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import imageio_ffmpeg

app = FastAPI(title="DropVid API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def normalize_netscape_cookies(raw: str) -> str:
    """
    Rebuilds a Netscape-format cookies file field-by-field so it survives
    being pasted through a browser textarea, which often collapses tabs
    into spaces. Each real cookie line has exactly 7 whitespace-separated
    fields; comments/blank lines are passed through untouched.
    """
    lines_out = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines_out.append(line if stripped else "")
            continue
        fields = stripped.split()
        if len(fields) >= 7:
            # Cookie values can't legally contain whitespace, so this is safe.
            fields = fields[:6] + [" ".join(fields[6:])]
            lines_out.append("\t".join(fields))
        else:
            lines_out.append(line)  # leave anything unexpected as-is
    return "\n".join(lines_out) + "\n"


# Render mounts "Secret Files" at /etc/secrets/<filename>. As a fallback
# (some browsers mangle tabs when pasting into that box), we also accept
# the raw cookies.txt contents pasted into a plain Environment Variable
# called YOUTUBE_COOKIES_CONTENT. Either way, we rewrite the content into
# a fresh, correctly-formatted temp file before handing it to yt-dlp.
COOKIES_FILE = None

_secret_candidates = [
    os.environ.get("COOKIES_FILE", ""),
    "/etc/secrets/cookies.txt",
    os.path.join(os.path.dirname(__file__), "cookies.txt"),
]
_secret_path = next((p for p in _secret_candidates if p and os.path.isfile(p)), None)

_raw_cookies = None
if _secret_path:
    with open(_secret_path, "r") as f:
        _raw_cookies = f.read()
elif os.environ.get("YOUTUBE_COOKIES_CONTENT", "").strip():
    _raw_cookies = os.environ["YOUTUBE_COOKIES_CONTENT"]

if _raw_cookies:
    _normalized = normalize_netscape_cookies(_raw_cookies)
    _tmp_cookie_path = os.path.join(tempfile.gettempdir(), "dropvid_cookies.txt")
    with open(_tmp_cookie_path, "w") as f:
        f.write(_normalized)
    COOKIES_FILE = _tmp_cookie_path


def base_ydl_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    return opts


class URLRequest(BaseModel):
    urls: List[str]


class DownloadRequest(BaseModel):
    url: str
    quality: str = "worst"  # "1080", "720", "480", "360", "240", "worst" (Data Saver), "audio"


def get_video_info(url: str):
    ydl_opts = {
        **base_ydl_opts(),
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        duration = info.get("duration", 0) or 0

        def fmt_size(f):
            return f.get("filesize") or f.get("filesize_approx") or 0

        audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        audio_size = max((fmt_size(f) for f in audio_formats), default=0)
        if not audio_size and duration:
            audio_size = int(duration * 128 * 1000 / 8)

        BUCKETS = [1080, 720, 480, 360, 240]
        bucket_sizes = {}
        for b in BUCKETS:
            candidates = [f for f in formats if f.get("height") and b - 40 <= f.get("height") <= b + 40]
            if not candidates:
                continue
            best = max(candidates, key=fmt_size)
            size = fmt_size(best)
            if size and best.get("vcodec") != "none" and best.get("acodec") == "none":
                size += audio_size
            bucket_sizes[b] = size

        # "Data Saver" = the smallest real stream available, mirroring what
        # sites like fdown serve by default (the platform's own low-bitrate
        # SD encode), rather than the best stream within a height cap.
        muxed = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none") and fmt_size(f) > 0]
        if muxed:
            smallest = min(muxed, key=fmt_size)
            data_saver_size = fmt_size(smallest)
        else:
            video_only = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none") and fmt_size(f) > 0]
            if video_only:
                smallest = min(video_only, key=fmt_size)
                data_saver_size = fmt_size(smallest) + audio_size
            else:
                data_saver_size = 0

        qualities = []
        data_saver_mb = round(data_saver_size / 1_000_000, 1) if data_saver_size else None
        qualities.append({"value": "worst", "label": "Data Saver", "size_mb": data_saver_mb})

        for b in sorted(bucket_sizes.keys(), reverse=True):
            size_mb = round(bucket_sizes[b] / 1_000_000, 1) if bucket_sizes[b] else None
            qualities.append({"value": str(b), "label": f"{b}p", "size_mb": size_mb})

        audio_size_mb = round(audio_size / 1_000_000, 1) if audio_size else None
        qualities.append({"value": "audio", "label": "Audio (MP3)", "size_mb": audio_size_mb})

        return {
            "title": info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": duration,
            "platform": info.get("extractor_key", "Unknown"),
            "qualities": qualities,
        }


def build_format_string(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    if quality == "worst":
        return "worst"
    return f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"


def download_to_temp(url: str, quality: str):
    """
    Actually downloads (and, for video, merges) the file server-side using
    yt-dlp + a bundled ffmpeg binary, so we can hand back a real playable
    file instead of a bare CDN URL that the browser can't use directly.
    Returns (filepath, tmpdir) — caller is responsible for cleaning tmpdir.
    """
    tmpdir = tempfile.mkdtemp(prefix="dropvid_")
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    ydl_opts = {
        **base_ydl_opts(),
        "outtmpl": outtmpl,
        "ffmpeg_location": FFMPEG_PATH,
        "format": build_format_string(quality),
    }

    if quality == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    # Postprocessing can rename the file (e.g. .webm -> .mp3), so find
    # whatever actually landed in the temp dir rather than guessing the name.
    produced = glob.glob(os.path.join(tmpdir, "*"))
    if not produced:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("Download finished but no output file was found")

    return produced[0], tmpdir


def cleanup_tmpdir(path: str):
    shutil.rmtree(path, ignore_errors=True)


@app.get("/")
def root():
    return {
        "status": "DropVid API is running \U0001f680",
        "cookies_loaded": bool(COOKIES_FILE),
    }


@app.post("/info")
async def fetch_info(request: URLRequest):
    if len(request.urls) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 URLs allowed")

    results = []
    for url in request.urls:
        if not url.strip():
            continue
        try:
            info = await asyncio.to_thread(get_video_info, url.strip())
            results.append({"url": url, "success": True, **info})
        except Exception as e:
            results.append({"url": url, "success": False, "error": str(e)})

    return {"results": results}


@app.post("/download")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        filepath, tmpdir = await asyncio.to_thread(
            download_to_temp, request.url, request.quality
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    background_tasks.add_task(cleanup_tmpdir, tmpdir)

    ext = os.path.splitext(filepath)[1].lstrip(".") or ("mp3" if request.quality == "audio" else "mp4")
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    return FileResponse(
        path=filepath,
        media_type=media_type,
        filename=f"dropvid.{ext}",
        background=background_tasks,
    )
