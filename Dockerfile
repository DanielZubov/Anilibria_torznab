FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV ANILIBRIA_BASE="https://anilibria.top/api"
ENV ANILIBRIA_SEARCH_PATH="/v1/search/releases"
ENV ANILIBRIA_TORRENT_FIELD="torrents"
ENV USER_AGENT="anilibria-torznab-bridge/1.0"

EXPOSE 8020

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8020", "--loop", "uvloop", "--workers", "1"]
