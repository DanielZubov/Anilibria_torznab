from fastapi import FastAPI, Query
from fastapi.responses import Response
import requests
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from typing import Optional, List

app = FastAPI()

ANILIBRIA_BASE = os.getenv("ANILIBRIA_BASE", "https://anilibria.top/api")
ANILIBRIA_SEARCH_PATH = os.getenv("ANILIBRIA_SEARCH_PATH", "/v1/titles")
ANILIBRIA_TORRENT_FIELD = os.getenv("ANILIBRIA_TORRENT_FIELD", "torrents")

# Final categories requested by you
CATEGORIES = {
    "5070": {"name": "TV/Anime", "query_cat": "anime"},
    "2000": {"name": "Movies", "query_cat": "movie"},
    "5000": {"name": "TV", "query_cat": "tv"},
    "8000": {"name": "Other", "query_cat": "other"},
}

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def safe_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def iso_to_rfc2822(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.utcnow()
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _xml_bytes(elem: ET.Element) -> bytes:
    """
    Return pretty-printed XML bytes with declaration.
    Uses minidom to produce a stable header that many indexers expect.
    """
    raw = ET.tostring(elem, encoding="utf-8")
    parsed = minidom.parseString(raw)
    # toprettyxml returns bytes if encoding provided
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8")
    return pretty


def fetch_anilibria(query: str, limit: int = 50, category: Optional[str] = None,
                    season: Optional[str] = None, ep: Optional[str] = None) -> List[dict]:
    """
    Query the upstream AniLibria REST endpoint and return a list of dicts.
    Attempts to map torznab category id -> backend query_cat if possible.
    """
    base = ANILIBRIA_BASE.rstrip("/")
    path = ANILIBRIA_SEARCH_PATH.lstrip("/")
    url = f"{base}/{path}"

    params = {"query": query, "limit": limit}

    # If torznab sends a category id (or comma-separated), map it to our query_cat if known
    if category:
        if "," in category:
            for cid in [c.strip() for c in category.split(",")]:
                if cid in CATEGORIES and CATEGORIES[cid].get("query_cat"):
                    params["category"] = CATEGORIES[cid]["query_cat"]
                    break
        else:
            if category in CATEGORIES and CATEGORIES[category].get("query_cat"):
                params["category"] = CATEGORIES[category]["query_cat"]
            else:
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
        # upstream failed or no JSON — return empty list (Sonarr/Prowlarr will handle empty)
        return []

    # Heuristics to locate list inside JSON
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


def map_result_to_item(res: dict) -> Optional[ET.Element]:
    """
    Map upstream JSON result -> RSS <item>.
    IMPORTANT: Sonarr/Prowlarr require either an <enclosure> or a download URL; without it the item will be ignored.
    Also require a valid pubDate.
    """
    # Build item
    item = ET.Element("item")

    # title
    title_field = res.get("title") or res.get("name") or "Unknown title"
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or next(iter(title_field.values()))
    else:
        title = title_field
    ET.SubElement(item, "title").text = safe_text(title)

    # link
    link = res.get("site_url") or res.get("url") or (f"https://www.anilibria.top/releases/{res.get('id')}" if res.get("id") else None)
    if link:
        ET.SubElement(item, "link").text = safe_text(link)

    # guid
    guid = ET.SubElement(item, "guid")
    guid.text = safe_text(res.get("id") or link or title)
    guid.set("isPermaLink", "false")

    # pubDate — MUST be present and valid for Sonarr/Prowlarr
    pub = res.get("published") or res.get("pubDate") or res.get("created_at") or datetime.utcnow().isoformat()
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)

    # description
    desc = res.get("description") or res.get("short_description") or ""
    ET.SubElement(item, "description").text = safe_text(desc)

    # category text
    cat_field = res.get("genre") or res.get("type") or res.get("category") or "Anime"
    if isinstance(cat_field, dict):
        cat_text = cat_field.get("name") or next(iter(cat_field.values()))
    else:
        cat_text = cat_field
    ET.SubElement(item, "category").text = safe_text(cat_text)

    # thumb/poster
    poster_field = res.get("poster") or res.get("image") or res.get("cover")
    poster_url = None
    if isinstance(poster_field, dict):
        poster_url = poster_field.get("src") or poster_field.get("preview") or poster_field.get("thumbnail")
    else:
        poster_url = poster_field
    if poster_url:
        ET.SubElement(item, "thumb").text = safe_text(poster_url)

    # torrents -> enclosure
    torrents = res.get(ANILIBRIA_TORRENT_FIELD) or res.get("torrents") or res.get("files") or []
    if isinstance(torrents, dict):
        # sometimes keyed by quality
        torrents = list(torrents.values())

    enclosure_url = None
    size = 0
    first_t = None

    for t in torrents:
        if isinstance(t, dict):
            for k in ("url", "magnet", "link", "download"):
                if t.get(k):
                    enclosure_url = t[k]
                    size = t.get("size", 0) or 0
                    first_t = t
                    break
        elif isinstance(t, str):
            enclosure_url = t
            first_t = None
        if enclosure_url:
            break

    # If no enclosure/download — return None (Sonarr/Prowlarr expect items to be downloadable)
    if not enclosure_url:
        return None

    e = ET.SubElement(item, "enclosure")
    e.set("url", safe_text(enclosure_url))
    e.set("length", str(size))
    e.set("type", "application/x-bittorrent")

    # optional additional fields
    if size:
        ET.SubElement(item, "size").text = str(size)

    if isinstance(first_t, dict):
        seeds = first_t.get("seeders") or first_t.get("seeds")
        peers = first_t.get("leechers") or first_t.get("peers")
        if seeds is not None:
            ET.SubElement(item, "seeders").text = str(seeds)
        if peers is not None:
            ET.SubElement(item, "leechers").text = str(peers)
        infohash = first_t.get("infoHash") or first_t.get("hash")
        if infohash:
            ET.SubElement(item, "torznab:attr", {"name": "infohash", "value": safe_text(infohash)})

    return item


@app.get("/health")
def health():
    return {"status": "ok"}


# ----------------------------------------------------------------------
# TORZNAB-compatible endpoints: /torznab?t=caps, t=search, t=tvsearch, t=movie-search, t=rss
# ----------------------------------------------------------------------
@app.get("/torznab")
def torznab(q: Optional[str] = Query(None),
            t: Optional[str] = Query(None),
            limit: Optional[int] = Query(50),
            cat: Optional[str] = Query(None),
            season: Optional[str] = Query(None),
            ep: Optional[str] = Query(None),
            offset: Optional[int] = Query(0)):

    # --------- CAPS (required by indexers like Prowlarr) ----------
    if t == "caps":
        caps = ET.Element("caps")

        server = ET.SubElement(caps, "server")
        ET.SubElement(server, "title").text = "AniLibria Torznab"
        ET.SubElement(server, "version").text = "1.0"
        ET.SubElement(server, "email").text = ""
        ET.SubElement(server, "link").text = ANILIBRIA_BASE

        limits = ET.SubElement(caps, "limits")
        ET.SubElement(limits, "default").text = "100"
        ET.SubElement(limits, "max").text = "500"

        # capabilities: searching, tv-search, movie-search, etc.
        searching = ET.SubElement(caps, "searching")
        ET.SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
        ET.SubElement(searching, "tv-search", {"available": "yes", "supportedParams": "q,season,ep"})
        ET.SubElement(searching, "movie-search", {"available": "yes", "supportedParams": "q"})
        ET.SubElement(searching, "book-search", {"available": "no"})
        ET.SubElement(searching, "music-search", {"available": "no"})

        # categories
        cats = ET.SubElement(caps, "categories")
        for cid, info in CATEGORIES.items():
            ET.SubElement(cats, "category", {"id": cid, "name": info["name"]})

        return Response(content=_xml_bytes(caps), media_type="application/xml")

    # --------- TEST SEARCH (Prowlarr/Sonarr validation) ----------
    # Prowlarr sends t=search without q to validate the indexer.
    if t == "search" and not q:
        test_rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(test_rss, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge"
        ET.SubElement(ch, "description").text = "Prowlarr Test OK"

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = "Prowlarr Test Entry"
        guid = ET.SubElement(item, "guid")
        guid.text = "test-12345"
        guid.set("isPermaLink", "false")

        # REQUIRED: pubDate and enclosure
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url", "https://example.com/test.torrent")
        enclosure.set("length", "123456")
        enclosure.set("type", "application/x-bittorrent")
        ET.SubElement(item, "size").text = "123456"

        return Response(content=_xml_bytes(test_rss), media_type="application/xml")

    # --------- RSS (latest) - optional helper for "rss" requests ----------
    if t == "rss":
        rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge (latest)"
        ET.SubElement(ch, "description").text = "Latest results"
        ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        results = fetch_anilibria("", limit, cat, season, ep)
        for res in results[:limit]:
            item = map_result_to_item(res)
            if item:
                ch.append(item)

        return Response(content=_xml_bytes(rss), media_type="application/rss+xml")

    # --------- NORMAL SEARCH (and tvsearch/movie-search) ----------
    # Support different t values commonly used by Sonarr/Prowlarr:
    # - t=search (with q)
    # - t=tvsearch
    # - t=movie
    # The implementation ignores t except where special-case above.
    rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "description").text = f"Results for {q or ''}"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    results = fetch_anilibria(q or "", limit, cat, season, ep)

    # Map and append items; only items with enclosure/download will be appended
    appended = 0
    for res in results:
        if appended >= (limit or 50):
            break
        item = map_result_to_item(res)
        if item:
            ch.append(item)
            appended += 1

    return Response(content=_xml_bytes(rss), media_type="application/rss+xml")
