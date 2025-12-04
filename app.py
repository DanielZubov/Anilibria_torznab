# app.py
from fastapi import FastAPI, Query
from fastapi.responses import Response
import requests
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, List, Any

app = FastAPI()

ANILIBRIA_BASE = os.getenv("ANILIBRIA_BASE", "https://anilibria.top/api")
ANILIBRIA_SEARCH_PATH = os.getenv("ANILIBRIA_SEARCH_PATH", "/v1/titles")
ANILIBRIA_RSS_PATH = os.getenv("ANILIBRIA_RSS_PATH", "/anime/torrents/rss")  # optional proxy of site's RSS
ANILIBRIA_RELEASE_TORRENTS = os.getenv("ANILIBRIA_RELEASE_TORRENTS", "/anime/torrents/release")  # /{id}
ANILIBRIA_TORRENT_FIELD = os.getenv("ANILIBRIA_TORRENT_FIELD", "torrents")

# User requested categories reduced to only these four:
CATEGORIES = {
    "5070": {"name": "TV/Anime", "query_cat": "anime"},
    "2000": {"name": "Movies", "query_cat": "movie"},
    "5000": {"name": "TV", "query_cat": "tv"},
    "8000": {"name": "Other", "query_cat": "other"},
}

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def iso_to_rfc2822(dt_str: Optional[str]) -> str:
    """Normalize many ISO-like timestamps into RFC2822 used by RSS pubDate.
    If parsing fails fallback to now.
    """
    if not dt_str:
        return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    try:
        # support Z and timezone offsets
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        try:
            # try common alternate format
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def anilibria_get(path: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[Any]:
    """Simple GET helper: tries to parse JSON, but returns raw text (RSS) if not JSON."""
    base = ANILIBRIA_BASE.rstrip("/")
    if path.startswith("/"):
        url = f"{base}{path}"
    else:
        url = f"{base}/{path}"
    try:
        r = SESSION.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        # try json, fallback to text
        try:
            return r.json()
        except Exception:
            return r.text
    except Exception:
        return None


def fetch_anilibria(query: str, limit: int = 50, category: Optional[str] = None,
                    season: Optional[str] = None, ep: Optional[str] = None) -> List[dict]:
    """Search AniLibria for releases. Returns list of dicts or empty list."""
    base = ANILIBRIA_BASE.rstrip("/")
    path = ANILIBRIA_SEARCH_PATH.lstrip("/")
    url_path = f"/{path}"
    params = {"query": query, "limit": limit}
    # map category id to query_cat if possible
    if category:
        # category might be comma-separated (from Sonarr/Prowlarr)
        if "," in category:
            for cid in [c.strip() for c in category.split(",")]:
                if cid in CATEGORIES:
                    params["category"] = CATEGORIES[cid]["query_cat"]
                    break
        else:
            if category in CATEGORIES:
                params["category"] = CATEGORIES[category]["query_cat"]
            else:
                params["category"] = category

    if season:
        params["season"] = season
    if ep:
        params["episode"] = ep

    data = anilibria_get(url_path, params=params)
    if not data:
        return []

    # handle common response shapes
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "items", "titles"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    return []


def fetch_torrents_for_release(release_id: Any) -> List[dict]:
    """Fetch torrents for a given release id via ANILIBRIA_RELEASE_TORRENTS endpoint."""
    if not release_id:
        return []
    path = f"{ANILIBRIA_RELEASE_TORRENTS}/{release_id}"
    data = anilibria_get(path)
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "torrents" in data and isinstance(data["torrents"], list):
            return data["torrents"]
        for key in ("data", "results", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    return []


def map_result_to_item(res: dict) -> Optional[ET.Element]:
    """Map a single AniLibria search result (release) to a torznab RSS <item>.
    We try to produce an item that includes pubDate and an enclosure (torrent/magnet).
    If no torrent is found for the release we return None (Sonarr/Prowlarr expect enclosure).
    """
    # title extraction
    title_field = res.get("title") or res.get("name") or res.get("rus") or res.get("eng") or res.get("full_title")
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or next(iter(title_field.values()))
    else:
        title = title_field or "Unknown title"

    # release id / link
    release_id = res.get("id") or res.get("releaseId") or res.get("release_id")
    link = res.get("site_url") or res.get("url") or (f"https://www.anilibria.top/releases/{release_id}" if release_id else "")

    # get publication date candidate
    pub = res.get("published") or res.get("created_at") or res.get("updated_at") or res.get("fresh_at")

    # collect torrents: either present in result or fetch by release id
    torrents = []
    if isinstance(res.get(ANILIBRIA_TORRENT_FIELD), list):
        torrents = res.get(ANILIBRIA_TORRENT_FIELD)
    elif isinstance(res.get(ANILIBRIA_TORRENT_FIELD), dict):
        torrents = list(res.get(ANILIBRIA_TORRENT_FIELD).values())

    # If none present, try release-level fetch
    if not torrents and release_id:
        try:
            torrents = fetch_torrents_for_release(release_id)
        except Exception:
            torrents = []

    # normalize torrent list
    if isinstance(torrents, dict):
        torrents = list(torrents.values())

    if not torrents:
        # we can't create a usable item without an enclosure (torrent or magnet)
        return None

    # choose first torrent
    t = torrents[0] if isinstance(torrents, list) else torrents
    # t may be string or dict
    if isinstance(t, str):
        url = t
        size = 0
        seeders = None
        leechers = None
        infohash = None
    else:
        url = t.get("url") or t.get("magnet") or t.get("link") or t.get("download")
        size = t.get("size") or t.get("filesize") or 0
        seeders = t.get("seeders") or t.get("seeds") or t.get("seeder")
        leechers = t.get("leechers") or t.get("peers") or t.get("leecher")
        infohash = t.get("infoHash") or t.get("hash") or t.get("torrent_hash")

    if not url:
        return None

    item = ET.Element("item")
    ET.SubElement(item, "title").text = safe_text(title)
    if link:
        ET.SubElement(item, "link").text = safe_text(link)

    guid = ET.SubElement(item, "guid")
    # try to set a stable guid: infohash / torrent id / release id / url
    guid_val = infohash or t.get("id") if isinstance(t, dict) else None
    if not guid_val:
        guid_val = release_id or url or title
    guid.text = safe_text(guid_val)
    guid.set("isPermaLink", "false")

    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)

    # category
    cat_field = res.get("genre") or res.get("type") or res.get("kind") or "Anime"
    if isinstance(cat_field, dict):
        cat_text = cat_field.get("value") or cat_field.get("name") or next(iter(cat_field.values()))
    else:
        cat_text = cat_field
    ET.SubElement(item, "category").text = safe_text(cat_text)

    # description
    ET.SubElement(item, "description").text = safe_text(res.get("description") or res.get("short_description") or "")

    # thumb / poster
    poster_field = res.get("poster") or res.get("image") or res.get("cover")
    if isinstance(poster_field, dict):
        poster_url = poster_field.get("src") or poster_field.get("preview") or poster_field.get("thumbnail")
    else:
        poster_url = poster_field
    if poster_url:
        ET.SubElement(item, "thumb").text = safe_text(poster_url)

    # enclosure
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", safe_text(url))
    enc.set("length", str(size or 0))
    enc.set("type", "application/x-bittorrent")

    # optional seeds/peers/size elements
    if seeders is not None:
        ET.SubElement(item, "seeders").text = str(seeders)
    if leechers is not None:
        ET.SubElement(item, "leechers").text = str(leechers)
    if size:
        ET.SubElement(item, "size").text = str(size)

    # torznab attr for infohash
    if infohash:
        ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "infohash", "value": safe_text(infohash)})

    return item


@app.get("/health")
def health():
    return {"status": "ok"}


# ======================================================================
#                               TORZNAB API
# ======================================================================

@app.get("/torznab")
def torznab(q: Optional[str] = Query(None),
            t: Optional[str] = Query(None),
            limit: Optional[int] = Query(50),
            cat: Optional[str] = Query(None),
            season: Optional[str] = Query(None),
            ep: Optional[str] = Query(None),
            extended: Optional[int] = Query(0),
            offset: Optional[int] = Query(0)):
    """
    Torznab-compatible endpoint:
     - t=caps -> capabilities (categories + supported search types)
     - t=search (or tvsearch/movie-search) -> search results as RSS with <enclosure> + <pubDate>
     - t=rss -> proxy latest RSS if site exposes it, otherwise build from latest search
    """

    # ===== CAPS =====
    if t == "caps":
        caps = ET.Element("caps")
        server = ET.SubElement(caps, "server")
        ET.SubElement(server, "title").text = "AniLibria Torznab"
        ET.SubElement(server, "version").text = "1.0"
        ET.SubElement(server, "email").text = ""
        ET.SubElement(server, "link").text = ANILIBRIA_BASE

        # limits
        limits = ET.SubElement(caps, "limits")
        ET.SubElement(limits, "max").text = "500"
        ET.SubElement(limits, "default").text = "100"

        # searching support: include raw search support via 'q' param (clients use q)
        searching = ET.SubElement(caps, "searching")
        ET.SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
        ET.SubElement(searching, "tv-search", {"available": "yes", "supportedParams": "q,season,ep"})
        ET.SubElement(searching, "movie-search", {"available": "yes", "supportedParams": "q"})
        ET.SubElement(searching, "music-search", {"available": "no"})
        ET.SubElement(searching, "book-search", {"available": "no"})

        # categories
        cats = ET.SubElement(caps, "categories")
        for cid, info in CATEGORIES.items():
            ET.SubElement(cats, "category", {"id": cid, "name": info["name"]})

        return Response(content=ET.tostring(caps, encoding="utf-8"), media_type="application/xml")

    # ===== RSS proxy (if site supports rss) =====
    if t == "rss":
        rss_text = anilibria_get(ANILIBRIA_RSS_PATH)
        if rss_text and isinstance(rss_text, str) and rss_text.strip().startswith("<"):
            # return proxied RSS as-is
            return Response(content=rss_text, media_type="application/rss+xml")
        # fallback: build RSS from latest releases
        channel = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(channel, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge (latest)"
        ET.SubElement(ch, "link").text = ANILIBRIA_BASE
        ET.SubElement(ch, "description").text = "Latest torrents"
        ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        results = fetch_anilibria("", limit=limit)
        for res in results[:limit]:
            item = map_result_to_item(res)
            if item is not None:
                ch.append(item)
        return Response(content=ET.tostring(channel, encoding="utf-8"), media_type="application/rss+xml")

    # ===== Prowlarr/Sonarr validation: they sometimes call t=search with no q =====
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
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        enc = ET.SubElement(item, "enclosure")
        enc.set("url", "https://example.com/test.torrent")
        enc.set("length", "123456")
        enc.set("type", "application/x-bittorrent")
        ET.SubElement(item, "size").text = "123456"
        return Response(content=ET.tostring(test_rss, encoding="utf-8"), media_type="application/rss+xml")

    # ===== SEARCH (regular usage by Prowlarr/Sonarr/Radarr) =====
    if t in ("search", "tvsearch", "movie-search", "movie", "tv-search"):
        query = q or ""
        # Map cat id(s) to a query category if possible
        cat_param = None
        if cat:
            for cid in [c.strip() for c in cat.split(",")]:
                if cid in CATEGORIES:
                    cat_param = CATEGORIES[cid]["query_cat"]
                    break

        results = fetch_anilibria(query, limit=limit, category=cat_param, season=season, ep=ep)

        rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge"
        ET.SubElement(ch, "link").text = ANILIBRIA_BASE
        ET.SubElement(ch, "description").text = f"Results for {query}"
        ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        added = 0
        for res in results:
            item = map_result_to_item(res)
            if item is not None:
                ch.append(item)
                added += 1
            if added >= (limit or 50):
                break

        return Response(content=ET.tostring(rss, encoding="utf-8"), media_type="application/rss+xml")

    # ===== default empty feed =====
    empty = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(empty, "channel")
    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "description").text = "No results"
    return Response(content=ET.tostring(empty, encoding="utf-8"), media_type="application/rss+xml")
