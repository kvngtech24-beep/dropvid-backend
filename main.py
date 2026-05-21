from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import yt_dlp
import asyncio

app = FastAPI(title="DropVid API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with your Vercel URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class URLRequest(BaseModel):
    urls: List[str]

class DownloadRequest(BaseModel):
    url: str
    quality: str  # "1080", "720", "480", "360", "audio"
def get_download_url(url: str, quality: str):
    format_str = build_format_string(quality)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": format_str,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])

        if quality == "audio":
            for f in reversed(formats):
                if f.get("acodec") != "none" and f.get("vcodec") == "none":
                    if f.get("url"):
                        return f.get("url"), "mp3"
            # fallback
            for f in reversed(formats):
                if f.get("acodec") != "none" and f.get("url"):
                    return f.get("url"), "mp3"
        else:
            # Try to find best format matching quality
            for f in reversed(formats):
                h = f.get("height", 0)
                if h and h <= int(quality) and f.get("url"):
                    return f.get("url"), "mp4"
            # fallback to any available
            for f in reversed(formats):
                if f.get("url"):
                    return f.get("url"), "mp4"

        return None, None
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

def get_download_url(url: str, quality: str):
    format_str = build_format_string(quality)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": format_str,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if quality == "audio":
            for f in reversed(info.get("formats", [])):
                if f.get("acodec") != "none" and f.get("vcodec") == "none":
                    return f.get("url"), "mp3"
            return info.get("url"), "mp3"
        else:
            return info.get("url"), "mp4"

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
        download_url, ext = await asyncio.to_thread(
            get_download_url, request.url, request.quality
        )
        if not download_url:
            raise HTTPException(status_code=404, detail="No download URL found")
        return {"download_url": download_url, "ext": ext}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
