import io
import asyncio
from typing import Callable
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from app.translator import translate_text


async def translate_epub(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
) -> bytes:
    book = epub.read_epub(io.BytesIO(file_bytes))

    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    total = len(items)

    for idx, item in enumerate(items):
        content = item.get_content()
        soup = BeautifulSoup(content, "lxml")

        text_nodes = []
        for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "span", "div"]):
            if tag.string and tag.string.strip():
                text_nodes.append((tag, tag.string.strip()))

        for tag, original_text in text_nodes:
            translated = await translate_text(original_text, source_lang, target_lang)
            tag.string = translated

        item.set_content(str(soup).encode("utf-8"))

        if progress_callback:
            progress_callback(int((idx + 1) / total * 90))

    out = io.BytesIO()
    epub.write_epub(out, book)
    out.seek(0)

    if progress_callback:
        progress_callback(100)

    return out.read()
