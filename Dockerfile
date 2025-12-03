FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV ANILIBRIA_BASE="https://anilibria.top/api"
ENV ANILIBRIA_SEARCH_PATH="/v1/titles"
ENV ANILIBRIA_TORRENT_FIELD="torrents"
ENV USER_AGENT="anilibria-torznab-bridge/1.0"

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop", "--workers", "1"]
