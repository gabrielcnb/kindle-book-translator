import io
from typing import Callable
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag
from app.translator import batch_translate

SKIP_TAGS = frozenset({"script", "style", "code", "pre", "head", "title"})
BLOCK_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "dt", "dd", "blockquote", "figcaption",
})

BILINGUAL_CSS = """
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
"""


def _should_skip(node) -> bool:
    return any(getattr(p, "name", None) in SKIP_TAGS for p in node.parents)


def _find_block_parent(node) -> Tag | None:
    for parent in node.parents:
        if getattr(parent, "name", None) in BLOCK_TAGS:
            return parent
    return None


def _collect_blocks(soup) -> list[tuple[Tag, list[NavigableString]]]:
    blocks: list[tuple[Tag, list[NavigableString]]] = []
    block_map: dict[int, list[NavigableString]] = {}

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
    engine: str = "google",
    glossary: list[str] | None = None,
) -> bytes:
    book = epub.read_epub(io.BytesIO(file_bytes))
    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    total = max(len(items), 1)

    # Parse all documents once — reuse soup for both counting and translating
    parsed: list[tuple[BeautifulSoup, list[tuple[Tag, list[NavigableString]]]]] = []
    total_blocks = 0
    for item in items:
        soup = BeautifulSoup(item.get_content(), "lxml")
        blocks = _collect_blocks(soup)
        parsed.append((soup, blocks))
        total_blocks += len(blocks)
    blocks_done = 0

    for doc_idx, item in enumerate(items):
        soup, blocks = parsed[doc_idx]

        if bilingual:
            head = soup.find("head")
            if head:
                style_tag = soup.new_tag("style", type="text/css")
                style_tag.string = BILINGUAL_CSS
                head.append(style_tag)

        if not blocks:
            if progress_callback:
                progress_callback(int(blocks_done / max(total_blocks, 1) * 90))
            continue

        # Extract text for all blocks at once
        texts = [
            " ".join(str(n).strip() for n in text_nodes if str(n).strip())
            for _, text_nodes in blocks
        ]

        # Granular progress: update as each batch within the chapter completes
        def _on_batch_done(count, _base=blocks_done):
            if progress_callback:
                progress_callback(int((_base + count) / max(total_blocks, 1) * 90))

        # Batch-translate all blocks for this document
        translations = await batch_translate(
            texts, source_lang, target_lang, on_batch_done=_on_batch_done,
            engine=engine, glossary=glossary,
        )
        blocks_done += len(blocks)

        # Apply translations back to DOM
        for (block, _), translated in zip(blocks, translations):
            if not translated:
                continue
            if bilingual:
                orig_text = block.get_text(separator=" ").strip()
                block.clear()
                orig_span = soup.new_tag("span")
                orig_span["class"] = "original-text"
                orig_span.string = orig_text

                trans_span = soup.new_tag("span")
                trans_span["class"] = "translated-text"
                trans_span.string = translated

                block.append(orig_span)
                block.append(trans_span)
            else:
                block.clear()
                block.string = translated

        item.set_content(str(soup).encode("utf-8"))

    out = io.BytesIO()
    epub.write_epub(out, book)
    out.seek(0)

    if progress_callback:
        progress_callback(100)

    return out.read()
