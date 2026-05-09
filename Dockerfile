# Matches playwright 1.53.x in pyproject.toml (includes Chromium + OS deps).
FROM mcr.microsoft.com/playwright/python:v1.53.0-jammy

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.2

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi

COPY downloader.py tvdb_naming.py rss_qbittorrent.py ./
COPY web_gui ./web_gui/

RUN mkdir -p /downloads

ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["uvicorn", "web_gui.app:app", "--host", "0.0.0.0", "--port", "8765"]
