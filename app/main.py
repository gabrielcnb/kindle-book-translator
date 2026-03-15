import asyncio
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.translator import LANGUAGES
from app.services.epub_handler import translate_epub
from app.services.pdf_handler import translate_pdf

app = FastAPI(title="Kindle Book Translator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent.parent / "static"
TEMP_DIR = Path("/tmp/book_translator")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory progress store
progress_store: dict[str, int] = {}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/languages")
async def get_languages():
    return {"languages": LANGUAGES}


@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    return {"progress": progress_store.get(job_id, 0)}


@app.post("/translate")
async def translate_book(
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("pt"),
    output_format: str = Form("same"),
):
    MAX_SIZE = 50 * 1024 * 1024  # 50 MB
    content = await file.read()

    if len(content) > MAX_SIZE:
        raise HTTPException(400, "File too large. Maximum size is 50 MB.")

    filename = file.filename or "book"
    ext = Path(filename).suffix.lower()

    if ext not in (".epub", ".pdf"):
        raise HTTPException(400, "Only EPUB and PDF files are supported.")

    job_id = str(uuid.uuid4())
    progress_store[job_id] = 0

    def update_progress(val: int):
        progress_store[job_id] = val

    try:
        if ext == ".epub":
            translated_bytes = await translate_epub(content, source_lang, target_lang, update_progress)
            out_ext = ".epub"
            media_type = "application/epub+zip"
        else:
            translated_bytes = await translate_pdf(content, source_lang, target_lang, update_progress)
            out_ext = ".pdf"
            media_type = "application/pdf"

        out_name = Path(filename).stem + f"_translated_{target_lang}" + out_ext
        out_path = TEMP_DIR / f"{job_id}{out_ext}"
        out_path.write_bytes(translated_bytes)

        progress_store.pop(job_id, None)

        return FileResponse(
            path=str(out_path),
            filename=out_name,
            media_type=media_type,
            background=None,
        )

    except Exception as e:
        progress_store.pop(job_id, None)
        raise HTTPException(500, f"Translation failed: {str(e)}")
