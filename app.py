from fastapi import FastAPI, Query, Request
from fastapi.responses import Response
import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from typing import Optional
import json

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
API_BASE = "https://anilibria.top/api/v1"
USER_AGENT = "AniLiberty-Prowlarr-Bridge/1.0"

def get_xml_bytes(elem):
    """Превращает объект XML в красивые байты для ответа"""
    rough_string = ET.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8")

def fetch_releases(query: str = None, limit: int = 50):
    """
    Запрос к API АниЛибрии.
    ВАЖНО: Добавлен параметр include=torrents, иначе API вернет пустой список файлов.
    """
    headers = {"User-Agent": USER_AGENT}
    
    try:
        # Параметр include обязателен, чтобы получить magnet-ссылки и файлы
        base_params = {"limit": limit, "include": "torrents"}
        
        if query:
            # Поиск
            url = f"{API_BASE}/app/search/releases"
            base_params["query"] = query
            print(f"DEBUG: Searching for '{query}'...") 
        else:
            # RSS (Лента новинок)
            url = f"{API_BASE}/anime/releases/latest"
            print(f"DEBUG: Fetching latest releases (RSS)...")

        resp = requests.get(url, params=base_params, headers=headers, timeout=15)
        resp.raise_for_status()
        
        data = resp.json()
        
        # Нормализация ответа (иногда data внутри data, иногда список)
        items = []
        if isinstance(data, dict) and "data" in data:
            items = data["data"]
        elif isinstance(data, list):
            items = data
            
        print(f"DEBUG: API returned {len(items)} releases.")
        return items
        
    except Exception as e:
        print(f"ERROR: Fetching data failed: {e}")
        return []

def build_rss_item(release, torrent):
    item = ET.Element("item")
    
    # Заголовок
    ru_title = release.get("name", {}).get("main", "Unknown")
    en_title = release.get("name", {}).get("english", "")
    
    # Качество и размер
    quality = "Unknown"
    if "quality" in torrent and isinstance(torrent["quality"], dict):
        quality = torrent["quality"].get("description", torrent["quality"].get("value", ""))
    
    size_bytes = torrent.get("size", 0)
    
    # Информация о сериях
    ep_info = torrent.get("description", "") # Обычно тут "1-12" или "Episode 5"
    if not ep_info:
        ep_info = f"E{release.get('episodes_total', '?')}"
        
    full_title = f"{ru_title} / {en_title} [{quality}] [{ep_info}]"
    ET.SubElement(item, "title").text = full_title
    
    # GUID (Уникальный ID)
    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = f"anilibria-{torrent.get('id')}"
    
    # Ссылка на файл
    torrent_id = torrent.get("id")
    # Используем API для получения файла, так надежнее
    download_url = f"{API_BASE}/anime/torrents/{torrent_id}/file"
    
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", download_url)
    enc.set("length", str(size_bytes))
    enc.set("type", "application/x-bittorrent")
    ET.SubElement(item, "link").text = download_url

    # Категория 5070 (Anime)
    ET.SubElement(item, "category").text = "5070"
    
    # Дата
    pub_date_str = torrent.get("updated_at") or release.get("updated_at")
    if pub_date_str:
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Torznab attributes
    seeders = torrent.get("seeders", 0)
    leechers = torrent.get("leechers", 0)
    
    ET.SubElement(item, "torznab:attr", name="seeders", value=str(seeders))
    ET.SubElement(item, "torznab:attr", name="peers", value=str(leechers + seeders))
    ET.SubElement(item, "torznab:attr", name="category", value="5070")
    
    # Постер
    poster = release.get("poster", {}).get("optimized", {}).get("src")
    if not poster:
        poster = release.get("poster", {}).get("src")
    if poster:
        if poster.startswith("/"):
             poster = "https://anilibria.top" + poster
        ET.SubElement(item, "torznab:attr", name="poster", value=poster)

    return item

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/torznab")
async def torznab_endpoint(
    t: str = Query("caps"), 
    q: Optional[str] = Query(None), 
    limit: int = Query(50),
    offset: int = Query(0)
):
    # 1. CAPS
    if t == "caps":
        root = ET.Element("caps")
        server = ET.SubElement(root, "server")
        ET.SubElement(server, "title").text = "AniLibria"
        
        searching = ET.SubElement(root, "searching")
        ET.SubElement(searching, "search", available="yes", supportedParams="q")
        ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,season,ep")
        ET.SubElement(searching, "movie-search", available="yes", supportedParams="q")
        
        categories = ET.SubElement(root, "categories")
        ET.SubElement(categories, "category", id="5070", name="Anime")
        
        return Response(content=get_xml_bytes(root), media_type="application/xml")

    # 2. SEARCH & RSS
    # Prowlarr может слать t=search, t=tvsearch, t=movie. Обрабатываем все как поиск.
    elif t in ["search", "tvsearch", "movie", "rss"]:
        
        releases = fetch_releases(query=q, limit=limit)
        
        rss = ET.Element("rss", version="2.0", xmlns_torznab="http://torznab.com/schemas/2015/feed")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        
        generated_count = 0
        for release in releases:
            # Получаем список торрентов. Если include=torrents не сработал, тут будет пусто.
            torrents_list = release.get("torrents", [])
            
            # API может вернуть словарь вместо массива, проверяем
            if isinstance(torrents_list, dict):
                torrents_list = list(torrents_list.values())
            
            if not torrents_list:
                # DEBUG: Если торрентов нет, значит что-то не так с API или include
                # print(f"DEBUG: Release {release.get('id')} has no torrents.")
                pass

            for torrent in torrents_list:
                try:
                    item = build_rss_item(release, torrent)
                    channel.append(item)
                    generated_count += 1
                except Exception as e:
                    print(f"ERROR building item: {e}")

        print(f"DEBUG: Generated {generated_count} XML items for Prowlarr.")
        
        # Если items = 0, Prowlarr выдаст ошибку.
        # Если это чистый тест (без query) и API вернуло 0, можно добавить фейковый айтем, 
        # но лучше починить получение данных. С include=torrents должно работать.
        
        return Response(content=get_xml_bytes(rss), media_type="application/xml")

    else:
        return Response(content="Unknown functionality", status_code=400)