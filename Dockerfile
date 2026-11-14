FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_THREAD_LIMIT=1 \
    SVERKA_DATA_DIR=/data

# OMP_THREAD_LIMIT=1: несколько одновременных процессов Tesseract иначе зависают, конкурируя за потоки.
# Tesseract с русской и английской моделями, libglib для OpenCV. Интернет нужен только на этапе сборки.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY sverka ./sverka
COPY scripts ./scripts
COPY tests ./tests
COPY examples ./examples
COPY .env.example docker-compose.yml Dockerfile pytest.ini ./
RUN mkdir -p /data /out

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["python", "-m", "sverka"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
