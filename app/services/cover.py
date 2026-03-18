"""Extract cover image from EPUB or PDF."""

import io
import logging
import zipfile

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def extract_epub_cover(epub_bytes: bytes) -> bytes | None:
    """Return cover image bytes (JPEG/PNG) from EPUB, or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zf:
            names = zf.namelist()

            # 1. Try OPF manifest for cover-image item
            opf_file = next((n for n in names if n.endswith(".opf")), None)
            if opf_file:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(zf.read(opf_file), "lxml-xml")
                base_dir = opf_file.rsplit("/", 1)[0] if "/" in opf_file else ""

                for attr in ({"properties": "cover-image"}, {"id": "cover-image"}, {"id": "cover"}):
                    item = soup.find("item", attr)
                    if item and item.get("href"):
                        candidate = f"{base_dir}/{item['href']}" if base_dir else item["href"]
                        # normalize path
                        candidate = candidate.replace("//", "/")
                        if candidate in names:
                            return zf.read(candidate)

                # Also check meta name="cover"
                meta = soup.find("meta", {"name": "cover"})
                if meta and meta.get("content"):
                    cover_id = meta["content"]
                    item = soup.find("item", {"id": cover_id})
                    if item and item.get("href"):
                        candidate = f"{base_dir}/{item['href']}" if base_dir else item["href"]
                        candidate = candidate.replace("//", "/")
                        if candidate in names:
                            return zf.read(candidate)

            # 2. Fallback: look for common cover filenames
            IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
            for name in names:
                low = name.lower()
                if "cover" in low and any(low.endswith(ext) for ext in IMAGE_EXTS):
                    return zf.read(name)

    except Exception:
        logger.warning("Failed to extract EPUB cover", exc_info=True)
    return None


def extract_pdf_cover(pdf_bytes: bytes, scale: float = 0.4) -> bytes | None:
    """Render first page of PDF as JPEG. scale=0.4 gives ~300x400px for A4."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) == 0:
            return None
        page = doc[0]
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("jpeg")
    except Exception:
        logger.warning("Failed to extract PDF cover", exc_info=True)
        return None
