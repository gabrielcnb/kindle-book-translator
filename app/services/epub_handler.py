import asyncio
import io
import re
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

# Separator that Google Translate preserves (treats as unknown acronym)
BATCH_SEP = "KBTXSEP"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))
BATCH_SIZE = 30  # blocks per API call


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


async def _batch_translate(
    texts: list[str],
    source_lang: str,
    target_lang: str,
) -> list[str]:
    """Translate a list of texts in batches, preserving order.
    Falls back to individual translation if separator is mangled."""
    results = list(texts)  # copy, fill in-place

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]

        # Only translate non-empty entries
        nonempty = [(i, t) for i, t in enumerate(batch) if t.strip()]
        if not nonempty:
            continue

        indices, nonempty_texts = zip(*nonempty)

        joined = f" {BATCH_SEP} ".join(nonempty_texts)
        translated_joined = await translate_text(joined, source_lang, target_lang)

        # Split back — handle Google adding/removing spaces around separator
        parts = [p.strip() for p in BATCH_SEP_RE.split(translated_joined)]

        if len(parts) == len(nonempty_texts):
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = parts[local_i]
        else:
            # Fallback: translate individually
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = await translate_text(
                    nonempty_texts[local_i], source_lang, target_lang
                )

    return results


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
        css = epub.EpubItem(
            uid="bilingual_css",
            file_name="Styles/bilingual.css",
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

    completed = 0

    async def _process_item(item):
        nonlocal completed
        try:
            content = item.get_content()
            soup = BeautifulSoup(content, "lxml")

            if bilingual:
                soup.head.append(soup.new_tag(
                    "link", rel="stylesheet",
                    href="../Styles/bilingual.css", type="text/css"
                ))

            blocks = _collect_blocks(soup)
            if blocks:
                texts = [
                    " ".join(str(n).strip() for n in text_nodes if str(n).strip())
                    for _, text_nodes in blocks
                ]
                translations = await _batch_translate(texts, source_lang, target_lang)

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
        except Exception as e:
            print(f"[epub_handler] item failed: {e}")

        # asyncio is single-threaded; no lock needed for simple increment
        completed += 1
        if progress_callback:
            progress_callback(int(completed / total * 90))

    await asyncio.gather(*[_process_item(item) for item in items], return_exceptions=True)

    out = io.BytesIO()
    epub.write_epub(out, book)
    out.seek(0)

    if progress_callback:
        progress_callback(100)

    return out.read()
