import asyncio
import io
import zipfile
import fitz  # PyMuPDF
from typing import Callable
from app.translator import translate_text


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
        img_list = page.get_images(full=True)
        for img_info in img_list:
            xref = img_info[0]
            img_rect = page.get_image_bbox(img_info)
            try:
                base_image = src_doc.extract_image(xref)
                img_bytes = base_image["image"]
                img_ext = base_image["ext"]
                pil_img = fitz.open(stream=img_bytes, filetype=img_ext)
                new_page.insert_image(img_rect, stream=img_bytes)
            except Exception:
                pass

        for block in blocks:
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue

                    translated = await translate_text(text, source_lang, target_lang)

                    bbox = fitz.Rect(span["bbox"])
                    fontsize = span.get("size", 11)
                    color_int = span.get("color", 0)
                    r = ((color_int >> 16) & 0xFF) / 255
                    g = ((color_int >> 8) & 0xFF) / 255
                    b = (color_int & 0xFF) / 255

                    try:
                        new_page.insert_textbox(
                            bbox,
                            translated,
                            fontsize=fontsize,
                            color=(r, g, b),
                            align=0,
                        )
                    except Exception:
                        try:
                            new_page.insert_text(
                                (bbox.x0, bbox.y1),
                                translated,
                                fontsize=fontsize,
                                color=(r, g, b),
                            )
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
    import zipfile

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
