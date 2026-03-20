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
PARA_SPACING = 18  # pixels between paragraphs
FIRST_LINE_INDENT = 24  # first-line indent like printed books

CHAPTER_RE = re.compile(
    r'^(CHAPTER|CAPITULO|CAP[ÍI]TULO|PART|PARTE)\s+[\dIVXLCDM]+',
    re.IGNORECASE,
)


# ── Font resolution ────────────────────────────────────────────────────────

_FONT_PATHS = [
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/liberation-serif/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/LiberationSerif-Regular.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
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


# ── Extraction ─────────────────────────────────────────────────────────────

def _join_lines(raw: str, paragraphs: list[str]):
    """Join soft-wrapped lines from a PDF text block into paragraphs."""
    lines = raw.split("\n")
    joined = ""
    for line in lines:
        line = line.strip()
        if not line:
            if joined:
                paragraphs.append(joined)
                joined = ""
            continue
        joined = (joined + " " + line) if joined else line
    if joined:
        paragraphs.append(joined)


def _extract_all(file_bytes: bytes) -> dict:
    """Extract text blocks, images, and metadata from PDF."""
    src_doc = fitz.open(stream=file_bytes, filetype="pdf")
    page_w = src_doc[0].rect.width if len(src_doc) > 0 else 612
    page_h = src_doc[0].rect.height if len(src_doc) > 0 else 792

    pages = []
    all_images = {}  # page_idx -> list of image dicts

    for page_idx in range(len(src_doc)):
        page = src_doc[page_idx]

        # Extract text using blocks (preserves paragraph structure)
        blocks = page.get_text("blocks")
        paragraphs = []
        # Collect raw text blocks for this page
        raw_blocks = []
        for b in blocks:
            if b[6] == 0:
                raw = b[4].strip()
                if raw:
                    raw_blocks.append(raw)

        # Process blocks with look-ahead for multi-block chapter detection
        i = 0
        while i < len(raw_blocks):
            raw = raw_blocks[i]

            # Format 1: "CHAPTER" alone + next block is a number
            if (raw.upper().strip() in ("CHAPTER", "CAPITULO", "CAPÍTULO", "PART", "PARTE")
                    and i + 1 < len(raw_blocks)
                    and raw_blocks[i + 1].strip().isdigit()):
                title = f"{raw} {raw_blocks[i + 1].strip()}"
                paragraphs.append(title)
                i += 2
                continue

            # Format 2: "CHAPTER X – TITLE\nbody text" in one block
            if CHAPTER_RE.match(raw):
                first_nl = raw.find("\n")
                if first_nl > 0:
                    paragraphs.append(raw[:first_nl].strip())
                    body = raw[first_nl:].strip()
                    if body:
                        _join_lines(body, paragraphs)
                else:
                    paragraphs.append(raw)
                i += 1
                continue

            # Normal block
            _join_lines(raw, paragraphs)
            i += 1

        pages.append(paragraphs)

        # Extract images (skip cover page 0, handled separately)
        if page_idx > 0:
            page_images = []
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    img_rect = page.get_image_bbox(img_info)
                    base_image = src_doc.extract_image(xref)
                    if base_image and base_image.get("image"):
                        page_images.append({
                            "stream": base_image["image"],
                            "width": base_image.get("width", 100),
                            "height": base_image.get("height", 100),
                            "rect_y": img_rect.y0,  # vertical position
                            "is_full_page": (
                                img_rect.width > page_w * 0.8
                                and img_rect.height > page_h * 0.8
                            ),
                        })
                except Exception:
                    pass
            if page_images:
                all_images[page_idx] = page_images

    src_doc.close()

    # Build content stream: list of (type, content)
    # Types: "text", "images", "chapter_title"
    content_stream = []
    for page_idx, paragraphs in enumerate(pages):
        if page_idx in all_images:
            content_stream.append(("images", all_images[page_idx]))
        for para in paragraphs:
            # Detect chapter headings and split title from body
            if CHAPTER_RE.match(para):
                # Split: first line/sentence is the title, rest is body
                # In the original PDF, title is on its own line within the block
                lines = para.split("\n", 1)
                if len(lines) > 1 and CHAPTER_RE.match(lines[0]):
                    content_stream.append(("chapter_title", lines[0].strip()))
                    if lines[1].strip():
                        content_stream.append(("text", lines[1].strip()))
                else:
                    # No newline split — try sentence split
                    # Title usually doesn't contain a period
                    first_period = para.find(". ")
                    if 0 < first_period < 80:
                        content_stream.append(("chapter_title", para[:first_period].strip()))
                        content_stream.append(("text", para[first_period + 2:].strip()))
                    else:
                        content_stream.append(("chapter_title", para.strip()))
            else:
                content_stream.append(("text", para))

    return {
        "page_w": page_w,
        "page_h": page_h,
        "content_stream": content_stream,
    }


# ── Main translation ───────────────────────────────────────────────────────

async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    if progress_callback:
        progress_callback(2)

    # Phase 1: Extract
    cover_img = extract_pdf_cover(file_bytes)
    data = _extract_all(file_bytes)
    page_w = data["page_w"]
    page_h = data["page_h"]
    content_stream = data["content_stream"]

    if progress_callback:
        progress_callback(5)

    # Phase 2: Collect all translatable text (text + chapter_title), merge into chunks
    text_items = [
        (i, item[1]) for i, item in enumerate(content_stream)
        if item[0] in ("text", "chapter_title")
    ]
    all_texts = [t for _, t in text_items]

    # Merge into chunks respecting ~4000 chars
    chunks = []
    chunk_map = []  # maps chunk_idx -> list of text_item indices
    current_chunk = ""
    current_indices = []

    for idx, text in enumerate(all_texts):
        if len(current_chunk) + len(text) + 4 > CHUNK_TARGET and current_chunk:
            chunks.append(current_chunk)
            chunk_map.append(current_indices)
            current_chunk = text
            current_indices = [idx]
        else:
            current_chunk += f"\n\n{text}" if current_chunk else text
            current_indices.append(idx)

    if current_chunk:
        chunks.append(current_chunk)
        chunk_map.append(current_indices)

    total_chunks = max(len(chunks), 1)
    completed = 0

    async def _translate_chunk(chunk):
        nonlocal completed
        result = await translate_text(chunk, source_lang, target_lang)
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total_chunks * 80))
        return result

    translations = await asyncio.gather(*[_translate_chunk(c) for c in chunks])

    if progress_callback:
        progress_callback(88)

    # Phase 3: Split translations back to individual paragraphs
    translated_texts = [""] * len(all_texts)
    for chunk_idx, indices in enumerate(chunk_map):
        translated = translations[chunk_idx]
        if len(indices) == 1:
            translated_texts[indices[0]] = translated
        else:
            # Split by double newline to recover paragraphs
            parts = translated.split("\n\n")
            for i, text_idx in enumerate(indices):
                if i < len(parts):
                    translated_texts[text_idx] = parts[i]
                else:
                    translated_texts[text_idx] = all_texts[text_idx]

    # Replace translatable items in content_stream with translations
    text_counter = 0
    for i, item in enumerate(content_stream):
        if item[0] in ("text", "chapter_title"):
            content_stream[i] = (item[0], translated_texts[text_counter])
            text_counter += 1

    if progress_callback:
        progress_callback(92)

    # Phase 4: Build PDF
    font_path = _find_font(_FONT_PATHS)
    bold_path = _find_font(_BOLD_FONT_PATHS)
    margin = 72
    text_rect_w = page_w - 2 * margin
    max_y = page_h - margin

    out_doc = fitz.open()

    # Track chapters for TOC
    chapter_entries = []  # list of (title, page_number)
    toc_page_num = None  # will be filled after we know where TOC lands

    # Cover
    if cover_img:
        try:
            cp = out_doc.new_page(width=page_w, height=page_h)
            cp.insert_image(fitz.Rect(0, 0, page_w, page_h), stream=cover_img)
        except Exception:
            pass

    # Reserve a page for TOC (will be filled after we know all chapter pages)
    toc_placeholder = out_doc.new_page(width=page_w, height=page_h)
    toc_page_num = len(out_doc) - 1

    current_page = out_doc.new_page(width=page_w, height=page_h)
    current_y = margin

    def _new_page():
        nonlocal current_page, current_y
        current_page = out_doc.new_page(width=page_w, height=page_h)
        current_y = margin

    def _font_kwargs(is_title=False):
        kwargs = {}
        if is_title and bold_path:
            kwargs["fontname"] = "serifbold"
            kwargs["fontfile"] = bold_path
        elif font_path:
            kwargs["fontname"] = "serif"
            kwargs["fontfile"] = font_path
        else:
            kwargs["fontname"] = "helv"
            kwargs["encoding"] = fitz.TEXT_ENCODING_LATIN
        return kwargs

    for item_type, content in content_stream:
        if item_type == "images":
            for img in content:
                if img["is_full_page"]:
                    # Full-page image on its own page
                    _new_page()
                    try:
                        current_page.insert_image(
                            fitz.Rect(0, 0, page_w, page_h),
                            stream=img["stream"],
                        )
                    except Exception:
                        pass
                    _new_page()
                else:
                    # Inline image — fit to width, maintain aspect ratio
                    iw = img["width"]
                    ih = img["height"]
                    max_img_w = text_rect_w
                    max_img_h = 300  # cap height
                    scale = min(max_img_w / iw, max_img_h / ih, 1.0)
                    draw_w = iw * scale
                    draw_h = ih * scale

                    if current_y + draw_h + 20 > max_y:
                        _new_page()

                    # Center horizontally
                    x0 = margin + (text_rect_w - draw_w) / 2
                    img_rect = fitz.Rect(x0, current_y, x0 + draw_w, current_y + draw_h)
                    try:
                        current_page.insert_image(img_rect, stream=img["stream"])
                    except Exception:
                        pass
                    current_y += draw_h + 12

        elif item_type == "chapter_title":
            title = content.strip()
            if not title:
                continue

            _new_page()
            chapter_entries.append((title, len(out_doc) - 1))
            current_y += 60

            title_rect = fitz.Rect(margin, current_y, page_w - margin, max_y)
            title_kw = _font_kwargs(is_title=True)
            rc = current_page.insert_textbox(
                title_rect, title,
                fontsize=TITLE_FONT_SIZE, align=fitz.TEXT_ALIGN_CENTER,
                **title_kw
            )
            if rc >= 0:
                used = (max_y - current_y) - rc
                current_y += used + 24
            else:
                current_y += 40
            continue

        elif item_type == "text":
            para = content.strip()
            if not para:
                continue

            fs = FONT_SIZE
            align = fitz.TEXT_ALIGN_LEFT

            # Estimate height
            avg_char_w = fs * 0.48
            chars_per_line = max(int(text_rect_w / avg_char_w), 1)
            num_lines = max(len(para) / chars_per_line, 1)
            needed = num_lines * fs * 1.5 + 10

            if current_y + needed > max_y:
                _new_page()

            # Add first-line indent for body paragraphs
            indent = FIRST_LINE_INDENT
            indented_para = " " * 3 + para  # 3 spaces ~= visual indent with serif

            para_rect = fitz.Rect(margin, current_y, page_w - margin, max_y)
            kwargs = _font_kwargs(is_title=False)
            rc = current_page.insert_textbox(
                para_rect, indented_para, fontsize=fs, align=align, **kwargs
            )

            if rc >= 0:
                used = (max_y - current_y) - rc
                current_y += used + PARA_SPACING
            else:
                # Overflow — text didn't fit. Insert what we can, continue on next page
                current_y = max_y + 1

    # Phase 5: Build TOC page with clickable links + PDF bookmarks
    if chapter_entries and toc_page_num is not None:
        toc_page = out_doc[toc_page_num]
        toc_y = margin
        toc_title = "TABLE OF CONTENTS"

        # Title
        title_rect = fitz.Rect(margin, toc_y, page_w - margin, toc_y + 40)
        title_kwargs = _font_kwargs(is_title=True)
        toc_page.insert_textbox(
            title_rect, toc_title,
            fontsize=16, align=fitz.TEXT_ALIGN_CENTER, **title_kwargs
        )
        toc_y += 50

        # Chapter links
        link_font_kwargs = _font_kwargs(is_title=False)
        for title, page_num in chapter_entries:
            if toc_y + 20 > max_y:
                break  # TOC overflow — stop (rare for <30 chapters)

            # Truncate long titles
            display = title[:60] + "..." if len(title) > 60 else title
            entry_rect = fitz.Rect(margin, toc_y, page_w - margin, toc_y + 18)

            toc_page.insert_textbox(
                entry_rect, display,
                fontsize=11, align=fitz.TEXT_ALIGN_LEFT,
                color=(0.2, 0.3, 0.7),  # blue-ish link color
                **link_font_kwargs
            )

            # Add clickable link annotation
            link = {
                "kind": fitz.LINK_GOTO,
                "from": entry_rect,
                "page": page_num,
                "to": fitz.Point(margin, margin),
            }
            toc_page.insert_link(link)

            toc_y += 20

        # Set PDF outline/bookmarks
        toc_list = []
        for title, page_num in chapter_entries:
            # set_toc format: [level, title, page (1-based)]
            toc_list.append([1, title, page_num + 1])
        out_doc.set_toc(toc_list)

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
