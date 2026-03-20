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
    """Translate a list of span texts in batches, preserving order."""
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


async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    src_doc = fitz.open(stream=file_bytes, filetype="pdf")
    out_doc = fitz.open()
    total = len(src_doc)

    for page_idx, page in enumerate(src_doc):
        blocks = page.get_text("dict")["blocks"]
        new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)

        # Copy images from original page
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            img_rect = page.get_image_bbox(img_info)
            try:
                base_image = src_doc.extract_image(xref)
                new_page.insert_image(img_rect, stream=base_image["image"])
            except Exception:
                pass

        # Collect all spans for this page
        span_data: list[tuple] = []  # (bbox, fontsize, color, text)
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
                    span_data.append((
                        fitz.Rect(span["bbox"]),
                        span.get("size", 11),
                        (r, g, b),
                        text,
                    ))

        if span_data:
            # Batch-translate all spans for the page at once
            texts = [s[3] for s in span_data]
            translations = await _batch_translate_spans(texts, source_lang, target_lang)

            for (bbox, fontsize, color, _), translated in zip(span_data, translations):
                try:
                    new_page.insert_textbox(bbox, translated, fontsize=fontsize, color=color, align=0)
                except Exception:
                    try:
                        new_page.insert_text((bbox.x0, bbox.y1), translated, fontsize=fontsize, color=color)
                    except Exception:
                        pass

        if progress_callback:
            progress_callback(int((page_idx + 1) / total * 90))

    out_bytes = out_doc.tobytes()

    if progress_callback:
        progress_callback(100)

    return out_bytes


async def epub_to_pdf(epub_bytes: bytes) -> bytes:
    """Convert EPUB to simple PDF via text extraction."""
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
