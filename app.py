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
USER_AGENT = "AniLiberty-Prowlarr-Bridge/5.4" # Обновляем версию

def get_xml_bytes(elem):
    """Превращает объект XML в байты."""
    return ET.tostring(elem, encoding="utf-8", xml_declaration=True)

# НОВАЯ ФУНКЦИЯ: Получает только метаданные релиза (для RSS/Latest)
def fetch_release_metadata(release_id: int) -> Optional[dict]:
    """
    Получает необходимые метаданные релиза по его ID. 
    Используется для RSS/Latest, чтобы получить контекст для каждого недавно вышедшего торрента.
    """
    # Используем include для минимально необходимых полей
    fields_to_include = "id,name,alias,poster,episodes_total,updated_at"
    url = f"{API_BASE}/anime/releases/{release_id}?include={fields_to_include}&exclude=description,franchises,torrents"
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ERROR: Failed to fetch release metadata {release_id}: {e}") 
        return None

# НОВАЯ ФУНКЦИЯ: Получает релиз и все его торренты в одном запросе (для Search)
def fetch_release_with_torrents(release_id: int) -> Optional[dict]:
    """
    Получает полный объект релиза и все его торренты в одном запросе.
    Используется для Search.
    """
    # Используем include=torrents, чтобы получить все в одном запросе
    fields_to_include = "id,name,alias,poster,episodes_total,updated_at,torrents"
    url = f"{API_BASE}/anime/releases/{release_id}?include={fields_to_include}&exclude=description,franchises"
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ERROR: Failed to fetch full release with torrents {release_id}: {e}") 
        return None

def fetch_latest_torrents(limit: int = 50) -> list:
    """Получает список последних торрентов в формате JSON (Шаг 1 RSS/Test)."""
    api_limit = min(limit, 50) 
    
    url = f"{API_BASE}/anime/torrents"
    headers = {"User-Agent": USER_AGENT}
    base_params = {"limit": api_limit} 
    
    print(f"DEBUG: Using /anime/torrents JSON endpoint for RSS/Latest (API Limit: {api_limit}).")
    
    try:
        resp = requests.get(url, params=base_params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        items = []
        
        if isinstance(data, dict) and 'data' in data:
            items = data['data']
        
        if not items and isinstance(data, dict):
            items_candidate = data.get('torrents', data)
            if isinstance(items_candidate, dict):
                items = list(items_candidate.values())
            elif isinstance(items_candidate, list):
                 items = items_candidate

        if not isinstance(items, list):
            items = []

        final_items = [item for item in items if isinstance(item, dict) and 'id' in item]

        print(f"DEBUG: Successfully retrieved {len(final_items)} torrents from /anime/torrents.")
        return final_items

    except Exception as e:
        print(f"ERROR: Fetching latest torrents failed: {e}")
        return []

def fetch_releases(query: str = None, limit: int = 50) -> list:
    """Получает список релизов по поисковому запросу. (Шаг 1 Поиска)"""
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

def build_rss_item(release, torrent):
    item = ET.Element("item")
    name_obj = release.get("name", {})
    en_title = name_obj.get("english", name_obj.get("main", "Unknown Series")) 
    
    quality = "Unknown"
    if "quality" in torrent and isinstance(torrent["quality"], dict):
        quality = torrent["quality"].get("description", torrent["quality"].get("value", ""))
    elif "quality" in torrent:
         quality = str(torrent["quality"])
         
    size_bytes = torrent.get("size", 0)
    torrent_id = torrent.get("id")
    
    # 1. Base Title 
    full_title = en_title.strip()
    
    # 2. Episode/Part Info
    ep_info = torrent.get("description", "") or "" 
    
    # 2a. Удаляем русские слова/метки
    ep_info = (ep_info
               .replace('[Фильм]', '')
               .replace('[Спешл]', '')
               .replace('[OVA]', '')
               .replace('/', '')
               .strip())
    
    if not ep_info:
        episodes_total = release.get('episodes_total')
        if episodes_total and episodes_total > 0:
             # Если описание пустое, но есть episodes_total, используем AEN-пак 1-XX
             ep_info = f"1-{episodes_total}" 
        else:
             ep_info = ""

    # 2b. Форматируем оставшуюся информацию об эпизодах
    if ep_info:
        # Убираем все скобки из диапазона (например, [1-12] -> 1-12)
        cleaned_ep_info = ep_info.strip().replace('[', '').replace(']', '').replace(' ', '')
        
        # Если это диапазон (напр. 1-12, 14-24), добавляем его без скобок (лучше для AEN парсинга Sonarr)
        if any(char in cleaned_ep_info for char in ['-', ',']):
            full_title += f" {cleaned_ep_info}" 
        # Если это один эпизод, форматируем как E01
        elif cleaned_ep_info.isdigit() and len(cleaned_ep_info) <= 3:
            full_title += f" E{int(cleaned_ep_info):02d}"
        # Если это текстовая метка (например, Ryuusui), оставляем ее в скобках
        elif cleaned_ep_info:
             full_title += f" [{cleaned_ep_info}]"

    # --- НОВОЕ: ЯВНО ДОБАВЛЯЕМ РУССКИЙ ЯЗЫК ДЛЯ SONARR/PROWLARR (ОСНОВНАЯ ЦЕЛЬ ЭТОЙ ВЕРСИИ) ---
    full_title += " [Rus]"
    
    # 3. Add Quality 
    if quality and quality != "Unknown":
        full_title += f" [{quality}]"
        
    # --- Задаем TITLE ---
    ET.SubElement(item, "title").text = full_title
    
    # --- Остальная часть функции ---
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
    # 1. CAPS - Без изменений
    if t == "caps":
        root = ET.Element("caps")
        server = ET.SubElement(root, "server")
        ET.SubElement(server, "title").text = "AniLibria"
        searching = ET.SubElement(root, "searching")
        ET.SubElement(searching, "search", available="yes", supportedParams="q")
        ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,season,ep")
        ET.SubElement(searching, "movie-search", available="yes", supportedParams="q")
        categories = ET.SubElement(root, "categories")
        ET.SubElement(categories, "category", id="5000", name="TV") 
        ET.SubElement(categories, "category", id="5070", name="Anime")
        return Response(content=get_xml_bytes(root), media_type="application/xml")

    # 2. RSS/Latest (t=search, q=None) - Используем fetch_release_metadata
    elif t in ["search", "tvsearch", "movie", "rss"] and not q:
        items_to_process = []
        latest_torrents = fetch_latest_torrents(limit=limit) 
        
        for torrent in latest_torrents:
            release_id = torrent.get('release_id') or torrent.get('release', {}).get('id')
            
            if not release_id:
                print(f"WARNING: Torrent {torrent.get('id')} lacks required release_id. Skipping.")
                continue

            # ИСПОЛЬЗУЕМ НОВУЮ ФУНКЦИЮ (оптимизированный N+1 запрос)
            release = fetch_release_metadata(release_id) 
            
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


    # 3. SEARCH (t=search, q=exists) - Используем fetch_release_with_torrents
    elif t in ["search", "tvsearch", "movie", "rss"] and q:
        
        # Шаг 1: Ищем релизы по запросу
        release_summaries = fetch_releases(query=q, limit=limit)
        items_to_process = []
        
        # Шаг 2: Для каждого релиза получаем полный объект с торрентами ОДНИМ запросом (ОПТИМИЗАЦИЯ!)
        for release_summary in release_summaries:
            release_id = release_summary.get("id")
            if not release_id:
                continue
            
            print(f"DEBUG: Two-step search: Fetching full release with torrents for ID {release_id}...")
            
            full_release = fetch_release_with_torrents(release_id)
            
            if full_release and 'torrents' in full_release:
                torrents_list = full_release['torrents']
                
                for torrent in torrents_list:
                    # Теперь передаем полный объект релиза в build_rss_item
                    items_to_process.append((full_release, torrent))
        
        
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