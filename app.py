# app.py
from fastapi import FastAPI, Query
from fastapi.responses import Response
import requests
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from typing import Optional, List

app = FastAPI()

# Configuration via env
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
    """Преобразует любое значение в строку, безопасную для XML"""
    if isinstance(value, str):
        return value
    elif isinstance(value, (int, float)):
        return str(value)
    return str(value)


def iso_to_rfc2822(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            dt = datetime.utcnow()
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def fetch_anilibria(query: str, limit: int = 50, category: Optional[str] = None,
                    season: Optional[str] = None, ep: Optional[str] = None) -> List[dict]:
    base = ANILIBRIA_BASE.rstrip("/")
    path = ANILIBRIA_SEARCH_PATH.lstrip("/")
    url = f"{base}/{path}"

    params_name = os.getenv("ANILIBRIA_QUERY_PARAM", "query")
    params = {params_name: query, "limit": limit}
    if category:
        params["category"] = category
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
    title_field = res.get("title") or res.get("name") or res.get("rus") or res.get("eng") or res.get("full_title")
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or next(iter(title_field.values()))
    else:
        title = title_field
    ET.SubElement(item, "title").text = safe_text(title) or "Unknown title"

    # link
    link = res.get("site_url") or res.get("url") or (f"https://www.anilibria.top/releases/{res.get('id')}" if res.get("id") else None)
    if link:
        ET.SubElement(item, "link").text = safe_text(link)

    # guid
    guid = res.get("id") or res.get("guid") or res.get("hash") or link or title
    g = ET.SubElement(item, "guid")
    g.text = safe_text(guid)
    g.set("isPermaLink", "false")

    # pubDate
    pub = res.get("published") or res.get("created_at") or res.get("date") or res.get("pubDate")
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub) if pub else datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # category
    cat_field = res.get("genre") or res.get("type") or "Anime"
    if isinstance(cat_field, dict):
        cat_text = cat_field.get("value") or cat_field.get("name") or next(iter(cat_field.values()))
    else:
        cat_text = cat_field
    ET.SubElement(item, "category").text = safe_text(cat_text)

    # description
    desc = res.get("description") or res.get("short_description") or res.get("announce") or ""
    ET.SubElement(item, "description").text = safe_text(desc)

    # thumb / poster
    poster_field = res.get("poster") or res.get("image") or res.get("cover")
    if isinstance(poster_field, dict):
        poster_url = poster_field.get("src") or poster_field.get("preview") or poster_field.get("thumbnail")
    else:
        poster_url = poster_field
    if poster_url:
        ET.SubElement(item, "thumb").text = safe_text(poster_url)

    # enclosure (torrent)
    torrents = res.get(ANILIBRIA_TORRENT_FIELD) or res.get("torrents") or res.get("files") or []
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
            e.set("type", chosen.get("mime_type", "application/x-bittorrent") if isinstance(chosen, dict) else "application/x-bittorrent")

    # seeders, leechers, infohash
    size = chosen.get("size") if chosen else None
    seeders = chosen.get("seeders") if chosen else None
    leechers = chosen.get("leechers") if chosen else None
    infohash = chosen.get("infoHash") if chosen else None
    if infohash:
        ET.SubElement(item, "torznab:attr", {"name": "infohash", "value": safe_text(infohash)})
    if seeders is not None:
        ET.SubElement(item, "seeders").text = str(seeders)
    if leechers is not None:
        ET.SubElement(item, "leechers").text = str(leechers)
    if size:
        ET.SubElement(item, "size").text = str(size)

    return item


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/capabilities")
def capabilities():
    root = ET.Element("caps")
    server = ET.SubElement(root, "server")
    ET.SubElement(server, "name").text = "AniLibria Torznab Bridge"
    ET.SubElement(server, "version").text = "1.0"
    ET.SubElement(server, "email").text = ""
    ET.SubElement(server, "link").text = ANILIBRIA_BASE

    categories = ET.SubElement(root, "categories")
    for cid, info in CATEGORIES.items():
        c = ET.SubElement(categories, "category", {"id": cid})
        c.text = info["name"]

    xml = ET.tostring(root, encoding="utf-8")
    return Response(content=xml, media_type="application/xml")


@app.get("/torznab")
def torznab(q: Optional[str] = Query(None), t: Optional[str] = Query(None), limit: Optional[int] = Query(50),
            cat: Optional[str] = Query(None), season: Optional[str] = Query(None),
            ep: Optional[str] = Query(None), offset: Optional[int] = Query(0)):

    query = q or t or ""
    channel = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(channel, "channel")
    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "link").text = ANILIBRIA_BASE
    ET.SubElement(ch, "description").text = f"Results for {query}"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    fetched = fetch_anilibria(query=query, limit=limit, category=cat, season=season, ep=ep)
    if isinstance(fetched, dict) and "__torznab_xml__" in fetched:
        return Response(content=fetched["__torznab_xml__"], media_type="application/xml")

    for res in fetched[:limit]:
        try:
            item = map_result_to_item(res)
            ch.append(item)
        except Exception:
            continue

    xml_bytes = ET.tostring(channel, encoding="utf-8")
    return Response(content=xml_bytes, media_type="application/rss+xml")
