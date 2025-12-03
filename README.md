# Anilibria_torznab
AniLibria Torznab Bridge
========================

Запуск:
  docker compose up -d --build

Переменные окружения:
  ANILIBRIA_BASE - базовый URL AniLibria API (по умолчанию https://anilibria.top/api)
  ANILIBRIA_SEARCH_PATH - путь поиска (настройте под новый API)
  ANILIBRIA_TORRENT_FIELD - поле в JSON, в котором находятся торрент-ссылки (по умолчанию "torrents")

Endpoints:
  GET /torznab?q=Naruto&limit=50&cat=5070
  GET /capabilities
  GET /health

Добавление в Prowlarr:
  Indexers → Add → Generic → Torznab
  URL: http://<server-ip>:8080/torznab
  API Key: (оставить пустым)
  Categories: 5070 (Anime), 5000, 5030
  Test: нажать "Test" (Prowlarr будет делать запрос к /torznab?q=...)

Примечание:
  - Новый AniLibria API v1 может возвращать JSON с отличной структурой от приведённой здесь.
    Если Prowlarr не получает релизов — откройте контейнер, поправьте переменные:
      - ANILIBRIA_SEARCH_PATH (например /v1/search или /v1/titles)
      - ANILIBRIA_TORRENT_FIELD (например 'files' или 'torrents')
    и при необходимости поправьте mapper в function `map_result_to_item` в app.py
