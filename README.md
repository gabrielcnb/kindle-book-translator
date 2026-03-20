# 📚 Kindle Book Translator

> Translate EPUB and PDF books to any language — covers preserved, Kindle-ready output.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)

---

## ✨ Features

- **EPUB & PDF support** — upload either format, get it back translated
- **Cover preserved** — book cover image stays intact in the output
- **100+ languages** — powered by Google Translate (no API key needed)
- **Kindle-ready EPUB** — output works on Kindle, Kobo, and any e-reader
- **Beautiful UI** — drag-and-drop, progress bar, instant download
- **Docker-ready** — one command to run anywhere

## 🚀 Quick Start

### With Docker (recommended)

```bash
git clone https://github.com/gabrielcnb/kindle-book-translator
cd kindle-book-translator
docker-compose up --build
```

Open **http://localhost:8000** in your browser.

### Without Docker

```bash
git clone https://github.com/gabrielcnb/kindle-book-translator
cd kindle-book-translator

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
python run.py
```

Open **http://localhost:8000** in your browser.

---

## 📖 How to Use

1. **Upload** your EPUB or PDF file (drag & drop or click)
2. **Select** source language (Auto Detect works great)
3. **Select** target language (Portuguese, Spanish, French, etc.)
4. Click **Translate Book**
5. Wait for translation to complete — then the file downloads automatically

### Sending to Kindle

- Use the [Send to Kindle](https://www.amazon.com/sendtokindle) service
- Or email the EPUB to your Kindle email address
- Or transfer via USB

---

## 🔧 Technical Details

| Component | Library |
|-----------|---------|
| Web framework | FastAPI |
| EPUB processing | ebooklib + BeautifulSoup4 |
| PDF processing | PyMuPDF (fitz) |
| Translation | deep-translator (Google Translate) |
| Image handling | Pillow |

### Architecture

```
Browser → FastAPI →
    ├── EPUB: ebooklib → parse HTML → translate text nodes (parallel) → repack EPUB
    └── PDF: PyMuPDF → extract text blocks → translate → rebuild PDF
```

### Performance

- EPUB chapters translated **in parallel** (`asyncio.gather`) — 3–5× faster than sequential
- Up to **4 concurrent** Google Translate calls (semaphore prevents 429 errors)
- Batch translation: **20 blocks per API call** (reduces total calls ~20×)
- Disk-backed translation cache — repeated phrases translated instantly

### Limitations

- Max file size: **50 MB**
- PDF layout preservation is best-effort (complex multi-column layouts may shift)
- Translation speed: typically **30–90 seconds** for a standard novel (previously 3–5 min)

---

## 🛣️ Roadmap

- [x] Bilingual output (original + translation side by side)
- [x] Translation memory (disk-backed cache)
- [ ] DeepL API support (higher quality translations)
- [ ] LibreTranslate support (fully self-hosted, no external calls)
- [ ] MOBI/AZW3 output format
- [ ] Batch translation (multiple books at once)
- [ ] Progress via WebSocket (real-time updates)

---

## 🤝 Contributing

Pull requests welcome! Please open an issue first to discuss what you'd like to change.

```bash
git checkout -b feature/my-feature
git commit -m "Add my feature"
git push origin feature/my-feature
```

---

## 📄 License

MIT — free to use, modify, and distribute.

---

<p align="center">Made with ❤️ for readers everywhere</p>
