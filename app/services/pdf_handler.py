import asyncio
import io
import os
import re
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text

CHUNK_TARGET = 4000
BATCH_SEP = "KBTXSEP"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))
BATCH_SIZE = 30

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
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
]

_font_cache: str | None = None


def _find_font() -> str | None:
    global _font_cache
    if _font_cache is None:
        for p in _FONT_PATHS:
            if os.path.exists(p):
                _font_cache = p
                break
    return _font_cache


# ── Main: overlay translation (block-level) ────────────────────────────────

async def translate_pdf(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    if progress_callback:
        progress_callback(2)

    src_doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_pages = len(src_doc)
    font_path = _find_font()

    # Phase 1: Extract text BLOCKS (not spans) from all pages
    page_blocks: list[list[dict]] = []
    all_texts: list[str] = []
    text_index_map: list[tuple[int, int]] = []  # (page_idx, block_idx)

    for page_idx in range(total_pages):
        page = src_doc[page_idx]
        blocks_data = []

        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue

            # Collect full text and dominant style from this block
            lines_text = []
            total_size = 0
            total_color = 0
            span_count = 0
            has_content = False

            for line in block.get("lines", []):
                line_parts = []
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if text.strip():
                        has_content = True
                        line_parts.append(text)
                        total_size += span.get("size", 11)
                        total_color += span.get("color", 0)
                        span_count += 1
                if line_parts:
                    lines_text.append(" ".join(line_parts))

            if not has_content:
                continue

            full_text = " ".join(lines_text).strip()
            if len(full_text) < 2:
                continue

            avg_size = total_size / span_count if span_count else 11
            avg_color = int(total_color / span_count) if span_count else 0

            blocks_data.append({
                "bbox": block["bbox"],
                "text": full_text,
                "size": avg_size,
                "color": avg_color,
            })
            text_index_map.append((page_idx, len(blocks_data) - 1))
            all_texts.append(full_text)

        page_blocks.append(blocks_data)

    src_doc.close()

    if progress_callback:
        progress_callback(5)

    # Phase 2: Batch translate all block texts
    total_texts = len(all_texts)
    chunks = []
    chunk_indices = []
    current_chunk = ""
    current_indices = []

    for idx, text in enumerate(all_texts):
        if len(current_chunk) + len(text) + len(BATCH_SEP) + 4 > CHUNK_TARGET and current_chunk:
            chunks.append(current_chunk)
            chunk_indices.append(current_indices)
            current_chunk = text
            current_indices = [idx]
        else:
            current_chunk = (current_chunk + f" {BATCH_SEP} " + text) if current_chunk else text
            current_indices.append(idx)
    if current_chunk:
        chunks.append(current_chunk)
        chunk_indices.append(current_indices)

    translated_all = list(all_texts)  # default: keep originals
    completed = 0
    total_chunks = max(len(chunks), 1)

    async def _translate_chunk(chunk, indices):
        nonlocal completed
        result = await translate_text(chunk, source_lang, target_lang)
        parts = [p.strip() for p in BATCH_SEP_RE.split(result)]
        if len(parts) == len(indices):
            for i, idx in enumerate(indices):
                translated_all[idx] = parts[i]
        else:
            # Fallback: translate individually
            for i, idx in enumerate(indices):
                try:
                    translated_all[idx] = await translate_text(all_texts[idx], source_lang, target_lang)
                except Exception:
                    pass
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total_chunks * 80))

    await asyncio.gather(*[
        _translate_chunk(chunk, indices)
        for chunk, indices in zip(chunks, chunk_indices)
    ])

    # Map translations back
    for text_idx, (page_idx, block_idx) in enumerate(text_index_map):
        page_blocks[page_idx][block_idx]["translated"] = translated_all[text_idx]

    if progress_callback:
        progress_callback(88)

    # Phase 3: Overlay on original PDF
    out_doc = fitz.open(stream=file_bytes, filetype="pdf")

    for page_idx in range(total_pages):
        page = out_doc[page_idx]
        blocks = page_blocks[page_idx]
        if not blocks:
            continue

        # Redact all text block areas
        for bl in blocks:
            rect = fitz.Rect(bl["bbox"])
            page.add_redact_annot(rect + (-2, -2, 2, 2), fill=(1, 1, 1))

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Insert translated text into each block's bbox
        for bl in blocks:
            translated = bl.get("translated", bl["text"])
            if not translated:
                continue

            rect = fitz.Rect(bl["bbox"])
            orig_size = bl["size"]
            color_int = bl["color"]

            # Parse color
            if isinstance(color_int, int) and color_int != 0:
                r = ((color_int >> 16) & 0xFF) / 255
                g = ((color_int >> 8) & 0xFF) / 255
                b = (color_int & 0xFF) / 255
                color = (r, g, b)
            else:
                color = (0, 0, 0)

            # Scale font if translated text is much longer
            len_ratio = len(translated) / max(len(bl["text"]), 1)
            font_size = orig_size
            if len_ratio > 1.2:
                font_size = max(orig_size / (len_ratio ** 0.4), orig_size * 0.65)

            kwargs = {"fontsize": font_size, "color": color, "align": fitz.TEXT_ALIGN_LEFT}
            if font_path:
                kwargs["fontname"] = "serif"
                kwargs["fontfile"] = font_path
            else:
                kwargs["fontname"] = "helv"
                kwargs["encoding"] = fitz.TEXT_ENCODING_LATIN

            rc = page.insert_textbox(rect, translated, **kwargs)

            # If overflow, try smaller font
            if rc < 0:
                smaller = font_size * 0.8
                kwargs["fontsize"] = smaller
                # Re-redact area (already white) and re-insert
                page.insert_textbox(rect, translated, **kwargs)

        if progress_callback and page_idx % 20 == 0:
            progress_callback(88 + int((page_idx + 1) / total_pages * 10))

    # Phase 4: Build TOC
    chapter_entries = []
    for page_idx, blocks in enumerate(page_blocks):
        for bl in blocks:
            t = bl.get("translated", bl["text"])
            if CHAPTER_RE.match(t) or CHAPTER_RE.match(bl["text"]):
                chapter_entries.append((t, page_idx))
                break

    if chapter_entries:
        toc_list = [[1, title[:60], pg + 1] for title, pg in chapter_entries]
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
            fitz.Rect(50, 50, 550, 780), text, fontsize=11, align=0,
        )
    return doc.tobytes()
