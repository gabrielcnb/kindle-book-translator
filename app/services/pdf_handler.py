import asyncio
import io
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text
from app.services.cover import extract_pdf_cover


CHUNK_TARGET = 4000  # chars per translation chunk
CHARS_PER_PAGE = 2800  # chars per output PDF page
FONT_SIZE = 11
PAGE_W, PAGE_H = 595, 842
TEXT_RECT = fitz.Rect(50, 50, 545, 790)


async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    # Phase 1: Extract cover + text (fast, ~0.2s)
    if progress_callback:
        progress_callback(2)

    cover_img = extract_pdf_cover(file_bytes)

    src_doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages_text = []
    for page in src_doc:
        text = page.get_text("text").strip()
        if text:
            pages_text.append(text)
    src_doc.close()

    if progress_callback:
        progress_callback(5)

    # Phase 2: Merge into translation chunks (~4000 chars each)
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

    # Phase 3: Translate all chunks in parallel
    completed = 0

    async def _translate_chunk(chunk):
        nonlocal completed
        result = await translate_text(chunk, source_lang, target_lang)
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total_chunks * 85))
        return result

    translations = await asyncio.gather(
        *[_translate_chunk(c) for c in chunks]
    )

    if progress_callback:
        progress_callback(92)

    # Phase 4: Build clean PDF with cover
    translated_text = "\n\n".join(translations)
    out_doc = fitz.open()

    # Cover page
    if cover_img:
        try:
            cover_page = out_doc.new_page(width=PAGE_W, height=PAGE_H)
            cover_page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=cover_img)
        except Exception:
            pass

    # Text pages with smart breaks
    pos = 0
    while pos < len(translated_text):
        page_text = translated_text[pos:pos + CHARS_PER_PAGE]

        if pos + CHARS_PER_PAGE < len(translated_text):
            for sep in ["\n\n", "\n", ". ", " "]:
                last = page_text.rfind(sep)
                if last > CHARS_PER_PAGE // 2:
                    page_text = page_text[:last + len(sep)]
                    break

        page = out_doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_textbox(TEXT_RECT, page_text, fontsize=FONT_SIZE, align=0)
        pos += len(page_text)

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
