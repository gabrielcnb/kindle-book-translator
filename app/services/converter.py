"""
Format conversion using Calibre CLI (ebook-convert).
Falls back to PyMuPDF / ebooklib if Calibre is not installed.
"""

import asyncio
import io
import os
import shutil
import tempfile
from pathlib import Path

import fitz  # PyMuPDF


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
                return None
            return dst.read_bytes() if dst.exists() else None
        except (asyncio.TimeoutError, FileNotFoundError, Exception):
            return None


_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
]


def _find_font() -> str | None:
    for p in _FONT_PATHS:
        if Path(p).exists():
            return p
    return None


def _insert_text_paginated(doc, text: str, fontsize: float = 11, fontfile: str | None = None):
    """Insert text into PDF with automatic page creation when text overflows.

    insert_textbox renders nothing if text doesn't fit, so we find the
    max chunk that fits using binary search, then continue with the rest.
    """
    rect = fitz.Rect(50, 50, 545, 792)  # A4 margins
    remaining = text.strip()

    def _make_kwargs():
        kw = {"fontsize": fontsize, "align": 0}
        if fontfile:
            kw["fontname"] = "custom"
            kw["fontfile"] = fontfile
        return kw

    while remaining:
        page = doc.new_page(width=595, height=842)
        rc = page.insert_textbox(rect, remaining, **_make_kwargs())
        if rc >= 0:
            break  # all text fit

        # Text didn't fit — binary search for max chars that fit
        doc.delete_page(-1)  # remove the failed page
        lo, hi = 0, len(remaining)
        best = 0

        while lo <= hi:
            mid = (lo + hi) // 2
            # Try to break at a newline or space near mid
            cut = remaining.rfind("\n", 0, mid)
            if cut <= lo:
                cut = remaining.rfind(" ", 0, mid)
            if cut <= lo:
                cut = mid
            if cut <= 0:
                break

            test_page = doc.new_page(width=595, height=842)
            test_rc = test_page.insert_textbox(rect, remaining[:cut], **_make_kwargs())
            doc.delete_page(-1)

            if test_rc >= 0:
                best = cut
                lo = mid + 1
            else:
                hi = mid - 1

        if best <= 0:
            # Can't fit anything — force a chunk to avoid infinite loop
            best = min(2000, len(remaining))

        page = doc.new_page(width=595, height=842)
        page.insert_textbox(rect, remaining[:best], **_make_kwargs())
        remaining = remaining[best:].strip()


async def epub_to_pdf(epub_bytes: bytes) -> bytes:
    """Convert EPUB → PDF. Uses Calibre if available, else PyMuPDF fallback."""
    if calibre_available():
        result = await convert_with_calibre(epub_bytes, ".epub", ".pdf")
        if result:
            return result

    # Fallback: extract text and build PDF with proper pagination
    import zipfile
    from bs4 import BeautifulSoup

    fontfile = _find_font()
    doc = fitz.open()

    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zf:
        # Read OPF spine order if available
        opf_file = next((n for n in zf.namelist() if n.endswith(".opf")), None)
        ordered_files = []

        if opf_file:
            opf_soup = BeautifulSoup(zf.read(opf_file), "lxml-xml")
            base_dir = opf_file.rsplit("/", 1)[0] if "/" in opf_file else ""
            manifest = {}
            for item in opf_soup.find_all("item"):
                if item.get("id") and item.get("href"):
                    href = item["href"]
                    full = f"{base_dir}/{href}" if base_dir else href
                    manifest[item["id"]] = full
            for itemref in opf_soup.find_all("itemref"):
                idref = itemref.get("idref", "")
                if idref in manifest:
                    ordered_files.append(manifest[idref])

        if not ordered_files:
            ordered_files = sorted(
                n for n in zf.namelist()
                if n.endswith((".html", ".xhtml", ".htm"))
            )

        for name in ordered_files:
            try:
                raw = zf.read(name)
            except KeyError:
                continue
            soup = BeautifulSoup(raw, "lxml")
            text = soup.get_text(separator="\n").strip()
            if not text:
                continue
            _insert_text_paginated(doc, text, fontsize=11, fontfile=fontfile)

    if len(doc) == 0:
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(fitz.Rect(50, 50, 545, 792), "(No text content found)", fontsize=14)

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

    tmp_out = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp_out.close()
    try:
        epub_lib.write_epub(tmp_out.name, book)
        with open(tmp_out.name, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_out.name)
