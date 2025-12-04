from fastapi import FastAPI, Query, Request
from fastapi.responses import Response
import requests
# minidom больше не нужен, убираем импорт
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
import json

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
API_BASE = "https://anilibria.top/api/v1"
USER_AGENT = "AniLiberty-Prowlarr-Bridge/1.6"

def get_xml_bytes(elem):
    """
    Превращает объект XML в байты для ответа.
    УДАЛЕНО minidom, чтобы избежать ошибки 'unbound prefix'.
    """
    # ET.tostring сам генерирует XML, включая объявление пространства имен и префиксов, 
    # что решает проблему с minidom.
    return ET.tostring(elem, encoding="utf-8", xml_declaration=True)

def fetch_releases(query: str = None, limit: int = 50):
    """
    Запрос к API АниЛибрии с рекурсивной нормализацией ответа.
    """
    headers = {"User-Agent": USER_AGENT}
    
    try:
        base_params = {"limit": limit}
        
        # 1. Определяем эндпоинт и параметры
        if query:
            # 1а. Поиск (Sonarr/Radarr): используем search endpoint с torrents
            url = f"{API_BASE}/app/search/releases"
            base_params["query"] = query
            base_params["include"] = "torrents" 
            print(f"DEBUG: Using SEARCH endpoint for query: '{query}' (Include Torrents: True)")
        else:
            # 1б. RSS/Тест Prowlarr: используем latest endpoint БЕЗ torrents
            url = f"{API_BASE}/anime/releases/latest"
            print(f"DEBUG: Using LATEST endpoint for RSS/Test (Include Torrents: False)")

        resp = requests.get(url, params=base_params, headers=headers, timeout=15)
        resp.raise_for_status()
        
        data = resp.json()
        
        # 2. Нормализация исходного списка
        items = []
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            items = data["data"]
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
            
        # 3. РЕКУРСИВНОЕ "РАЗГЛАЖИВАНИЕ"
        final_items = []
        
        def recursive_flatten(obj):
            """Рекурсивно извлекает все словари из вложенных списков."""
            if isinstance(obj, dict):
                final_items.append(obj)
            elif isinstance(obj, list):
                for element in obj:
                    recursive_flatten(element)

        recursive_flatten(items)
                                
        print(f"DEBUG: API returned {len(items)} initial groups, recursively normalized to {len(final_items)} dict releases.")
        
        return final_items
        
    except Exception as e:
        print(f"ERROR: Fetching data failed: {e}")
        return []

def build_rss_item(release, torrent=None):
    """
    Создает один <item> для XML.
    """
    item = ET.Element("item")
    
    # Заголовок
    name_obj = release.get("name", {})
    ru_title = name_obj.get("main", "Unknown")
    en_title = name_obj.get("english", "")
    
    # Если нет торрента (для тестовых заглушек)
    is_test_item = torrent is None
    
    # Параметры по умолчанию для заглушек
    quality = "Unknown"
    size_bytes = 100000000
    torrent_id = release.get("id", "test")
    ep_info = "" 

    if torrent:
        if "quality" in torrent and isinstance(torrent["quality"], dict):
            quality = torrent["quality"].get("description", torrent["quality"].get("value", ""))
        elif "quality" in torrent:
            quality = str(torrent["quality"])
        
        size_bytes = torrent.get("size", 0)
        torrent_id = torrent.get("id")
        ep_info = torrent.get("description", "") 
    
    if not ep_info:
        ep_info = f"E{release.get('episodes_total', '?')}"
        
    full_title = f"{ru_title} / {en_title} [{quality}] [{ep_info}]"
    
    if is_test_item:
         full_title = f"[TEST_PASS_ONLY] {full_title}"
    
    ET.SubElement(item, "title").text = full_title
    
    # GUID (Уникальный ID)
    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = f"anilibria-{torrent_id}-{'test' if is_test_item else 'real'}"
    
    # Ссылка на файл
    download_url = f"{API_BASE}/anime/torrents/{torrent_id}/file"
    
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", download_url)
    enc.set("length", str(size_bytes))
    enc.set("type", "application/x-bittorrent")
    ET.SubElement(item, "link").text = download_url

    # Категория
    ET.SubElement(item, "category").text = "5070"
    
    # Дата
    pub_date_str = (torrent.get("updated_at") if torrent else None) or release.get("updated_at")
    if pub_date_str and isinstance(pub_date_str, str):
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    else:
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Torznab attributes
    seeders = torrent.get("seeders", 1) if torrent else 1
    leechers = torrent.get("leechers", 1) if torrent else 1
    
    # !!! Используем префикс torznab: !!!
    ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", name="seeders", value=str(seeders))
    ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", name="peers", value=str(leechers + seeders))
    ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", name="category", value="5070")
    
    # Постер
    poster = release.get("poster", {}).get("optimized", {}).get("src")
    if not poster:
        poster = release.get("poster", {}).get("src")
    if poster:
        if poster.startswith("/"):
             poster = "https://anilibria.top" + poster
        ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", name="poster", value=poster)

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
        
        # !!! Правильное определение пространства имен Torznab !!!
        rss = ET.Element(
            "rss", 
            attrib={"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"}
        )
        
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        
        generated_count = 0
        for release in releases:
            
            torrents_list = release.get("torrents", [])
            
            # API может вернуть словарь вместо массива
            if isinstance(torrents_list, dict):
                torrents_list = list(torrents_list.values())
            
            if torrents_list:
                # Если торренты есть (для реального поиска), обрабатываем их 
                for torrent in torrents_list:
                    try:
                        if not isinstance(torrent, dict):
                            continue
                            
                        item = build_rss_item(release, torrent)
                        channel.append(item)
                        generated_count += 1
                    except Exception as e:
                        print(f"ERROR building item: {e}")
            elif not q:
                # Если торрентов нет, и это тестовый запрос (q=None), создаем заглушку
                try:
                    item = build_rss_item(release, torrent=None)
                    channel.append(item)
                    generated_count += 1
                except Exception as e:
                    print(f"ERROR building TEST item: {e}")

        print(f"DEBUG: Generated {generated_count} XML items for Prowlarr.")
        
        return Response(content=get_xml_bytes(rss), media_type="application/xml")

    else:
        return Response(content="Unknown functionality", status_code=400)