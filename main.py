from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import yt_dlp
import asyncio
import os
import uuid
import tempfile

app = FastAPI(title="DropVid API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class URLRequest(BaseModel):
    urls: List[str]

class DownloadRequest(BaseModel):
    url: str
    quality: str

def get_video_info(url: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
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

def download_video_file(url: str, quality: str, output_path: str):
    if quality == "audio":
        format_str = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }]
        ext = "mp3"
    else:
        format_str = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best[height<={quality}]"
        postprocessors = [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }]
        ext = "mp4"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": format_str,
        "outtmpl": output_path,
        "postprocessors": postprocessors,
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return ext

@app.get("/")
def root():
    return {"status": "DropVid API is running 🚀"}

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
async def download_video(request: DownloadRequest):
    try:
        tmp_dir = tempfile.mkdtemp()
        filename = str(uuid.uuid4())
        output_path = os.path.join(tmp_dir, filename)

        ext = await asyncio.to_thread(
            download_video_file, request.url, request.quality, output_path
        )

        # Find the actual file (yt-dlp adds extension)
        for f in os.listdir(tmp_dir):
            if f.startswith(filename):
                final_path = os.path.join(tmp_dir, f)
                return FileResponse(
                    path=final_path,
                    filename=f"dropvid.{ext}",
                    media_type="video/mp4" if ext == "mp4" else "audio/mpeg"
                )

        raise HTTPException(status_code=404, detail="File not found after download")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
