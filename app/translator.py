import asyncio
from deep_translator import GoogleTranslator
from app import cache

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
DELAY_BETWEEN_REQUESTS = 0.15  # reduced from 0.4
TRANSLATE_TIMEOUT = 30  # seconds per API call
MAX_RETRIES = 2


def split_text(text: str, max_size: int = MAX_CHUNK) -> list[str]:
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    sentences = text.replace("\n", " \n ").split(". ")
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 2 > max_size:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current += (". " if current else "") + sentence
    if current:
        chunks.append(current.strip())
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
        except asyncio.TimeoutError as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
    # Log failure and return original text as fallback
    print(f"[translator] chunk failed after {MAX_RETRIES} attempts: {last_exc}")
    return chunk


async def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    if not text or not text.strip():
        return text

    chunks = split_text(text)
    translated_chunks: list[str] = []

    translator = GoogleTranslator(source=source_lang, target=target_lang)

    for chunk in chunks:
        if not chunk.strip():
            translated_chunks.append(chunk)
            continue

        cached = cache.get(source_lang, target_lang, chunk)
        if cached is not None:
            translated_chunks.append(cached)
            continue

        result = await _translate_chunk(translator, chunk)
        cache.set(source_lang, target_lang, chunk, result)
        translated_chunks.append(result)
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    return " ".join(translated_chunks)
