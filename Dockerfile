FROM python:3.11-slim

WORKDIR /app

# Calibre for high-quality EPUB↔PDF conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    calibre \
    xvfb \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
