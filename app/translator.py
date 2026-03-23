import asyncio
import logging
import re
from typing import Callable

from deep_translator import GoogleTranslator
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

MAX_CHUNK = 4500
DELAY_BETWEEN_REQUESTS = 0.05
TRANSLATE_TIMEOUT = 30
MAX_RETRIES = 2
MAX_CONCURRENT = 5  # parallel translation requests

# Batch translation constants
BATCH_SEP = "\n|||KBTXSEP|||\n"
BATCH_SEP_RE = re.compile(re.escape(BATCH_SEP))
BATCH_SIZE = 12


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


async def _translate_chunk(translator: GoogleTranslator, chunk: str) -> str:
    """Translate a single chunk with timeout and retry."""
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

    logger.warning("Chunk failed after %d attempts: %s", MAX_RETRIES, last_exc)
    return chunk


_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def _translate_chunk_cached(
    translator: GoogleTranslator,
    source_lang: str,
    target_lang: str,
    chunk: str,
) -> str:
    """Translate a single chunk with cache lookup and concurrency limit."""
    if not chunk.strip():
        return chunk

    cached = cache.get(source_lang, target_lang, chunk)
    if cached is not None:
        return cached

    async with _semaphore:
        result = await _translate_chunk(translator, chunk)
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    cache.set(source_lang, target_lang, chunk, result)
    return result


async def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    if not text or not text.strip():
        return text

    chunks = split_text(text)
    translator = GoogleTranslator(source=source_lang, target=target_lang)

    translated_chunks = await asyncio.gather(
        *(_translate_chunk_cached(translator, source_lang, target_lang, c) for c in chunks)
    )

    return "\n".join(translated_chunks)


async def batch_translate(
    texts: list[str],
    source_lang: str,
    target_lang: str,
    batch_size: int = BATCH_SIZE,
    on_batch_done: Callable[[int], None] | None = None,
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
        translated_joined = await translate_text(joined, source_lang, target_lang)

        parts = [p.strip() for p in BATCH_SEP_RE.split(translated_joined)]

        if len(parts) == len(nonempty_texts):
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = parts[local_i]
        else:
            for local_i, global_i in enumerate(indices):
                results[batch_start + global_i] = await translate_text(
                    nonempty_texts[local_i], source_lang, target_lang
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
