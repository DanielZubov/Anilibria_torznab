# app.py
from fastapi import FastAPI, Query
from fastapi.responses import Response
import requests
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, List

app = FastAPI()

ANILIBRIA_BASE = os.getenv("ANILIBRIA_BASE", "https://anilibria.top/api")
ANILIBRIA_SEARCH_PATH = os.getenv("ANILIBRIA_SEARCH_PATH", "/v1/titles")
ANILIBRIA_TORRENT_FIELD = os.getenv("ANILIBRIA_TORRENT_FIELD", "torrents")

CATEGORIES = {
    "5070": {"name": "Anime", "query_cat": "anime"},
    "5000": {"name": "TV", "query_cat": "tv"},
    "5030": {"name": "AnimeOther", "query_cat": "anime_other"}
}

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

def safe_text(value) -> str:
    if isinstance(value, str):
        return value
    elif isinstance(value, (int, float)):
        return str(value)
    return str(value)

def iso_to_rfc2822(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.utcnow()
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

def fetch_anilibria(query: str, limit: int = 50, category: Optional[str] = None,
                    season: Optional[str] = None, ep: Optional[str] = None) -> List[dict]:
    base = ANILIBRIA_BASE.rstrip("/")
    path = ANILIBRIA_SEARCH_PATH.lstrip("/")
    url = f"{base}/{path}"

    params = {"query": query, "limit": limit}
    if category:
        for cid, info in CATEGORIES.items():
            if cid == category:
                params["category"] = info["query_cat"]
                break
    if season:
        params["season"] = season
    if ep:
        params["episode"] = ep

    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        fallback = os.getenv("ANILIBRIA_FALLBACK_V3", "https://api.anilibria.tv/v3/search/torznab")
        try:
            r = SESSION.get(fallback, params={"q": query, "limit": limit}, timeout=10)
            r.raise_for_status()
            return {"__torznab_xml__": r.text}
        except Exception:
            return []

    if isinstance(data, dict):
        for key in ("data", "results", "items", "titles"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    elif isinstance(data, list):
        return data
    return []

def map_result_to_item(res: dict) -> ET.Element:
    item = ET.Element("item")

    # title
    title_field = res.get("title") or res.get("name") or "Unknown title"
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or next(iter(title_field.values()))
    else:
        title = title_field
    ET.SubElement(item, "title").text = safe_text(title)

    # link
    link = res.get("site_url") or f"https://www.anilibria.top/releases/{res.get('id')}"
    ET.SubElement(item, "link").text = safe_text(link)

    # guid
    guid = ET.SubElement(item, "guid")
    guid.text = safe_text(res.get("id") or link)
    guid.set("isPermaLink", "false")

    # pubDate
    pub = res.get("published") or datetime.utcnow().isoformat()
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)

    # category
    cat_field = res.get("genre") or "Anime"
    if isinstance(cat_field, dict):
        cat_text = cat_field.get("name") or next(iter(cat_field.values()))
    else:
        cat_text = cat_field
    ET.SubElement(item, "category").text = safe_text(cat_text)

    # description
    desc = res.get("description") or ""
    ET.SubElement(item, "description").text = safe_text(desc)

    # thumb / poster
    poster_field = res.get("poster")
    if isinstance(poster_field, dict):
        poster_url = poster_field.get("src") or poster_field.get("preview") or poster_field.get("thumbnail")
    else:
        poster_url = poster_field
    if poster_url:
        ET.SubElement(item, "thumb").text = safe_text(poster_url)

    # enclosure (torrent)
    torrents = res.get(ANILIBRIA_TORRENT_FIELD) or []
    if isinstance(torrents, dict):
        torrents = list(torrents.values())
    chosen = None
    for t in torrents:
        if isinstance(t, dict):
            for k in ("url", "magnet", "link", "download"):
                if k in t and t[k]:
                    chosen = t
                    break
        elif isinstance(t, str):
            chosen = {"url": t}
        if chosen:
            break
    if chosen:
        enclosure_url = chosen.get("url") or chosen.get("magnet") or chosen.get("link") or chosen.get("download")
        if enclosure_url:
            e = ET.SubElement(item, "enclosure")
            e.set("url", safe_text(enclosure_url))
            e.set("length", str(chosen.get("size", 0)))
            e.set("type", chosen.get("mime_type", "application/x-bittorrent"))

    return item

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/torznab")
def torznab(q: Optional[str] = Query(None),
            t: Optional[str] = Query(None),
            limit: Optional[int] = Query(50),
            cat: Optional[str] = Query(None),
            season: Optional[str] = Query(None),
            ep: Optional[str] = Query(None)):

    if t == "caps":
        root = ET.Element("caps")
        server = ET.SubElement(root, "server")
        ET.SubElement(server, "title").text = "AniLibria Bridge"
        ET.SubElement(server, "version").text = "1.0"
        ET.SubElement(server, "email").text = ""
        ET.SubElement(server, "baseUrl").text = "http://anilibria-torznab:8020/torznab"

        limits = ET.SubElement(root, "limits")
        ET.SubElement(limits, "max").text = "100"
        ET.SubElement(limits, "default").text = "50"

        cats = ET.SubElement(root, "categories")
        for cid, info in CATEGORIES.items():
            ET.SubElement(cats, "category", {"id": cid, "name": info["name"]})

        return Response(content=ET.tostring(root, encoding="utf-8"), media_type="application/xml")

    query = q or ""
    channel = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(channel, "channel")
    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "link").text = ANILIBRIA_BASE
    ET.SubElement(ch, "description").text = f"Results for {query}"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    fetched = fetch_anilibria(query=query, limit=limit, category=cat, season=season, ep=ep)
    if isinstance(fetched, dict) and "__torznab_xml__" in fetched:
        return Response(content=fetched["__torznab_xml__"], media_type="application/xml")

    if not fetched:
        # добавить dummy item, чтобы Prowlarr не ругался
        fetched = [{"title": "No results", "id": "0", "description": "No items found"}]

    for res in fetched[:limit]:
        try:
            item = map_result_to_item(res)
            ch.append(item)
        except Exception:
            continue

    return Response(content=ET.tostring(channel, encoding="utf-8"), media_type="application/rss+xml")
