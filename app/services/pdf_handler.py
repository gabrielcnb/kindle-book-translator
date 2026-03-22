import asyncio
import io
import os
import re
import shutil
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text

try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = shutil.which("tesseract") is not None
except ImportError:
    HAS_TESSERACT = False

CHUNK_TARGET = 4000
BATCH_SEP = "KBTXSEP"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))

CHAPTER_RE = re.compile(
    r'^(CHAPTER|CAPITULO|CAP[ÍI]TULO|PART|PARTE)\s+[\dIVXLCDM]+',
    re.IGNORECASE,
)

# ── Font resolution (regular, italic, bold, bold-italic) ──────────────────

_FONT_VARIANTS = {
    "regular": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/times.ttf",
    ],
    "italic": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        "C:/Windows/Fonts/georgiai.ttf",
        "C:/Windows/Fonts/timesi.ttf",
    ],
    "bold": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "C:/Windows/Fonts/georgiab.ttf",
        "C:/Windows/Fonts/timesbd.ttf",
    ],
    "bolditalic": [
        "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf",
        "C:/Windows/Fonts/georgiaz.ttf",
        "C:/Windows/Fonts/timesbi.ttf",
    ],
}

_font_cache: dict[str, str | None] = {}


def _find_font(variant: str = "regular") -> str | None:
    if variant not in _font_cache:
        _font_cache[variant] = None
        for p in _FONT_VARIANTS.get(variant, []):
            if os.path.exists(p):
                _font_cache[variant] = p
                break
    return _font_cache[variant]


def _get_font_variant(flags: int) -> str:
    """Map PDF span flags to font variant name."""
    is_italic = bool(flags & 2)
    is_bold = bool(flags & (1 << 4))
    if is_bold and is_italic:
        return "bolditalic"
    elif is_bold:
        return "bold"
    elif is_italic:
        return "italic"
    return "regular"


def _detect_alignment(block_bbox, page_width, text: str) -> int:
    """Detect block alignment. Only short, centered text gets CENTER; body text gets JUSTIFY."""
    x0, _, x1, _ = block_bbox
    block_width = x1 - x0
    text_len = len(text)

    # Short text that's centered on page → CENTER (titles, epigraphs, short lines)
    if text_len < 80 and block_width < page_width * 0.6:
        left_margin = x0
        right_margin = page_width - x1
        if abs(left_margin - right_margin) < 40:
            return fitz.TEXT_ALIGN_CENTER

    # Wide body text → JUSTIFY (like original book layout)
    if block_width > page_width * 0.5:
        return fitz.TEXT_ALIGN_JUSTIFY

    return fitz.TEXT_ALIGN_LEFT


# ── OCR for image-based PDFs ───────────────────────────────────────────────

def _is_image_pdf(doc: fitz.Document, sample_pages: int = 5) -> bool:
    """Check if a PDF is image-based (scanned) by sampling pages for text."""
    if len(doc) == 0:
        return False
    total_chars = 0
    pages_to_check = min(len(doc), sample_pages)
    for i in range(pages_to_check):
        total_chars += len(doc[i].get_text().strip())
    avg_chars = total_chars / pages_to_check
    return avg_chars < 50


def _ocr_page(page: fitz.Page, dpi: int = 300) -> list[dict]:
    """OCR a single page and return text blocks with bounding boxes."""
    if not HAS_TESSERACT:
        return []

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Get word-level bounding boxes from Tesseract
    ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    # Group words into lines, then lines into blocks
    scale = 72 / dpi  # convert pixel coords back to PDF points
    blocks_by_block_num: dict[int, list] = {}

    for i in range(len(ocr_data["text"])):
        word = ocr_data["text"][i].strip()
        try:
            conf = int(float(ocr_data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if not word or conf < 30:
            continue

        block_num = ocr_data["block_num"][i]
        x = ocr_data["left"][i] * scale
        y = ocr_data["top"][i] * scale
        w = ocr_data["width"][i] * scale
        h = ocr_data["height"][i] * scale

        if block_num not in blocks_by_block_num:
            blocks_by_block_num[block_num] = {
                "words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h
            }
        blk = blocks_by_block_num[block_num]
        blk["words"].append(word)
        blk["x0"] = min(blk["x0"], x)
        blk["y0"] = min(blk["y0"], y)
        blk["x1"] = max(blk["x1"], x + w)
        blk["y1"] = max(blk["y1"], y + h)

    result = []
    page_width = page.rect.width
    for blk in blocks_by_block_num.values():
        text = " ".join(blk["words"])
        if not text.strip():
            continue
        bbox = (blk["x0"], blk["y0"], blk["x1"], blk["y1"])
        result.append({
            "bbox": bbox,
            "text": text,
            "size": 11,
            "color": 0,
            "flags": 0,
            "align": _detect_alignment(bbox, page_width, text),
        })

    return result


# ── Main: overlay translation ──────────────────────────────────────────────

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
    page_width = src_doc[0].rect.width if total_pages > 0 else 595

    is_image = _is_image_pdf(src_doc)

    # Phase 1: Extract text blocks from all pages
    page_blocks: list[list[dict]] = []
    all_texts: list[str] = []
    text_index_map: list[tuple[int, int]] = []

    for page_idx in range(total_pages):
        page = src_doc[page_idx]

        if is_image:
            blocks_data = _ocr_page(page)
        else:
            blocks_data = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue

                lines_text = []
                dominant_size = 0
                dominant_color = 0
                span_count = 0
                italic_count = 0
                bold_count = 0

                for line in block.get("lines", []):
                    line_parts = []
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if text.strip():
                            line_parts.append(text)
                            dominant_size += span.get("size", 11)
                            dominant_color += span.get("color", 0)
                            flags = span.get("flags", 0)
                            if flags & 2:
                                italic_count += 1
                            if flags & (1 << 4):
                                bold_count += 1
                            span_count += 1
                    if line_parts:
                        lines_text.append(" ".join(line_parts))

                full_text = " ".join(lines_text).strip()
                if not full_text:
                    continue

                avg_size = dominant_size / span_count if span_count else 11
                avg_color = int(dominant_color / span_count) if span_count else 0
                majority_flags = 0
                if italic_count > span_count / 2:
                    majority_flags |= 2
                if bold_count > span_count / 2:
                    majority_flags |= (1 << 4)

                blocks_data.append({
                    "bbox": block["bbox"],
                    "text": full_text,
                    "size": avg_size,
                    "color": avg_color,
                    "flags": majority_flags,
                    "align": _detect_alignment(block["bbox"], page_width, full_text),
                })

        for bi, bd in enumerate(blocks_data):
            text_index_map.append((page_idx, bi))
            all_texts.append(bd["text"])
        page_blocks.append(blocks_data)

        if progress_callback and page_idx % 10 == 0:
            progress_callback(2 + int(page_idx / total_pages * 3))

    src_doc.close()

    if progress_callback:
        progress_callback(5)

    # Phase 2: Batch translate
    total_texts = len(all_texts)
    chunks = []
    chunk_indices = []
    current_chunk = ""
    current_indices = []

    for idx, text in enumerate(all_texts):
        if len(text) < 20:
            continue  # short texts translated individually, skip batching
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

    translated_all = list(all_texts)
    completed = 0
    total_chunks = max(len(chunks), 1)

    # Translate short texts individually (they get lost in batch separators)
    short_tasks = []
    for idx, text in enumerate(all_texts):
        if len(text) < 20:
            async def _translate_short(i=idx, t=text):
                nonlocal completed
                try:
                    translated_all[i] = await translate_text(t, source_lang, target_lang)
                except Exception:
                    pass
            short_tasks.append(_translate_short())

    async def _translate_chunk(chunk, indices):
        nonlocal completed
        result = await translate_text(chunk, source_lang, target_lang)
        parts = [p.strip() for p in BATCH_SEP_RE.split(result)]
        if len(parts) == len(indices):
            for i, idx in enumerate(indices):
                if len(all_texts[idx]) >= 20:  # skip shorts, already handled
                    translated_all[idx] = parts[i]
        else:
            for i, idx in enumerate(indices):
                if len(all_texts[idx]) >= 20:
                    try:
                        translated_all[idx] = await translate_text(all_texts[idx], source_lang, target_lang)
                    except Exception:
                        pass
        completed += 1
        if progress_callback:
            progress_callback(5 + int(completed / total_chunks * 80))

    await asyncio.gather(
        *short_tasks,
        *[_translate_chunk(chunk, indices)
          for chunk, indices in zip(chunks, chunk_indices)]
    )

    # Map back
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

        if is_image:
            # For image PDFs: draw white rectangles over OCR'd text areas
            for bl in blocks:
                rect = fitz.Rect(bl["bbox"]) + (-2, -2, 2, 2)
                page.draw_rect(rect, color=None, fill=(1, 1, 1))
        else:
            # For text PDFs: use redact to cleanly remove text
            for bl in blocks:
                rect = fitz.Rect(bl["bbox"]) + (-2, -2, 2, 2)
                page.add_redact_annot(rect, fill=(1, 1, 1))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Insert translated text
        for bl in blocks:
            translated = bl.get("translated", bl["text"])
            if not translated:
                continue

            rect = fitz.Rect(bl["bbox"])
            orig_size = bl["size"]
            color_int = bl["color"]
            flags = bl["flags"]
            align = bl["align"]

            # Parse color
            if isinstance(color_int, int) and color_int != 0:
                r = ((color_int >> 16) & 0xFF) / 255
                g = ((color_int >> 8) & 0xFF) / 255
                b = (color_int & 0xFF) / 255
                color = (r, g, b)
            else:
                color = (0, 0, 0)

            # Scale font gently if translated text is longer
            # Never scale short texts — they always fit
            len_ratio = len(translated) / max(len(bl["text"]), 1)
            font_size = orig_size
            if len_ratio > 1.3 and len(bl["text"]) > 40:
                font_size = max(orig_size / (len_ratio ** 0.25), orig_size * 0.85)

            # Select font variant (regular, italic, bold, bolditalic)
            variant = _get_font_variant(flags)
            font_path = _find_font(variant)
            if not font_path:
                font_path = _find_font("regular")

            kwargs = {
                "fontsize": font_size,
                "color": color,
                "align": align,
            }
            if font_path:
                kwargs["fontname"] = f"s_{variant}"
                kwargs["fontfile"] = font_path
            else:
                # helv doesn't support Unicode — try DejaVuSans as universal fallback
                _unicode_fallbacks = [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "C:/Windows/Fonts/arial.ttf",
                    "C:/Windows/Fonts/segoeui.ttf",
                ]
                fallback = next((f for f in _unicode_fallbacks if os.path.exists(f)), None)
                if fallback:
                    kwargs["fontname"] = "s_fallback"
                    kwargs["fontfile"] = fallback
                else:
                    kwargs["fontname"] = "helv"
                    kwargs["encoding"] = fitz.TEXT_ENCODING_LATIN

            # Short text: use insert_text (no clipping) instead of textbox
            if len(translated) < 30:
                try:
                    page.insert_text(
                        (rect.x0, rect.y0 + font_size), translated,
                        fontsize=font_size, color=color,
                        fontname=kwargs.get("fontname", "helv"),
                        fontfile=kwargs.get("fontfile"),
                    )
                except Exception:
                    page.insert_textbox(rect, translated, **kwargs)
            else:
                # Expand rect width if needed, but never past page margins
                page_right = out_doc[page_idx].rect.width - 50
                min_width = len(translated) * font_size * 0.45
                if rect.width < min_width:
                    new_x1 = min(rect.x0 + min_width, page_right)
                    rect = fitz.Rect(rect.x0, rect.y0, new_x1, rect.y1)

                rc = page.insert_textbox(rect, translated, **kwargs)

                # If overflow, retry with progressively smaller font
                if rc < 0:
                    for shrink in [0.88, 0.78, 0.68]:
                        kwargs["fontsize"] = orig_size * shrink
                        rc = page.insert_textbox(rect, translated, **kwargs)
                        if rc >= 0:
                            break

        if progress_callback and page_idx % 20 == 0:
            progress_callback(88 + int((page_idx + 1) / total_pages * 10))

    # Phase 4: Build TOC
    # Detect chapters: look for "CHAPTER"/"CAPÍTULO" block followed by a number block
    CHAPTER_WORD_RE = re.compile(
        r'^(CHAPTER|CAPITULO|CAP[ÍI]TULO|PART|PARTE)\s*$', re.IGNORECASE
    )
    chapter_entries = []
    for page_idx, blocks in enumerate(page_blocks):
        for i, bl in enumerate(blocks):
            t = bl.get("translated", bl["text"])
            orig_t = bl["text"]
            # Format 1: "CHAPTER X – TITLE" in one block
            if CHAPTER_RE.match(t) or CHAPTER_RE.match(orig_t):
                chapter_entries.append((t[:60], page_idx))
                break
            # Format 2: "CHAPTER" alone + next block is number
            if CHAPTER_WORD_RE.match(t.strip()) or CHAPTER_WORD_RE.match(orig_t.strip()):
                num = ""
                if i + 1 < len(blocks):
                    num = blocks[i + 1].get("translated", blocks[i + 1]["text"]).strip()
                    if not num.isdigit():
                        num = blocks[i + 1]["text"].strip()
                if num.isdigit():
                    chapter_entries.append((f"{t.strip()} {num}", page_idx))
                else:
                    chapter_entries.append((t.strip(), page_idx))
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
