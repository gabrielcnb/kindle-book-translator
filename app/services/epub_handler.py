import io
from typing import Callable
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag
from app.translator import translate_text

SKIP_TAGS = frozenset({"script", "style", "code", "pre", "head", "title"})
BLOCK_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "dt", "dd", "blockquote", "figcaption",
})


def _should_skip(node) -> bool:
    return any(getattr(p, "name", None) in SKIP_TAGS for p in node.parents)


def _find_block_parent(node) -> Tag | None:
    for parent in node.parents:
        if getattr(parent, "name", None) in BLOCK_TAGS:
            return parent
    return None


def _collect_blocks(soup) -> list[tuple[Tag, list[NavigableString]]]:
    blocks: list[tuple] = []
    block_map: dict[int, list] = {}

    for string in soup.find_all(string=True):
        if not isinstance(string, NavigableString):
            continue
        if not str(string).strip():
            continue
        if _should_skip(string):
            continue
        block = _find_block_parent(string)
        if block is None:
            continue
        bid = id(block)
        if bid not in block_map:
            block_map[bid] = []
            blocks.append((block, block_map[bid]))
        block_map[bid].append(string)

    return blocks


async def translate_epub(
    file_bytes: bytes,
    source_lang: str,
    target_lang: str,
    progress_callback: Callable[[int], None] | None = None,
    bilingual: bool = False,
) -> bytes:
    book = epub.read_epub(io.BytesIO(file_bytes))
    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    total = max(len(items), 1)

    if bilingual:
        # Inject CSS for bilingual style
        css = epub.EpubItem(
            uid="bilingual_css",
            file_name="bilingual.css",
            media_type="text/css",
            content=b"""
.original-text {
    font-size: 0.82em;
    color: #888;
    font-style: italic;
    display: block;
    margin-bottom: 2px;
    line-height: 1.4;
}
.translated-text {
    display: block;
    line-height: 1.6;
}
""",
        )
        book.add_item(css)

    for doc_idx, item in enumerate(items):
        content = item.get_content()
        soup = BeautifulSoup(content, "lxml")

        if bilingual:
            soup.head.append(soup.new_tag(
                "link", rel="stylesheet",
                href="../bilingual.css", type="text/css"
            ))

        blocks = _collect_blocks(soup)
        n_blocks = max(len(blocks), 1)

        for blk_idx, (block, text_nodes) in enumerate(blocks):
            full_text = " ".join(str(n).strip() for n in text_nodes if str(n).strip())
            if not full_text:
                continue

            translated = await translate_text(full_text, source_lang, target_lang)

            if bilingual:
                block.clear()
                orig_span = soup.new_tag("span")
                orig_span["class"] = "original-text"
                orig_span.string = full_text

                trans_span = soup.new_tag("span")
                trans_span["class"] = "translated-text"
                trans_span.string = translated

                block.append(orig_span)
                block.append(trans_span)
            else:
                block.clear()
                block.string = translated

            if progress_callback:
                overall = (doc_idx / total + (blk_idx / n_blocks) / total) * 90
                progress_callback(int(overall))

        item.set_content(str(soup).encode("utf-8"))

        if progress_callback:
            progress_callback(int((doc_idx + 1) / total * 90))

    out = io.BytesIO()
    epub.write_epub(out, book)
    out.seek(0)

    if progress_callback:
        progress_callback(100)

    return out.read()
