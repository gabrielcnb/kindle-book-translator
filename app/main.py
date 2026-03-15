import asyncio
import json
import tempfile
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
TEMP_DIR = Path(tempfile.gettempdir()) / "book_translator"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

MAX_SIZE = 50 * 1024 * 1024  # 50 MB

# job_id -> {"status": "running"|"done"|"error", "progress": int, ...}
jobs: dict[str, dict] = {}
# job_id -> asyncio.Queue for SSE events
job_queues: dict[str, asyncio.Queue] = {}


async def _run_translation(
    job_id: str,
    content: bytes,
    ext: str,
    source_lang: str,
    target_lang: str,
    filename: str,
):
    queue = job_queues.get(job_id)

    def push(event: dict):
        if queue:
            queue.put_nowait(event)

    def on_progress(val: int):
        jobs[job_id]["progress"] = val
        push({"progress": val, "status": "running"})

    try:
        if ext == ".epub":
            result = await translate_epub(content, source_lang, target_lang, on_progress)
            out_ext = ".epub"
            media_type = "application/epub+zip"
        else:
            result = await translate_pdf(content, source_lang, target_lang, on_progress)
            out_ext = ".pdf"
            media_type = "application/pdf"

        out_name = Path(filename).stem + f"_translated_{target_lang}" + out_ext
        out_path = TEMP_DIR / f"{job_id}{out_ext}"
        out_path.write_bytes(result)

        jobs[job_id] = {
            "status": "done",
            "progress": 100,
            "file_path": str(out_path),
            "filename": out_name,
            "media_type": media_type,
        }
        push({"progress": 100, "status": "done", "download_url": f"/download/{job_id}"})

    except Exception as e:
        jobs[job_id] = {"status": "error", "progress": 0, "error": str(e)}
        push({"progress": 0, "status": "error", "error": str(e)})

    finally:
        job_queues.pop(job_id, None)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/languages")
async def get_languages():
    return {"languages": LANGUAGES}


@app.post("/translate")
async def start_translation(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("pt"),
):
    content = await file.read()

    if len(content) > MAX_SIZE:
        raise HTTPException(400, "File too large. Maximum size is 50 MB.")

    filename = file.filename or "book"
    ext = Path(filename).suffix.lower()

    if ext not in (".epub", ".pdf"):
        raise HTTPException(400, "Only EPUB and PDF files are supported.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "progress": 0}
    job_queues[job_id] = asyncio.Queue()

    background_tasks.add_task(
        _run_translation, job_id, content, ext, source_lang, target_lang, filename
    )

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    """SSE endpoint — streams real translation progress."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")

    async def generator():
        # If job already finished before SSE connected, send final state immediately
        job = jobs.get(job_id, {})
        if job.get("status") == "done":
            yield f"data: {json.dumps({'progress': 100, 'status': 'done', 'download_url': f'/download/{job_id}'})}\n\n"
            return
        if job.get("status") == "error":
            yield f"data: {json.dumps({'progress': 0, 'status': 'error', 'error': job.get('error', '')})}\n\n"
            return

        queue = job_queues.get(job_id)
        if not queue:
            yield f"data: {json.dumps({'progress': 0, 'status': 'error', 'error': 'Queue gone'})}\n\n"
            return

        # Send current progress immediately so frontend doesn't start at 0
        yield f"data: {json.dumps({'progress': jobs[job_id].get('progress', 0), 'status': 'running'})}\n\n"

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("status") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield "data: {\"heartbeat\": true}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "File not ready or not found.")

    file_path = job["file_path"]
    if not Path(file_path).exists():
        raise HTTPException(404, "File expired or missing.")

    return FileResponse(
        path=file_path,
        filename=job["filename"],
        media_type=job["media_type"],
    )
