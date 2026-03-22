# Book Translator

> Translate EPUB and PDF books to any language. Covers preserved, Kindle-ready output.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)
![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)
![License](https://img.shields.io/badge/License-MIT-yellow)

**Live:** [kindle-book-translator.onrender.com](https://kindle-book-translator.onrender.com)

---

## Features

- **EPUB & PDF translation** — upload either format, get it back translated
- **OCR for scanned PDFs** — image-based PDFs are OCR'd with Tesseract before translation
- **Format conversion** — EPUB to PDF and back (Calibre when available, smart fallback)
- **Bilingual mode** — original and translation side by side in the same file
- **Cover preserved** — book cover image stays intact in every output
- **100+ languages** — powered by Google Translate, no API key needed
- **Kindle-ready** — output works on Kindle, Kobo, and any e-reader

## How to Use

1. Open the app and **upload** your EPUB or PDF (drag & drop or click)
2. Choose **Translate** or **Convert** tab
3. Select source and target languages
4. Optionally enable **Bilingual mode**
5. Click **Translate Book** and wait for the download

### Sending to Kindle

- [Send to Kindle](https://www.amazon.com/sendtokindle) (web upload)
- Email the EPUB to your Kindle address
- Transfer via USB

---

## Self-hosting

### Docker (recommended)

```bash
git clone https://github.com/gabrielcnb/kindle-book-translator
cd kindle-book-translator
docker-compose up --build
```

Open **http://localhost:8000**.

### Without Docker

```bash
git clone https://github.com/gabrielcnb/kindle-book-translator
cd kindle-book-translator
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

> Note: OCR requires [Tesseract](https://github.com/tesseract-ocr/tesseract) installed. Format conversion is better with [Calibre](https://calibre-ebook.com/) installed.

---

## Architecture

```
Browser -> FastAPI
  |
  |-- EPUB translation: ebooklib -> parse HTML -> batch translate -> repack
  |-- PDF translation:  PyMuPDF -> extract blocks (or OCR) -> translate -> overlay
  |-- EPUB <-> PDF:     Calibre (or paginated fallback)
  |
  +-- Google Translate (deep-translator, batched, cached)
```

| Component | Library |
|-----------|---------|
| Web framework | FastAPI + Uvicorn |
| EPUB processing | ebooklib + BeautifulSoup4 |
| PDF processing | PyMuPDF (fitz) |
| OCR | Tesseract + pytesseract |
| Translation | deep-translator (Google Translate) |
| Format conversion | Calibre CLI (fallback: PyMuPDF) |
| Caching | Disk-backed LRU (50k entries) |

### Performance

- EPUB chapters translated in parallel (`asyncio.gather`)
- Batch translation: ~30 blocks per API call
- Disk-backed translation cache for repeated phrases
- Rate limiting: 5 requests/min per IP, 3 concurrent jobs

### Limitations

- Max file size: 50 MB
- Scanned PDF quality depends on scan resolution
- Complex multi-column PDF layouts may shift slightly
- Free tier (Render): cold starts after inactivity

---

## Deploy on Render

1. Fork this repo
2. Create a new **Web Service** on [Render](https://render.com)
3. Select **Docker** runtime
4. Add env var `LOG_API_KEY` (any secret string, for error monitoring)
5. Deploy

---

## Support

If this tool is useful to you, consider supporting development:

[Buy me a coffee on Ko-fi](https://ko-fi.com/gabrielcnb)

---

## License

MIT
