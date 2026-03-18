"""
Format conversion using Calibre CLI (ebook-convert).
Falls back to PyMuPDF / ebooklib if Calibre is not installed.
"""

import asyncio
import io
import logging
import shutil
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def calibre_available() -> bool:
    return shutil.which("ebook-convert") is not None


async def convert_with_calibre(
    input_bytes: bytes,
    input_ext: str,
    output_ext: str,
    extra_args: list[str] | None = None,
) -> bytes | None:
    """Convert file bytes using Calibre ebook-convert. Returns output bytes or None."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input{input_ext}"
        dst = Path(tmp) / f"output{output_ext}"
        src.write_bytes(input_bytes)

        cmd = ["ebook-convert", str(src), str(dst)] + (extra_args or [])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                logger.warning("Calibre conversion failed (exit %d): %s", proc.returncode, stderr.decode(errors="replace")[:500])
                return None
            return dst.read_bytes() if dst.exists() else None
        except asyncio.TimeoutError:
            logger.warning("Calibre conversion timed out after 300s")
            return None
        except FileNotFoundError:
            logger.warning("ebook-convert not found in PATH")
            return None
        except Exception:
            logger.warning("Calibre conversion error", exc_info=True)
            return None


async def epub_to_pdf(epub_bytes: bytes) -> bytes:
    """Convert EPUB → PDF. Uses Calibre if available, else PyMuPDF fallback."""
    if calibre_available():
        result = await convert_with_calibre(epub_bytes, ".epub", ".pdf")
        if result:
            return result

    # Fallback: extract text and build simple PDF
    import zipfile
    from bs4 import BeautifulSoup

    doc = fitz.open()
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zf:
            html_files = sorted(
                n for n in zf.namelist()
                if n.endswith((".html", ".xhtml", ".htm"))
                and "toc" not in n.lower()
            )
            for name in html_files:
                soup = BeautifulSoup(zf.read(name), "lxml")
                text = soup.get_text(separator="\n").strip()
                if not text:
                    continue
                page = doc.new_page(width=595, height=842)  # A4
                page.insert_textbox(
                    fitz.Rect(50, 50, 545, 792),
                    text,
                    fontsize=11,
                    align=0,
                )
    except Exception:
        logger.warning("EPUB to PDF fallback conversion failed", exc_info=True)

    return doc.tobytes()


async def pdf_to_epub(pdf_bytes: bytes, title: str = "Book") -> bytes:
    """Convert PDF → EPUB. Uses Calibre if available, else text-extraction fallback."""
    if calibre_available():
        extra = ["--title", title]
        result = await convert_with_calibre(pdf_bytes, ".pdf", ".epub", extra)
        if result:
            return result

    # Fallback: extract text, build simple EPUB
    from ebooklib import epub as epub_lib

    book = epub_lib.EpubBook()
    book.set_title(title)
    book.set_language("en")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    chapters = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if not text:
            continue
        chapter = epub_lib.EpubHtml(
            title=f"Page {i + 1}",
            file_name=f"page_{i + 1}.xhtml",
            lang="en",
        )
        paragraphs = "".join(f"<p>{line}</p>" for line in text.split("\n") if line.strip())
        chapter.content = f"<html><body>{paragraphs}</body></html>"
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters)
    book.add_item(epub_lib.EpubNcx())
    book.add_item(epub_lib.EpubNav())
    book.spine = ["nav"] + chapters

    out = io.BytesIO()
    epub_lib.write_epub(out, book)
    out.seek(0)
    return out.read()
