# app.py
from fastapi import FastAPI, Request, Query
from fastapi.responses import Response, PlainTextResponse
import requests
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from typing import Optional, List

app = FastAPI()

# Configuration via env
ANILIBRIA_BASE = os.getenv("ANILIBRIA_BASE", "https://anilibria.top/api")
# Default search path patterns (you can override to match real API)
# Try a few common possibilities; change via ANILIBRIA_SEARCH_PATH
ANILIBRIA_SEARCH_PATH = os.getenv("ANILIBRIA_SEARCH_PATH", "/v1/titles")  # default guess
ANILIBRIA_TORRENT_FIELD = os.getenv("ANILIBRIA_TORRENT_FIELD", "torrents")  # field containing torrent entries

# Categories mapping (Torznab numeric -> human)
# Prowlarr commonly uses 5070 for anime; you can extend this mapping
CATEGORIES = {
    "5070": {"name": "Anime", "query_cat": "anime"},
    "5000": {"name": "TV", "query_cat": "tv"},
    "5030": {"name": "AnimeOther", "query_cat": "anime_other"}
}

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

def prettify_xml(elem: ET.Element) -> str:
    raw = ET.tostring(elem, encoding="utf-8")
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent="  ", encoding="utf-8")

def build_caps_xml() -> bytes:
    rss = ET.Element("caps")
    server = ET.SubElement(rss, "server")
    ET.SubElement(server, "title").text = "AniLibria Torznab Bridge"
    ET.SubElement(server, "version").text = "1.0"
    ET.SubElement(server, "email").text = ""
    ET.SubElement(server, "location").text = ANILIBRIA_BASE

    catmap = ET.SubElement(rss, "categories")
    for catid, info in CATEGORIES.items():
        c = ET.SubElement(catmap, "category", {"id": catid})
        c.text = info["name"]

    caps = ET.Element("caps")
    return ET.tostring(rss, encoding="utf-8")

def iso_to_rfc2822(dt_str: str) -> str:
    # try parse common ISO formats, fallback to now
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            dt = datetime.utcnow()
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

def fetch_anilibria(query: str, limit: int = 50, category: Optional[str] = None, season: Optional[str] = None, ep: Optional[str] = None) -> List[dict]:
    """
    Fetch results from AniLibria REST API.
    This function is intentionally permissive: many API shapes exist, so the URL & query params are configurable.
    If your endpoint differs, change ANILIBRIA_SEARCH_PATH to the correct path (for example: /v1/search or /v1/titles).
    """
    base = ANILIBRIA_BASE.rstrip("/")
    path = ANILIBRIA_SEARCH_PATH.lstrip("/")
    url = f"{base}/{path}"

    params = {}
    # Common query parameter names tried; override by setting ANILIBRIA_*
    # Many APIs use 'query' or 'q'
    params_name = os.getenv("ANILIBRIA_QUERY_PARAM", os.getenv("QUERY_PARAM", "query"))
    params[params_name] = query
    params["limit"] = limit

    # If category mapping provided, map to API category param name
    if category:
        # try typical API param names in order
        for pname in ("category", "cat", "genre", "type"):
            # only set if not already present
            if pname not in params:
                params[pname] = category
                break

    # season/ep if passed
    if season:
        params["season"] = season
    if ep:
        params["episode"] = ep
        # some APIs use 'ep' or 'ep_num' etc.

    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        # Try fallback: maybe v3 torznab exists
        # Note: this fallback will NOT parse JSON but attempt to use a Torznab endpoint if available.
        fallback = os.getenv("ANILIBRIA_FALLBACK_V3", "https://api.anilibria.tv/v3/search/torznab")
        try:
            r = SESSION.get(fallback, params={"q": query, "limit": limit}, timeout=10)
            r.raise_for_status()
            # return a wrapper that indicates this is already torznab xml (we won't parse JSON in this flow)
            return {"__torznab_xml__": r.text}
        except Exception:
            return []

    # Heuristic: if API returns dict with 'data' or 'results' list
    if isinstance(data, dict):
        for key in ("data", "results", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # If dict is already a list-like mapping of titles
        # fallback: if top-level looks like list via some wrapper
        # try to find list by scanning values
        for v in data.values():
            if isinstance(v, list):
                return v
        # otherwise maybe it's already a list-like structure in 'titles'
        if "titles" in data and isinstance(data["titles"], list):
            return data["titles"]
        # if API returns single object for the query — wrap in list
        return [data]
    elif isinstance(data, list):
        return data
    else:
        return []

def map_result_to_item(res: dict) -> ET.Element:
    """
    Map one AniLibria JSON result to an RSS <item>.
    This mapping is best-effort; adjust fields according to actual API response.
    """
    item = ET.Element("item")
    title = res.get("name") or res.get("title") or res.get("rus") or res.get("eng") or res.get("full_title") or res.get("title_native")
    ET.SubElement(item, "title").text = title or "Unknown title"

    # link / details
    link = None
    if res.get("site_url"):
        link = res.get("site_url")
    elif res.get("url"):
        link = res.get("url")
    elif res.get("id"):
        link = f"https://www.anilibria.top/releases/{res.get('id')}"
    if link:
        ET.SubElement(item, "link").text = link

    # guid
    guid = res.get("id") or res.get("guid") or res.get("hash") or link or title
    g = ET.SubElement(item, "guid")
    g.text = str(guid)
    g.set("isPermaLink", "false")

    # pubDate
    pub = res.get("published") or res.get("created_at") or res.get("date") or res.get("pubDate")
    if pub:
        ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)
    else:
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # enclosure — find torrent/magnet
    # many APIs store a 'torrents' array or 'files'
    torrents = res.get(ANILIBRIA_TORRENT_FIELD) or res.get("torrents") or res.get("files") or []
    if isinstance(torrents, dict):
        # sometimes dict keyed by quality
        torrents = list(torrents.values())
    # pick first torrent-like item
    chosen = None
    for t in torrents:
        if isinstance(t, dict):
            # common fields: "url", "magnet", "link", "download"
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
            e.set("url", enclosure_url)
            # size/type may be unknown; set length=0 and type
            e.set("length", str(chosen.get("size", 0)))
            e.set("type", chosen.get("mime_type", "application/x-bittorrent") if isinstance(chosen, dict) else "application/x-bittorrent")

    # category
    cat_text = res.get("genre") or res.get("type") or "Anime"
    ET.SubElement(item, "category").text = cat_text

    # description
    desc = res.get("description") or res.get("short_description") or res.get("announce") or ""
    if desc:
        d = ET.SubElement(item, "description")
        d.text = desc

    # torznab:attr elements
    # size, seeders, leechers, infoHash (if present)
    size = None
    seeders = None
    leechers = None
    infohash = None
    # try to extract from chosen torrent
    if chosen and isinstance(chosen, dict):
        size = chosen.get("size") or chosen.get("filesize") or chosen.get("length")
        seeders = chosen.get("seeders") or chosen.get("seeds")
        leechers = chosen.get("leechers") or chosen.get("peers")
        infohash = chosen.get("infoHash") or chosen.get("hash")
    # sometimes top-level fields
    size = size or res.get("size")
    seeders = seeders or res.get("seeders")
    leechers = leechers or res.get("leechers")
    infohash = infohash or res.get("info_hash") or res.get("infohash")

    if size:
        s = ET.SubElement(item, "size")
        s.text = str(size)
    if seeders is not None:
        sd = ET.SubElement(item, "seeders")
        sd.text = str(seeders)
    if leechers is not None:
        ld = ET.SubElement(item, "leechers")
        ld.text = str(leechers)

    # torznab attr (info hash)
    if infohash:
        attr = ET.SubElement(item, "torznab:attr", {"name": "infohash", "value": str(infohash)})

    # poster if present
    poster = res.get("poster") or res.get("image") or res.get("cover")
    if poster:
        thumb = ET.SubElement(item, "thumb")
        thumb.text = poster

    return item

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/capabilities")
def capabilities():
    # Return a simple Torznab caps XML
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
def torznab(q: Optional[str] = Query(None), t: Optional[str] = Query(None), limit: Optional[int] = Query(50), cat: Optional[str] = Query(None),
            season: Optional[str] = Query(None), ep: Optional[str] = Query(None), offset: Optional[int] = Query(0)):
    """
    Main Torznab endpoint. Accepts typical torznab params:
      q (or 't'), cat, limit, season, ep, offset
    """
    query = q or t or ""
    # If no query, return empty feed
    channel = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(channel, "channel")
    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "link").text = ANILIBRIA_BASE
    ET.SubElement(ch, "description").text = f"Results for {query}"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Try fetch
    fetched = fetch_anilibria(query=query, limit=limit, category=cat, season=season, ep=ep)
    # If fallback returned xml already, pass it directly
    if isinstance(fetched, dict) and "__torznab_xml__" in fetched:
        return Response(content=fetched["__torznab_xml__"], media_type="application/xml")

    if not fetched:
        # empty feed
        return Response(content=ET.tostring(channel, encoding="utf-8"), media_type="application/rss+xml")

    # fetched is a list of results (dicts)
    added = 0
    for res in fetched:
        try:
            item = map_result_to_item(res)
            ch.append(item)
            added += 1
            if added >= limit:
                break
        except Exception:
            continue

    xml_bytes = ET.tostring(channel, encoding="utf-8")
    return Response(content=xml_bytes, media_type="application/rss+xml")
