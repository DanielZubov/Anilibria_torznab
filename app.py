from fastapi import FastAPI, Query, Request
from fastapi.responses import Response
import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from typing import Optional

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
# Базовый URL согласно api.json
API_BASE = "https://anilibria.top/api/v1"
# User-Agent, чтобы нас не блочили
USER_AGENT = "AniLiberty-Prowlarr-Bridge/1.0"

# Категории Torznab. 5070 - это стандарт для Аниме.
# Prowlarr и Sonarr ориентируются именно на эти цифры.
CATEGORIES = {
    "5070": "Anime",
    "5000": "TV",
    "2000": "Movies"
}

def get_xml_bytes(elem):
    """Превращает объект XML в красивые байты для ответа"""
    rough_string = ET.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8")

def safe_str(val):
    if val is None: return ""
    return str(val)

def fetch_releases(query: str = None, limit: int = 50):
    """
    Запрос к API АниЛибрии.
    Если есть query -> поиск.
    Если нет query -> последние релизы (RSS).
    """
    headers = {"User-Agent": USER_AGENT}
    
    try:
        if query:
            # Поиск по названию
            url = f"{API_BASE}/app/search/releases"
            params = {"query": query, "limit": limit}
        else:
            # RSS (Лента новинок)
            url = f"{API_BASE}/anime/releases/latest"
            params = {"limit": limit}

        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        # API поиска возвращает список сразу, либо обернутый в data.
        # В api.json для /app/search/releases схема просто array, но перестрахуемся.
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        return []
        
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []

def build_rss_item(release, torrent):
    """
    Создает один <item> для XML.
    release - объект тайтла (название, постер)
    torrent - объект конкретного торрент-файла внутри тайтла
    """
    item = ET.Element("item")
    
    # 1. Формируем заголовок: Название (Рус) / Название (Англ) [Качество] [Серии]
    ru_title = release.get("name", {}).get("main", "Unknown")
    en_title = release.get("name", {}).get("english", "")
    
    # Достаем качество (1080p/720p)
    quality = "Unknown"
    if "quality" in torrent and isinstance(torrent["quality"], dict):
        quality = torrent["quality"].get("description", torrent["quality"].get("value", ""))
    
    # Достаем размер (строкой для заголовка)
    size_bytes = torrent.get("size", 0)
    
    # Серии (информация из самого торрента или из релиза)
    # В объекте torrent поле description часто содержит номера серий (напр "1-12")
    ep_info = torrent.get("description", "")
    if not ep_info:
        ep_info = f"E{release.get('episodes_total', '?')}"
        
    full_title = f"{ru_title} / {en_title} [{quality}] [{ep_info}]"
    ET.SubElement(item, "title").text = full_title
    
    # 2. GUID (Уникальный ID). Используем ID торрента, а не релиза!
    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = f"anilibria-{torrent.get('id')}"
    
    # 3. Ссылка на файл (Enclosure) - Самое важное для Prowlarr
    # Ссылка строится: /anime/torrents/{id}/file
    torrent_id = torrent.get("id")
    download_url = f"{API_BASE}/anime/torrents/{torrent_id}/file"
    
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", download_url)
    enc.set("length", str(size_bytes))
    enc.set("type", "application/x-bittorrent")
    
    # Дублируем ссылку в <link>, некоторые читалки требуют
    ET.SubElement(item, "link").text = download_url

    # 4. Категория (Всегда аниме - 5070)
    ET.SubElement(item, "category").text = "5070"
    
    # 5. Дата публикации
    # Берем дату обновления торрента или релиза
    pub_date_str = torrent.get("updated_at") or release.get("updated_at")
    if pub_date_str:
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # 6. Torznab аттрибуты (сидеры, личеры)
    seeders = torrent.get("seeders", 0)
    leechers = torrent.get("leechers", 0)
    
    ET.SubElement(item, "torznab:attr", name="seeders", value=str(seeders))
    ET.SubElement(item, "torznab:attr", name="peers", value=str(leechers + seeders))
    ET.SubElement(item, "torznab:attr", name="category", value="5070") # Anime
    ET.SubElement(item, "torznab:attr", name="downloadvolumefactor", value="0") # Freeleech часто на аниме трекерах
    
    # 7. Постер
    poster = release.get("poster", {}).get("optimized", {}).get("src")
    if not poster:
        poster = release.get("poster", {}).get("src")
    if poster:
        # Обычно ссылки относительные, нужно добавить домен, если его нет
        if poster.startswith("/"):
             poster = "https://anilibria.top" + poster
        ET.SubElement(item, "torznab:attr", name="poster", value=poster)

    return item

@app.get("/health")
def health():
    return {"status": "ok", "api_url": API_BASE}

@app.get("/torznab")
async def torznab_endpoint(
    t: str = Query("caps"), 
    q: Optional[str] = Query(None), 
    limit: int = Query(50),
    offset: int = Query(0)
):
    # 1. CAPS - Prowlarr спрашивает возможности индексатора
    if t == "caps":
        root = ET.Element("caps")
        server = ET.SubElement(root, "server")
        ET.SubElement(server, "title").text = "AniLibria"
        
        searching = ET.SubElement(root, "searching")
        ET.SubElement(searching, "search", available="yes", supportedParams="q")
        ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,season,ep")
        ET.SubElement(searching, "movie-search", available="yes", supportedParams="q")
        
        categories = ET.SubElement(root, "categories")
        # Объявляем категорию Аниме, чтобы Prowlarr её увидел
        ET.SubElement(categories, "category", id="5070", name="Anime")
        
        return Response(content=get_xml_bytes(root), media_type="application/xml")

    # 2. SEARCH & RSS - Запрос данных
    elif t in ["search", "tvsearch", "movie", "rss"]:
        
        # Получаем данные с АниЛибрии
        releases = fetch_releases(query=q, limit=limit)
        
        # Строим XML ответ
        rss = ET.Element("rss", version="2.0", xmlns_torznab="http://torznab.com/schemas/2015/feed")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        ET.SubElement(channel, "description").text = "AniLibria Torznab Bridge"
        
        count = 0
        # Проходим по каждому релизу
        for release in releases:
            # ВНИМАНИЕ: Нам нужно достать список торрентов из релиза
            torrents_list = release.get("torrents", [])
            
            # Если это объект (иногда бывает в API), превращаем в список
            if isinstance(torrents_list, dict):
                torrents_list = list(torrents_list.values())
            
            # Для каждого торрента внутри релиза создаем отдельную запись
            for torrent in torrents_list:
                item = build_rss_item(release, torrent)
                channel.append(item)
                count += 1
                
        return Response(content=get_xml_bytes(rss), media_type="application/xml")

    else:
        return Response(content="Unknown functionality", status_code=400)