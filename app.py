from fastapi import FastAPI, Query, Request
from fastapi.responses import Response
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
import json

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
API_BASE = "https://anilibria.top/api/v1"
USER_AGENT = "AniLiberty-Prowlarr-Bridge/2.9" # Обновляем версию

def get_xml_bytes(elem):
    """Превращает объект XML в байты."""
    return ET.tostring(elem, encoding="utf-8", xml_declaration=True)

def fetch_release_by_id(release_id: int) -> Optional[dict]:
    """Получает полный объект релиза по его ID. (Исправлен URL)"""
    # ИСПРАВЛЕНО: Теперь используется правильный путь: /anime/releases/{id}
    url = f"{API_BASE}/anime/releases/{release_id}" 
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ERROR: Failed to fetch full release {release_id}: {e}")
        return None

def fetch_latest_torrents(limit: int = 50) -> list:
    """Получает список последних торрентов в формате JSON (Шаг 1 RSS/Test)."""
    url = f"{API_BASE}/anime/torrents"
    headers = {"User-Agent": USER_AGENT}
    base_params = {"limit": limit}

    print(f"DEBUG: Using /anime/torrents JSON endpoint for RSS/Latest (URL: {url}?limit={limit}).")
    
    try:
        resp = requests.get(url, params=base_params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        items = []
        
        # 1. Приоритетное извлечение из ключа 'data' (как в вашем логе)
        if isinstance(data, dict) and 'data' in data:
            items = data['data']
        
        # 2. Обработка старого формата или корневого словаря с 'torrents'
        if not items and isinstance(data, dict):
            items_candidate = data.get('torrents', data)
            if isinstance(items_candidate, dict):
                items = list(items_candidate.values())
            elif isinstance(items_candidate, list):
                 items = items_candidate

        if not isinstance(items, list):
            items = []

        # Фильтруем для надежности
        final_items = [item for item in items if isinstance(item, dict) and 'id' in item]

        print(f"DEBUG: Successfully retrieved {len(final_items)} torrents from /anime/torrents.")
        return final_items

    except Exception as e:
        print(f"ERROR: Fetching latest torrents failed: {e}")
        return []

def fetch_torrents_for_release(release_id: int) -> list:
    """Получает данные о торрентах для конкретного ID релиза. (Шаг 2 Поиска)"""
    url = f"{API_BASE}/anime/torrents/release/{release_id}"
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        torrents_data = data
        if isinstance(data, dict):
            torrents_data = data.get('torrents', data)
        final_torrents = []
        if isinstance(torrents_data, dict):
            final_torrents = list(torrents_data.values())
        elif isinstance(torrents_data, list):
            final_torrents = torrents_data
        cleaned_torrents = [t for t in final_torrents if isinstance(t, dict)]
        print(f"DEBUG: Found {len(cleaned_torrents)} torrents for release ID {release_id}.")
        return cleaned_torrents
    except Exception as e:
        print(f"ERROR: Failed to fetch torrents for release {release_id}: {e}")
        return []

def fetch_releases(query: str = None, limit: int = 50) -> list:
    """Получает список релизов. (Шаг 1 Поиска)"""
    headers = {"User-Agent": USER_AGENT}
    try:
        url = f"{API_BASE}/app/search/releases"
        resp = requests.get(url, params={"limit": limit, "query": query}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        final_items = []
        def recursive_flatten(obj):
            if isinstance(obj, dict):
                if "release" in obj and isinstance(obj["release"], dict):
                    final_items.append(obj["release"])
                    return
                if "id" in obj and "name" in obj:
                    final_items.append(obj)
                    return
                for value in obj.values():
                    recursive_flatten(value)
            elif isinstance(obj, list):
                for element in obj:
                    recursive_flatten(element)
        recursive_flatten(items)
        print(f"DEBUG: API returned {len(items)} initial groups, recursively normalized to {len(final_items)} dict releases.")
        return final_items
    except Exception as e:
        print(f"ERROR: Fetching data failed: {e}")
        return []

# Функция build_rss_item() без изменений.
def build_rss_item(release, torrent):
    item = ET.Element("item")
    name_obj = release.get("name", {})
    ru_title = name_obj.get("main", "Unknown")
    en_title = name_obj.get("english", "")
    quality = "Unknown"
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
    ET.SubElement(item, "title").text = full_title
    guid = ET.SubElement(item, "guid", isPermaLink="false")
    guid.text = f"anilibria-{torrent_id}"
    download_url = f"{API_BASE}/anime/torrents/{torrent_id}/file"
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", download_url)
    enc.set("length", str(size_bytes))
    enc.set("type", "application/x-bittorrent")
    ET.SubElement(item, "link").text = download_url
    ET.SubElement(item, "category").text = "5070" 
    pub_date_str = torrent.get("updated_at") or release.get("updated_at")
    if pub_date_str and isinstance(pub_date_str, str):
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    else:
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    seeders = torrent.get("seeders", 0)
    leechers = torrent.get("leechers", 0)
    TORZNAB_NAMESPACE = "{http://torznab.com/schemas/2015/feed}"
    ET.SubElement(item, TORZNAB_NAMESPACE + "attr", name="seeders", value=str(seeders))
    ET.SubElement(item, TORZNAB_NAMESPACE + "attr", name="peers", value=str(leechers + seeders))
    ET.SubElement(item, TORZNAB_NAMESPACE + "attr", name="category", value="5070")
    poster = release.get("poster", {}).get("optimized", {}).get("src")
    if not poster:
        poster = release.get("poster", {}).get("src")
    if poster:
        if poster.startswith("/"):
             poster = "https://anilibria.top" + poster
        ET.SubElement(item, TORZNAB_NAMESPACE + "attr", name="poster", value=poster)
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
    # 1. CAPS - без изменений
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

    # 2. RSS/Latest (t=search, q=None) - Двухшаговый процесс
    elif t in ["search", "tvsearch", "movie", "rss"] and not q:
        items_to_process = []
        latest_torrents = fetch_latest_torrents(limit=limit) # Шаг 1: Получаем список торрентов
        
        for torrent in latest_torrents:
            # Пытаемся получить ID релиза из торрента
            release_id = torrent.get('release_id') or torrent.get('release', {}).get('id')
            
            if not release_id:
                print(f"WARNING: Torrent {torrent.get('id')} lacks required release_id. Skipping.")
                continue

            # Шаг 2: Получаем полный объект релиза (используя исправленный URL)
            release = fetch_release_by_id(release_id)
            
            if release:
                items_to_process.append((release, torrent))
            
        rss = ET.Element(
            "rss", 
            attrib={"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"}
        )
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        
        generated_count = 0
        for release, torrent in items_to_process:
            try:
                item = build_rss_item(release, torrent)
                channel.append(item)
                generated_count += 1
            except Exception as e:
                print(f"ERROR building item: {e}")

        print(f"DEBUG: Generated {generated_count} XML items for Prowlarr/Sonarr RSS.")
        return Response(content=get_xml_bytes(rss), media_type="application/xml")


    # 3. SEARCH (t=search, q=exists) - работает
    elif t in ["search", "tvsearch", "movie", "rss"] and q:
        
        releases = fetch_releases(query=q, limit=limit)
        items_to_process = []
        
        for release in releases:
            release_id = release.get("id")
            if not release_id:
                continue
            
            print(f"DEBUG: Two-step search: Fetching torrents for release ID {release_id}...")
            torrents_list = fetch_torrents_for_release(release_id)
            
            for torrent in torrents_list:
                items_to_process.append((release, torrent))
        
        
        rss = ET.Element(
            "rss", 
            attrib={"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"}
        )
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = "AniLibria"
        
        generated_count = 0
        for release, torrent in items_to_process:
            try:
                if not isinstance(torrent, dict):
                    continue
                    
                item = build_rss_item(release, torrent)
                channel.append(item)
                generated_count += 1
            except Exception as e:
                print(f"ERROR building item: {e}")

        print(f"DEBUG: Generated {generated_count} XML items for Prowlarr Search.")
        
        return Response(content=get_xml_bytes(rss), media_type="application/xml")

    else:
        return Response(content="Unknown functionality", status_code=400) 