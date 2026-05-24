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

def get_video_info(url: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "platform": info.get("extractor_key", "Unknown"),
        }

def download_video_file(url: str, output_path: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best",
        "outtmpl": output_path + ".%(ext)s",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

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

        await asyncio.to_thread(download_video_file, request.url, output_path)

        for f in os.listdir(tmp_dir):
            if f.startswith(filename):
                final_path = os.path.join(tmp_dir, f)
                actual_ext = f.split(".")[-1]
                return FileResponse(
                    path=final_path,
                    filename=f"dropvid.{actual_ext}",
                    media_type="video/mp4" if actual_ext != "mp3" else "audio/mpeg"
                )

        raise HTTPException(status_code=404, detail="File not found after download")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
