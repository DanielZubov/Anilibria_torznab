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
        
        items = []
        # Логика обработки разных вариантов ответа API
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], list):
                items = data["data"]
            else:
                # Если вернулся один объект или странная структура, попробуем обернуть
                # Но для начала логируем, чтобы понять структуру
                # print(f"DEBUG: Raw dict response keys: {data.keys()}")
                items = [data] # Попытка обработки одиночного объекта
        elif isinstance(data, list):
            items = data
            
        print(f"DEBUG: API returned {len(items)} items.")
        
        # DEBUG: Выведем тип первого элемента, чтобы понять, почему падает
        if items:
            print(f"DEBUG: First item type: {type(items[0])}")
            if not isinstance(items[0], dict):
                 print(f"DEBUG: First item content (partial): {str(items[0])[:100]}")

        return items
        
    except Exception as e:
        print(f"ERROR: Fetching data failed: {e}")
        return []

def build_rss_item(release, torrent):
    item = ET.Element("item")
    
    # Заголовок
    name_obj = release.get("name", {})
    if not isinstance(name_obj, dict):
         name_obj = {} # Защита от странных данных
         
    ru_title = name_obj.get("main", "Unknown")
    en_title = name_obj.get("english", "")
    
    # Качество и размер
    quality = "Unknown"
    if "quality" in torrent and isinstance(torrent["quality"], dict):
        quality = torrent["quality"].get("description", torrent["quality"].get("value", ""))
    elif "quality" in torrent:
         quality = str(torrent["quality"])
    
    size_bytes = torrent.get("size", 0)
    
    # Информация о сериях
    ep_info = torrent.get("description", "") 
    if not ep_info:
        ep_info = f"E{release.get('episodes_total', '?')}"
        
    full_title = f"{ru_title} / {en_title} [{quality}] [{ep_info}]"
    ET.SubElement(item, "title").text = full_title
    
    # GUID
    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = f"anilibria-{torrent.get('id')}"
    
    # Ссылка на файл
    torrent_id = torrent.get("id")
    download_url = f"{API_BASE}/anime/torrents/{torrent_id}/file"
    
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", download_url)
    enc.set("length", str(size_bytes))
    enc.set("type", "application/x-bittorrent")
    ET.SubElement(item, "link").text = download_url

    # Категория
    ET.SubElement(item, "category").text = "5070"
    
    # Дата
    pub_date_str = torrent.get("updated_at") or release.get("updated_at")
    if pub_date_str and isinstance(pub_date_str, str):
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    else:
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
    elif t in ["search", "tvsearch", "movie", "rss"]:
        
        releases = fetch_releases(query=q, limit=limit)
        
        rss = ET.Element("rss", version="2.0", xmlns_torznab="http://torznab.com/schemas/2015/feed")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        
        generated_count = 0
        for release in releases:
            # ЗАЩИТА ОТ БИТОЙ СТРУКТУРЫ:
            if not isinstance(release, dict):
                print(f"WARN: Skipping item because it is not a dict: {type(release)}")
                continue

            # Получаем список торрентов.
            torrents_list = release.get("torrents", [])
            
            # API может вернуть словарь вместо массива
            if isinstance(torrents_list, dict):
                torrents_list = list(torrents_list.values())
            
            # Если include не сработал или торрентов нет, список будет пуст
            if not torrents_list:
                # Можно попробовать логировать ID релиза, чтобы проверить его на сайте
                # print(f"DEBUG: No torrents for release {release.get('id')}")
                continue

            for torrent in torrents_list:
                try:
                    # Еще одна защита, если торрент внутри списка не словарь
                    if not isinstance(torrent, dict):
                        continue
                        
                    item = build_rss_item(release, torrent)
                    channel.append(item)
                    generated_count += 1
                except Exception as e:
                    print(f"ERROR building item: {e}")

        print(f"DEBUG: Generated {generated_count} XML items for Prowlarr.")
        
        return Response(content=get_xml_bytes(rss), media_type="application/xml")

    else:
        return Response(content="Unknown functionality", status_code=400)