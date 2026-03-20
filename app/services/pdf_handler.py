import asyncio
import io
import os
import re
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text
from app.services.cover import extract_pdf_cover


CHUNK_TARGET = 4000
FONT_SIZE = 13
TITLE_FONT_SIZE = 18
CHARS_PER_PAGE = 2000

# Chapter detection pattern (matches "CHAPTER X", "CAPITULO X", etc.)
CHAPTER_RE = re.compile(
    r'^(CHAPTER|CAPITULO|CAP[ÍI]TULO|PART|PARTE)\s+[\dIVXLCDM]+',
    re.IGNORECASE | re.MULTILINE,
)

# ── Font resolution ────────────────────────────────────────────────────────

_FONT_PATHS = [
    # Linux (Calibre/Debian)
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/liberation-serif/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/LiberationSerif-Regular.ttf",
    # Windows
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/cambria.ttc",
]

_BOLD_FONT_PATHS = [
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/liberation-serif/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
]


def _find_font(paths: list[str]) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


# ── Main translation ───────────────────────────────────────────────────────

async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    if progress_callback:
        progress_callback(2)

    # Phase 1: Extract original page size, cover, and text
    cover_img = extract_pdf_cover(file_bytes)
    src_doc = fitz.open(stream=file_bytes, filetype="pdf")

    # Match original page dimensions
    page_w = src_doc[0].rect.width if len(src_doc) > 0 else 612
    page_h = src_doc[0].rect.height if len(src_doc) > 0 else 792

    pages_text = []
    for page in src_doc:
        text = page.get_text("text").strip()
        if text:
            pages_text.append(text)
    src_doc.close()

    if progress_callback:
        progress_callback(5)

    # Phase 2: Merge into translation chunks
    chunks = []
    current = ""
    for pt in pages_text:
        if len(current) + len(pt) + 4 > CHUNK_TARGET:
            if current:
                chunks.append(current)
            current = pt
        else:
            current += f"\n\n{pt}" if current else pt
    if current:
        chunks.append(current)

    total_chunks = max(len(chunks), 1)

    # Phase 3: Translate in parallel
    completed = 0

    async def _translate_chunk(chunk):
        nonlocal completed
        result = await translate_text(chunk, source_lang, target_lang)
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total_chunks * 82))
        return result

    translations = await asyncio.gather(
        *[_translate_chunk(c) for c in chunks]
    )

    if progress_callback:
        progress_callback(90)

    # Phase 4: Build PDF
    translated_text = "\n\n".join(translations)
    out_doc = fitz.open()

    # Resolve fonts
    font_path = _find_font(_FONT_PATHS)
    bold_path = _find_font(_BOLD_FONT_PATHS)

    # Margins (1 inch = 72pt)
    margin = 72
    text_rect = fitz.Rect(margin, margin, page_w - margin, page_h - margin)

    # Cover page
    if cover_img:
        try:
            cover_page = out_doc.new_page(width=page_w, height=page_h)
            cover_page.insert_image(
                fitz.Rect(0, 0, page_w, page_h), stream=cover_img
            )
        except Exception:
            pass

    # Split text into paragraphs and paginate
    paragraphs = translated_text.split("\n")
    paragraphs = [p for p in paragraphs if p.strip()]

    current_y = margin
    current_page = None

    def _new_page():
        nonlocal current_page, current_y
        current_page = out_doc.new_page(width=page_w, height=page_h)
        current_y = margin
        return current_page

    _new_page()

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        is_chapter = bool(CHAPTER_RE.match(para))

        # Chapter heading: new page + bigger font
        if is_chapter:
            _new_page()
            current_y += 60  # extra top margin for chapter

        # Estimate height needed
        fs = TITLE_FONT_SIZE if is_chapter else FONT_SIZE
        text_width = text_rect.width

        # Rough estimate: chars per line and lines needed
        avg_char_width = fs * 0.5
        chars_per_line = max(int(text_width / avg_char_width), 1)
        lines = max(len(para) / chars_per_line, 1)
        line_height = fs * 1.5
        needed_height = lines * line_height + (12 if not is_chapter else 24)

        # New page if not enough space
        if current_y + needed_height > page_h - margin:
            _new_page()

        para_rect = fitz.Rect(margin, current_y, page_w - margin, page_h - margin)

        # Insert text with font
        kwargs = {"fontsize": fs, "align": fitz.TEXT_ALIGN_LEFT}
        if is_chapter:
            kwargs["align"] = fitz.TEXT_ALIGN_CENTER

        if font_path and not is_chapter:
            kwargs["fontname"] = "serif"
            kwargs["fontfile"] = font_path
        elif bold_path and is_chapter:
            kwargs["fontname"] = "serifbold"
            kwargs["fontfile"] = bold_path
        elif font_path and is_chapter:
            kwargs["fontname"] = "serif"
            kwargs["fontfile"] = font_path
        else:
            kwargs["fontname"] = "helv"
            kwargs["encoding"] = fitz.TEXT_ENCODING_LATIN

        rc = current_page.insert_textbox(para_rect, para, **kwargs)

        # rc is remaining height (positive = space left, negative = overflow)
        if rc >= 0:
            used_height = (page_h - margin - current_y) - rc
            current_y += used_height + 8  # 8pt paragraph spacing
        else:
            # Text overflowed — move to next page, simplified: just advance
            current_y = page_h  # force new page on next paragraph

    result = out_doc.tobytes()

    if progress_callback:
        progress_callback(100)

    return result


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
