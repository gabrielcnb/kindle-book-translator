"""
Disk-backed translation cache.
Key = SHA-256(source_lang + target_lang + text), value = translated string.
Persists to JSON file; loaded into memory at startup for fast reads.
Thread-safe via threading.Lock. LRU eviction at MAX_ENTRIES.
"""

import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path

_cache_dir = Path.home() / ".book_translator"
_cache_dir.mkdir(exist_ok=True)
CACHE_FILE = _cache_dir / "translations.json"

MAX_ENTRIES = 50_000
_SAVE_EVERY = 100

_lock = threading.Lock()
_cache: OrderedDict[str, str] = OrderedDict()
_new_entries = 0


def _load() -> None:
    global _cache
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            _cache = OrderedDict(data)
    except Exception:
        _cache = OrderedDict()


def _save() -> None:
    try:
        CACHE_FILE.write_text(json.dumps(dict(_cache), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load()


def _key(src: str, tgt: str, text: str) -> str:
    return hashlib.sha256(f"{src}\x00{tgt}\x00{text}".encode()).hexdigest()


def get(src: str, tgt: str, text: str) -> str | None:
    with _lock:
        k = _key(src, tgt, text)
        val = _cache.get(k)
        if val is not None:
            _cache.move_to_end(k)
        return val


def set(src: str, tgt: str, text: str, translation: str) -> None:
    global _new_entries
    with _lock:
        k = _key(src, tgt, text)
        _cache[k] = translation
        _cache.move_to_end(k)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)
        _new_entries += 1
        if _new_entries >= _SAVE_EVERY:
            _save()
            _new_entries = 0


def flush() -> None:
    with _lock:
        _save()


def stats() -> dict:
    return {"entries": len(_cache), "file": str(CACHE_FILE)}
