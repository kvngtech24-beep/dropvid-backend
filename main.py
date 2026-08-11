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
)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


class URLRequest(BaseModel):
    urls: List[str]


class DownloadRequest(BaseModel):
    url: str
    quality: str = "720"  # "1080", "720", "480", "360", "audio" — defaults so old clients don't 422


def get_video_info(url: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])

        available_qualities = set()
        for f in formats:
            h = f.get("height")
            if h:
                if h >= 1080:
                    available_qualities.add("1080")
                elif h >= 720:
                    available_qualities.add("720")
                elif h >= 480:
                    available_qualities.add("480")
                elif h >= 360:
                    available_qualities.add("360")
        available_qualities.add("audio")

        return {
            "title": info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": info.get("extractor_key", "Unknown"),
            "qualities": sorted(list(available_qualities - {"audio"}), reverse=True) + ["audio"],
        }


def build_format_string(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
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
        "quiet": True,
        "no_warnings": True,
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
    return {"status": "DropVid API is running \U0001f680"}


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
