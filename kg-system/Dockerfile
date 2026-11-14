FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup -S app && adduser -S -G app app

COPY requirements.txt ./
RUN pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app web ./web
COPY --chown=app:app seed ./seed
COPY --chown=app:app ontology_ly/terms ./ontology_ly/terms
COPY --chown=app:app ontology_ly/kernel ./ontology_ly/kernel
COPY --chown=app:app deploy/schema.ngql ./deploy/schema.ngql

RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
