import asyncio
import io
import re
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text

BATCH_SEP = "KBTXSEP"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))
BATCH_SIZE = 30


async def _batch_translate_spans(
    texts: list[str],
    source_lang: str,
    target_lang: str,
) -> list[str]:
    results = list(texts)

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]

        nonempty = [(i, t) for i, t in enumerate(batch) if t.strip()]
        if not nonempty:
            continue

        indices, nonempty_texts = zip(*nonempty)

        joined = f" {BATCH_SEP} ".join(nonempty_texts)
        translated_joined = await translate_text(joined, source_lang, target_lang)
        parts = [p.strip() for p in BATCH_SEP_RE.split(translated_joined)]

        if len(parts) == len(nonempty_texts):
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = parts[local_i]
        else:
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = await translate_text(
                    nonempty_texts[local_i], source_lang, target_lang
                )

    return results


# ── Page data extraction (sequential, fast, no I/O) ────────────────────────

def _extract_page_data(src_doc, page_idx: int) -> dict:
    """Extract all data from a page into plain Python objects (no fitz refs)."""
    page = src_doc[page_idx]
    width = page.rect.width
    height = page.rect.height
    blocks = page.get_text("dict")["blocks"]

    # Images
    images = []
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        img_rect = page.get_image_bbox(img_info)
        try:
            base_image = src_doc.extract_image(xref)
            images.append({
                "rect": (img_rect.x0, img_rect.y0, img_rect.x1, img_rect.y1),
                "stream": base_image["image"],
            })
        except Exception:
            pass

    # Spans
    spans = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                color_int = span.get("color", 0)
                r = ((color_int >> 16) & 0xFF) / 255
                g = ((color_int >> 8) & 0xFF) / 255
                b = (color_int & 0xFF) / 255
                spans.append({
                    "bbox": tuple(span["bbox"]),
                    "size": span.get("size", 11),
                    "color": (r, g, b),
                    "text": text,
                })

    return {
        "idx": page_idx,
        "width": width,
        "height": height,
        "images": images,
        "spans": spans,
    }


# ── Translate one page's spans (async, parallel-safe) ──────────────────────

async def _translate_page_spans(
    page_data: dict,
    source_lang: str,
    target_lang: str,
) -> dict:
    """Translate all spans for a page. Returns page_data with translated texts."""
    spans = page_data["spans"]
    if not spans:
        return page_data

    texts = [s["text"] for s in spans]
    translations = await _batch_translate_spans(texts, source_lang, target_lang)

    for span, translated in zip(spans, translations):
        span["translated"] = translated

    return page_data


# ── Build output PDF (sequential, uses fitz) ───────────────────────────────

def _build_output_pdf(pages_data: list[dict]) -> bytes:
    """Assemble translated pages into a new PDF."""
    out_doc = fitz.open()

    for pd in pages_data:
        new_page = out_doc.new_page(width=pd["width"], height=pd["height"])

        # Images
        for img in pd["images"]:
            r = img["rect"]
            try:
                new_page.insert_image(fitz.Rect(*r), stream=img["stream"])
            except Exception:
                pass

        # Translated spans
        for span in pd["spans"]:
            text = span.get("translated", span["text"])
            bbox = fitz.Rect(span["bbox"])
            try:
                new_page.insert_textbox(
                    bbox, text,
                    fontsize=span["size"], color=span["color"], align=0,
                )
            except Exception:
                try:
                    new_page.insert_text(
                        (bbox.x0, bbox.y1), text,
                        fontsize=span["size"], color=span["color"],
                    )
                except Exception:
                    pass

    return out_doc.tobytes()


# ── Main entry point ───────────────────────────────────────────────────────

async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    src_doc = fitz.open(stream=file_bytes, filetype="pdf")
    total = len(src_doc)

    # Phase 1: Extract all page data (sequential, fast)
    if progress_callback:
        progress_callback(2)
    pages_data = [_extract_page_data(src_doc, i) for i in range(total)]
    src_doc.close()

    if progress_callback:
        progress_callback(5)

    # Phase 2: Translate pages in parallel (capped to avoid GT rate-limit)
    PAGE_CONCURRENCY = 6
    page_sem = asyncio.Semaphore(PAGE_CONCURRENCY)
    completed = 0

    async def _translate_with_progress(pd):
        nonlocal completed
        async with page_sem:
            result = await _translate_page_spans(pd, source_lang, target_lang)
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total * 85))
        return result

    pages_data = await asyncio.gather(
        *[_translate_with_progress(pd) for pd in pages_data],
        return_exceptions=True,
    )

    # Filter out any failed pages (keep original data)
    for i, pd in enumerate(pages_data):
        if isinstance(pd, Exception):
            print(f"[pdf_handler] page {i} failed: {pd}")
            pages_data[i] = {"idx": i, "width": 595, "height": 842,
                             "images": [], "spans": []}

    # Phase 3: Build output PDF (sequential)
    if progress_callback:
        progress_callback(92)
    out_bytes = _build_output_pdf(pages_data)

    if progress_callback:
        progress_callback(100)

    return out_bytes


async def epub_to_pdf(epub_bytes: bytes) -> bytes:
    pages_text = []
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
            from bs4 import BeautifulSoup
            for name in z.namelist():
                if name.endswith((".html", ".xhtml", ".htm")):
                    content = z.read(name)
                    soup = BeautifulSoup(content, "lxml")
                    text = soup.get_text(separator="\n")
                    pages_text.append(text)
    except Exception:
        return b""

    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(50, 50, 550, 780),
            text,
            fontsize=11,
            align=0,
        )

    return doc.tobytes()
