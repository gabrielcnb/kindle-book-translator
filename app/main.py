import asyncio
import json
import logging
import os
import signal
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.translator import LANGUAGES, ENGINES
from app.services.epub_handler import translate_epub
from app.services.pdf_handler import translate_pdf
from app.services.converter import epub_to_pdf, pdf_to_epub, calibre_available, convert_to_epub
from app.services.cover import extract_epub_cover, extract_pdf_cover
from app import cache

logger = logging.getLogger(__name__)

VERSION = "3.0.0"

app = FastAPI(title="Kindle Book Translator", version=VERSION)

cors_origins = os.getenv("CORS_ORIGINS", "https://kindle-book-translator.onrender.com,http://localhost:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

STATIC_DIR = Path(__file__).parent.parent / "static"
TEMP_DIR = Path(tempfile.gettempdir()) / "book_translator"
TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

MAX_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_JOB_AGE = 3600  # 1 hour
CLEANUP_INTERVAL = 300  # 5 minutes
RATE_LIMIT_MAX = 5  # max jobs per IP per hour
SUPPORTED_INPUT = frozenset({".epub", ".pdf", ".mobi", ".azw3"})

jobs: dict[str, dict] = {}
job_queues: dict[str, asyncio.Queue] = {}
_rate_limits: dict[str, list[float]] = defaultdict(list)  # IP -> list of timestamps


# ──────────────────────────────────────────────────────────────────────────────
# Job cleanup
# ──────────────────────────────────────────────────────────────────────────────

def _cleanup_old_jobs():
    """Remove jobs older than MAX_JOB_AGE and delete their temp files."""
    now = time.time()
    expired = [
        jid for jid, j in jobs.items()
        if now - j.get("created_at", 0) > MAX_JOB_AGE
        and j.get("status") in ("done", "error")
    ]
    for jid in expired:
        file_path = jobs[jid].get("file_path")
        if file_path:
            try:
                Path(file_path).unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to delete temp file: %s", file_path)
        del jobs[jid]
        job_queues.pop(jid, None)
    if expired:
        logger.info("Cleaned up %d expired jobs", len(expired))


# TODO: Migrate to lifespan context manager when upgrading FastAPI
@app.on_event("startup")
async def _start_periodic_cleanup():
    async def _loop():
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            _cleanup_old_jobs()
    asyncio.create_task(_loop())


# ──────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────────────────────

_shutting_down = False

@app.on_event("startup")
async def _setup_graceful_shutdown():
    def _handle_sigterm(*_):
        global _shutting_down
        _shutting_down = True
        logger.info("SIGTERM received, waiting for active jobs to finish (max 30s)…")
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        pass  # Not available on all platforms


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_job_id(job_id: str) -> None:
    """Validate that job_id is a valid UUID."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job ID.")


def _check_rate_limit(ip: str) -> None:
    """Enforce per-IP rate limiting."""
    now = time.time()
    timestamps = _rate_limits[ip]
    # Remove entries older than 1 hour
    _rate_limits[ip] = [t for t in timestamps if now - t < 3600]
    if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "Too many requests. Please wait before submitting another job.")
    _rate_limits[ip].append(now)


def _push(job_id: str, event: dict):
    q = job_queues.get(job_id)
    if q:
        q.put_nowait(event)


async def _run_translation(
    job_id: str,
    content: bytes,
    ext: str,
    source_lang: str,
    target_lang: str,
    filename: str,
    bilingual: bool,
    engine: str = "google",
    glossary: list[str] | None = None,
):
    def on_progress(val: int):
        if jobs[job_id].get("cancelled"):
            raise asyncio.CancelledError("Job cancelled by user")
        jobs[job_id]["progress"] = val
        _push(job_id, {"progress": val, "status": "running"})

    try:
        # Convert MOBI/AZW3 to EPUB first
        original_ext = ext
        if ext in (".mobi", ".azw3"):
            content = await convert_to_epub(content, ext)
            ext = ".epub"

        if ext == ".epub":
            result = await translate_epub(
                content, source_lang, target_lang, on_progress,
                bilingual=bilingual, engine=engine, glossary=glossary,
            )
            out_ext, media = ".epub", "application/epub+zip"
        else:
            result = await translate_pdf(
                content, source_lang, target_lang, on_progress,
                engine=engine, glossary=glossary,
            )
            out_ext, media = ".pdf", "application/pdf"

        suffix = "_bilingual" if bilingual else f"_translated_{target_lang}"
        out_name = Path(filename).stem + suffix + out_ext
        out_path = TEMP_DIR / f"{job_id}{out_ext}"
        out_path.write_bytes(result)

        jobs[job_id].update({
            "status": "done", "progress": 100,
            "file_path": str(out_path), "filename": out_name, "media_type": media,
        })
        _push(job_id, {"progress": 100, "status": "done", "download_url": f"/download/{job_id}"})

    except asyncio.CancelledError:
        jobs[job_id].update({"status": "error", "progress": 0, "error": "Translation cancelled."})
        _push(job_id, {"progress": 0, "status": "error", "error": "Translation cancelled."})
    except Exception as e:
        logger.error("Translation job %s failed: %s", job_id, e, exc_info=True)
        user_msg = "Translation failed. Please try again or use a different file."
        jobs[job_id].update({"status": "error", "progress": 0, "error": user_msg})
        _push(job_id, {"progress": 0, "status": "error", "error": user_msg})
    finally:
        job_queues.pop(job_id, None)


async def _run_conversion(
    job_id: str,
    content: bytes,
    src_ext: str,
    out_ext: str,
    filename: str,
):
    def on_progress(val: int):
        jobs[job_id]["progress"] = val
        _push(job_id, {"progress": val, "status": "running"})

    try:
        on_progress(10)
        # Convert MOBI/AZW3 to EPUB first if needed
        if src_ext in (".mobi", ".azw3"):
            content = await convert_to_epub(content, src_ext)
            src_ext = ".epub"

        if src_ext == ".epub" and out_ext == ".pdf":
            result = await epub_to_pdf(content)
            media = "application/pdf"
        elif src_ext == ".pdf" and out_ext == ".epub":
            result = await pdf_to_epub(content, title=Path(filename).stem)
            media = "application/epub+zip"
        else:
            raise ValueError(f"Unsupported conversion: {src_ext} → {out_ext}")

        on_progress(95)
        out_name = Path(filename).stem + out_ext
        out_path = TEMP_DIR / f"{job_id}{out_ext}"
        out_path.write_bytes(result)

        jobs[job_id].update({
            "status": "done", "progress": 100,
            "file_path": str(out_path), "filename": out_name, "media_type": media,
        })
        _push(job_id, {"progress": 100, "status": "done", "download_url": f"/download/{job_id}"})

    except Exception as e:
        logger.error("Conversion job %s failed: %s", job_id, e, exc_info=True)
        user_msg = "Conversion failed. Please try again or use a different file."
        jobs[job_id].update({"status": "error", "progress": 0, "error": user_msg})
        _push(job_id, {"progress": 0, "status": "error", "error": user_msg})
    finally:
        job_queues.pop(job_id, None)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/languages")
async def get_languages():
    return {"languages": LANGUAGES}


@app.get("/info")
async def info():
    return {
        "calibre_available": calibre_available(),
        "cache_stats": cache.stats(),
        "version": VERSION,
        "engines": list(ENGINES.keys()),
        "supported_formats": list(SUPPORTED_INPUT),
    }


@app.post("/cover")
async def get_cover(file: UploadFile = File(...)):
    """Return book cover as JPEG/PNG image."""
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "File too large.")

    ext = Path(file.filename or "").suffix.lower()
    if ext == ".epub":
        img = extract_epub_cover(content)
        mime = "image/jpeg"
    elif ext == ".pdf":
        img = extract_pdf_cover(content)
        mime = "image/jpeg"
    elif ext in (".mobi", ".azw3"):
        # Convert to EPUB first, then extract cover
        try:
            epub_bytes = await convert_to_epub(content, ext)
            img = extract_epub_cover(epub_bytes)
            mime = "image/jpeg"
        except Exception:
            raise HTTPException(404, "Could not extract cover from this file.")
    else:
        raise HTTPException(400, f"Unsupported format. Accepted: {', '.join(SUPPORTED_INPUT)}")

    if not img:
        raise HTTPException(404, "No cover found.")

    return Response(content=img, media_type=mime)


@app.post("/translate")
async def start_translation(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("pt"),
    bilingual: str = Form("false"),
    engine: str = Form("google"),
    glossary: str = Form(""),
):
    _check_rate_limit(request.client.host if request.client else "unknown")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "File too large. Maximum size is 50 MB.")

    filename = file.filename or "book"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_INPUT:
        raise HTTPException(400, f"Unsupported format. Accepted: {', '.join(SUPPORTED_INPUT)}")

    if target_lang not in LANGUAGES:
        raise HTTPException(400, f"Unsupported target language: {target_lang}")

    if source_lang != "auto" and source_lang not in LANGUAGES:
        raise HTTPException(400, f"Unsupported source language: {source_lang}")

    if source_lang != "auto" and source_lang == target_lang:
        raise HTTPException(400, "Source and target languages must be different.")

    if engine not in ENGINES:
        raise HTTPException(400, f"Unsupported engine. Available: {', '.join(ENGINES)}")

    # Parse glossary terms
    glossary_terms = [t.strip() for t in glossary.split("\n") if t.strip()] if glossary else None

    _cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "progress": 0, "created_at": time.time()}
    job_queues[job_id] = asyncio.Queue()

    background_tasks.add_task(
        _run_translation,
        job_id, content, ext, source_lang, target_lang, filename,
        bilingual.lower() == "true", engine, glossary_terms,
    )
    return {"job_id": job_id}


@app.post("/convert")
async def start_conversion(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_format: str = Form(...),
):
    """Convert between EPUB, PDF, MOBI, AZW3."""
    _check_rate_limit(request.client.host if request.client else "unknown")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "File too large.")

    filename = file.filename or "book"
    src_ext = Path(filename).suffix.lower()
    out_ext = f".{output_format.lstrip('.').lower()}"

    if src_ext == out_ext:
        raise HTTPException(400, "Source and output format are the same.")
    if src_ext not in SUPPORTED_INPUT or out_ext not in (".epub", ".pdf"):
        raise HTTPException(400, f"Supported input: {', '.join(SUPPORTED_INPUT)}. Output: EPUB, PDF.")

    _cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "progress": 0, "created_at": time.time()}
    job_queues[job_id] = asyncio.Queue()

    background_tasks.add_task(
        _run_conversion, job_id, content, src_ext, out_ext, filename
    )
    return {"job_id": job_id}


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running job."""
    _validate_job_id(job_id)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("status") != "running":
        raise HTTPException(400, "Job is not running.")
    job["cancelled"] = True
    return {"status": "cancelled"}


@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    _validate_job_id(job_id)
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")

    async def generator():
        job = jobs.get(job_id, {})
        if job.get("status") == "done":
            yield f"data: {json.dumps({'progress': 100, 'status': 'done', 'download_url': f'/download/{job_id}'})}\n\n"
            return
        if job.get("status") == "error":
            yield f"data: {json.dumps({'progress': 0, 'status': 'error', 'error': job.get('error', '')})}\n\n"
            return

        q = job_queues.get(job_id)
        if not q:
            yield f"data: {json.dumps({'progress': 0, 'status': 'error', 'error': 'Queue gone'})}\n\n"
            return

        yield f"data: {json.dumps({'progress': jobs[job_id].get('progress', 0), 'status': 'running'})}\n\n"

        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("status") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield 'data: {"heartbeat": true}\n\n'

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/status/{job_id}")
async def job_status(job_id: str):
    """Polling fallback for when SSE disconnects (e.g. mobile app switch)."""
    _validate_job_id(job_id)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    result = {"progress": job.get("progress", 0), "status": job.get("status", "running")}
    if job.get("status") == "done":
        result["download_url"] = f"/download/{job_id}"
    if job.get("status") == "error":
        result["error"] = job.get("error", "")
    return result


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    _validate_job_id(job_id)
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "File not ready or not found.")
    if not Path(job["file_path"]).exists():
        raise HTTPException(404, "File expired.")
    return FileResponse(
        path=job["file_path"],
        filename=job["filename"],
        media_type=job["media_type"],
    )
