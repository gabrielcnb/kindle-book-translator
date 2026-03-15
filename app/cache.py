"""
Disk-backed translation cache.
Key = MD5(source_lang + target_lang + text), value = translated string.
Persists to JSON file; loaded into memory at startup for fast reads.
"""

import hashlib
import json
import tempfile
from pathlib import Path

# Use a stable directory under user home instead of OS tempdir (survives reboots)


_cache_dir = Path.home() / ".book_translator"
_cache_dir.mkdir(exist_ok=True)
CACHE_FILE = _cache_dir / "translations.json"
_cache: dict[str, str] = {}
_new_entries = 0
_SAVE_EVERY = 100  # persist to disk every N new entries


def _load() -> None:
    global _cache
    try:
        if CACHE_FILE.exists():
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _cache = {}


def _save() -> None:
    try:
        CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load()


def _key(src: str, tgt: str, text: str) -> str:
    return hashlib.sha256(f"{src}\x00{tgt}\x00{text}".encode()).hexdigest()


def get(src: str, tgt: str, text: str) -> str | None:
    return _cache.get(_key(src, tgt, text))


def set(src: str, tgt: str, text: str, translation: str) -> None:
    global _new_entries
    _cache[_key(src, tgt, text)] = translation
    _new_entries += 1
    if _new_entries >= _SAVE_EVERY:
        _save()
        _new_entries = 0


def flush() -> None:
    """Force-write cache to disk."""
    _save()


def stats() -> dict:
    return {"entries": len(_cache), "file": str(CACHE_FILE)}
