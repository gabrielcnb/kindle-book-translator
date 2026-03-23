import asyncio
import logging
import re
from typing import Callable

from deep_translator import GoogleTranslator, MyMemoryTranslator
from app import cache

logger = logging.getLogger(__name__)

LANGUAGES = {
    "af": "Afrikaans", "sq": "Albanian", "am": "Amharic", "ar": "Arabic",
    "hy": "Armenian", "az": "Azerbaijani", "eu": "Basque", "be": "Belarusian",
    "bn": "Bengali", "bs": "Bosnian", "bg": "Bulgarian", "ca": "Catalan",
    "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
    "co": "Corsican", "hr": "Croatian", "cs": "Czech", "da": "Danish",
    "nl": "Dutch", "en": "English", "eo": "Esperanto", "et": "Estonian",
    "fi": "Finnish", "fr": "French", "fy": "Frisian", "gl": "Galician",
    "ka": "Georgian", "de": "German", "el": "Greek", "gu": "Gujarati",
    "ht": "Haitian Creole", "ha": "Hausa", "he": "Hebrew", "hi": "Hindi",
    "hu": "Hungarian", "is": "Icelandic", "ig": "Igbo", "id": "Indonesian",
    "ga": "Irish", "it": "Italian", "ja": "Japanese", "jv": "Javanese",
    "kn": "Kannada", "kk": "Kazakh", "km": "Khmer", "ko": "Korean",
    "ku": "Kurdish", "ky": "Kyrgyz", "lo": "Lao", "la": "Latin",
    "lv": "Latvian", "lt": "Lithuanian", "lb": "Luxembourgish",
    "mk": "Macedonian", "mg": "Malagasy", "ms": "Malay", "ml": "Malayalam",
    "mt": "Maltese", "mi": "Maori", "mr": "Marathi", "mn": "Mongolian",
    "my": "Myanmar (Burmese)", "ne": "Nepali", "no": "Norwegian",
    "ny": "Nyanja", "or": "Odia", "ps": "Pashto", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "pa": "Punjabi", "ro": "Romanian",
    "ru": "Russian", "sm": "Samoan", "gd": "Scots Gaelic", "sr": "Serbian",
    "st": "Sesotho", "sn": "Shona", "sd": "Sindhi", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "so": "Somali", "es": "Spanish",
    "su": "Sundanese", "sw": "Swahili", "sv": "Swedish", "tl": "Filipino",
    "tg": "Tajik", "ta": "Tamil", "tt": "Tatar", "te": "Telugu",
    "th": "Thai", "tr": "Turkish", "tk": "Turkmen", "uk": "Ukrainian",
    "ur": "Urdu", "ug": "Uyghur", "uz": "Uzbek", "vi": "Vietnamese",
    "cy": "Welsh", "xh": "Xhosa", "yi": "Yiddish", "yo": "Yoruba",
    "zu": "Zulu",
}

ENGINES = {
    "google": {"name": "Google Translate", "class": GoogleTranslator},
    "mymemory": {"name": "MyMemory", "class": MyMemoryTranslator},
}

MAX_CHUNK = 4500
DELAY_BETWEEN_REQUESTS = 0.05
TRANSLATE_TIMEOUT = 30
MAX_RETRIES = 2
MAX_CONCURRENT = 5  # parallel translation requests

# Batch translation constants
BATCH_SEP = "\n|||KBTXSEP|||\n"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))
BATCH_SIZE = 12


# ──────────────────────────────────────────────────────────────────────────────
# Glossary: protect terms from translation using placeholders
# ──────────────────────────────────────────────────────────────────────────────

def _protect_glossary(text: str, glossary: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Replace glossary terms with numbered placeholders."""
    replacements = []
    for i, term in enumerate(glossary):
        placeholder = f"⟦KBT{i:03d}⟧"
        if term in text:
            text = text.replace(term, placeholder)
            replacements.append((placeholder, term))
    return text, replacements


def _restore_glossary(text: str, replacements: list[tuple[str, str]]) -> str:
    """Restore glossary terms from placeholders."""
    for placeholder, term in replacements:
        text = text.replace(placeholder, term)
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Translation engine
# ──────────────────────────────────────────────────────────────────────────────

def _create_translator(engine: str, source_lang: str, target_lang: str):
    """Create a translator instance for the given engine."""
    cls = ENGINES[engine]["class"]
    return cls(source=source_lang, target=target_lang)


def split_text(text: str, max_size: int = MAX_CHUNK) -> list[str]:
    if len(text) <= max_size:
        return [text]

    chunks: list[str] = []
    # Split by paragraphs first to preserve structure
    paragraphs = text.split("\n")
    current = ""

    for para in paragraphs:
        # If a single paragraph exceeds max_size, split by sentences
        if len(para) > max_size:
            if current:
                chunks.append(current)
                current = ""
            sentences = para.split(". ")
            for sentence in sentences:
                if len(current) + len(sentence) + 2 > max_size:
                    if current:
                        chunks.append(current)
                    current = sentence
                else:
                    current += (". " if current else "") + sentence
        elif len(current) + len(para) + 1 > max_size:
            if current:
                chunks.append(current)
            current = para
        else:
            current += ("\n" if current else "") + para

    if current:
        chunks.append(current)

    return chunks or [text[:max_size]]


async def _translate_chunk(translator, chunk: str, engine: str) -> str:
    """Translate a single chunk with timeout, retry, and engine fallback."""
    last_exc: BaseException | None = None
    for attempt in range(MAX_RETRIES):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(translator.translate, chunk),
                timeout=TRANSLATE_TIMEOUT,
            )
            return result or chunk
        except (asyncio.TimeoutError, Exception) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)

    # Fallback: try alternative engine if primary fails
    fallback_engine = "mymemory" if engine == "google" else "google"
    try:
        fb_translator = _create_translator(
            fallback_engine, translator.source, translator.target
        )
        result = await asyncio.wait_for(
            asyncio.to_thread(fb_translator.translate, chunk),
            timeout=TRANSLATE_TIMEOUT,
        )
        logger.info("Fallback to %s succeeded", fallback_engine)
        return result or chunk
    except Exception:
        pass

    logger.warning("Chunk failed after %d attempts + fallback: %s", MAX_RETRIES, last_exc)
    return chunk


_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def _translate_chunk_cached(
    translator,
    source_lang: str,
    target_lang: str,
    chunk: str,
    engine: str = "google",
) -> str:
    """Translate a single chunk with cache lookup and concurrency limit."""
    if not chunk.strip():
        return chunk

    cached = cache.get(source_lang, target_lang, chunk)
    if cached is not None:
        return cached

    async with _semaphore:
        result = await _translate_chunk(translator, chunk, engine)
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    cache.set(source_lang, target_lang, chunk, result)
    return result


async def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    engine: str = "google",
    glossary: list[str] | None = None,
) -> str:
    if not text or not text.strip():
        return text

    # Protect glossary terms
    replacements = []
    if glossary:
        text, replacements = _protect_glossary(text, glossary)

    chunks = split_text(text)
    translator = _create_translator(engine, source_lang, target_lang)

    translated_chunks = await asyncio.gather(
        *(_translate_chunk_cached(translator, source_lang, target_lang, c, engine) for c in chunks)
    )

    result = "\n".join(translated_chunks)

    # Restore glossary terms
    if replacements:
        result = _restore_glossary(result, replacements)

    return result


async def batch_translate(
    texts: list[str],
    source_lang: str,
    target_lang: str,
    batch_size: int = BATCH_SIZE,
    on_batch_done: Callable[[int], None] | None = None,
    engine: str = "google",
    glossary: list[str] | None = None,
) -> list[str]:
    """Translate a list of texts in batches, preserving order.
    Falls back to individual translation if separator is mangled.
    Calls on_batch_done(completed_count) after each batch finishes."""
    results = list(texts)
    completed = 0

    async def _do_batch(batch_start: int, batch: list[str]):
        nonlocal completed
        nonempty = [(i, t) for i, t in enumerate(batch) if t.strip()]
        if not nonempty:
            completed += len(batch)
            if on_batch_done:
                on_batch_done(completed)
            return

        indices, nonempty_texts = zip(*nonempty)

        joined = BATCH_SEP.join(nonempty_texts)
        translated_joined = await translate_text(
            joined, source_lang, target_lang, engine=engine, glossary=glossary,
        )

        parts = [p.strip() for p in BATCH_SEP_RE.split(translated_joined)]

        if len(parts) == len(nonempty_texts):
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = parts[local_i]
        else:
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = await translate_text(
                    nonempty_texts[local_i], source_lang, target_lang,
                    engine=engine, glossary=glossary,
                )

        completed += len(batch)
        if on_batch_done:
            on_batch_done(completed)

    # Process batches with limited concurrency (2 batches at a time)
    batch_sem = asyncio.Semaphore(2)
    batch_ranges = list(range(0, len(texts), batch_size))

    async def _limited_batch(start: int):
        async with batch_sem:
            await _do_batch(start, texts[start : start + batch_size])

    await asyncio.gather(*(_limited_batch(s) for s in batch_ranges))

    return results
