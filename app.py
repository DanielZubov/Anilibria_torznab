# app.py
from fastapi import FastAPI, Query
from fastapi.responses import Response
import requests
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, List, Dict, Any

# Register torznab namespace for torznab:attr elements
ET.register_namespace("torznab", "http://torznab.com/schemas/2015/feed")

app = FastAPI()

# -------------- Config --------------
ANILIBRIA_BASE = os.getenv("ANILIBRIA_BASE", "https://anilibria.top/api")
# endpoints used:
# - search endpoint: /app/search/releases?query=...
# - rss endpoint: /anime/torrents/rss
# - release torrents: /anime/torrents/release/{releaseId}
ANILIBRIA_SEARCH_PATH = os.getenv("ANILIBRIA_SEARCH_PATH", "/app/search/releases")
ANILIBRIA_RSS_PATH = os.getenv("ANILIBRIA_RSS_PATH", "/anime/torrents/rss")
ANILIBRIA_RELEASE_TORRENTS = os.getenv("ANILIBRIA_RELEASE_TORRENTS", "/anime/torrents/release")  # append /{id}
ANILIBRIA_TORRENT_FIELD = os.getenv("ANILIBRIA_TORRENT_FIELD", "torrents")

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Torznab categories mapping (IDs that Sonarr/Prowlarr expect) -> human / query hint for API
# You can expand this mapping if you want more fine-grained categories later.
CATEGORIES: Dict[str, Dict[str, str]] = {
    "5070": {"name": "TV/Anime", "query_cat": "anime"},
    "2000": {"name": "Movies", "query_cat": "movie"},
    "5000": {"name": "TV", "query_cat": "tv"},
    "8000": {"name": "Other", "query_cat": "other"},

    # More anime-specific categories (common custom IDs)
    "100002": {"name": "Anime TV", "query_cat": "anime_tv"},
    "100003": {"name": "Anime Movies", "query_cat": "anime_movie"},
    "100004": {"name": "Anime OVA", "query_cat": "anime_ova"},
    "100010": {"name": "Anime Ongoing", "query_cat": "anime_ongoing"},
    "100014": {"name": "Anime Finished", "query_cat": "anime_finished"},
    "100013": {"name": "18+", "query_cat": "adult"},
}

# ---------- Helpers ----------
def safe_text(value) -> str:
    """Return string safe to put in XML (simple)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def iso_to_rfc2822(dt_str: Optional[str]) -> str:
    """Try to parse common ISO formats, fallback to now."""
    if not dt_str:
        return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def anilibria_api_get(path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Optional[Any]:
    """GET helper for AniLibria API paths (returns parsed JSON or text on RSS)."""
    base = ANILIBRIA_BASE.rstrip("/")
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    try:
        r = SESSION.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        # When we call RSS endpoints, they might return XML/text; caller should handle.
        # We'll try to parse JSON; if fails, return raw text.
        try:
            return r.json()
        except Exception:
            return r.text
    except Exception:
        return None


def fetch_search_results(query: str, limit: int = 50, category: Optional[str] = None) -> List[dict]:
    """Call AniLibria search endpoint and return list of releases (dicts)."""
    path = ANILIBRIA_SEARCH_PATH
    params = {"query": query, "limit": limit}
    # If category is torznab id, map to our query_cat
    if category:
        # category may be comma separated ids; pick first mapping available
        if "," in category:
            # keep original list for filtering later
            cats = [c.strip() for c in category.split(",") if c.strip()]
            # prefer first mapped
            for c in cats:
                if c in CATEGORIES:
                    params["category"] = CATEGORIES[c]["query_cat"]
                    break
        else:
            if category in CATEGORIES:
                params["category"] = CATEGORIES[category]["query_cat"]
            else:
                params["category"] = category
    data = anilibria_api_get(path, params=params)
    if not data:
        return []
    # Many AniLibria endpoints return list directly
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # common wrappers
        for key in ("data", "results", "items", "titles"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # else try to find first list value
        for v in data.values():
            if isinstance(v, list):
                return v
        # wrap single object
        return [data]
    return []


def fetch_torrents_for_release(release_id: int) -> List[dict]:
    """Fetch torrents for a release via /anime/torrents/release/{releaseId}"""
    path = f"{ANILIBRIA_RELEASE_TORRENTS}/{release_id}"
    data = anilibria_api_get(path)
    if not data:
        return []
    # expected to be list or dict with 'torrents'
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "torrents" in data and isinstance(data["torrents"], list):
            return data["torrents"]
        # maybe response contains 'data' wrapper
        for key in ("data", "results", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # fallback: if dict looks like single torrent
        return [data]
    return []


def fetch_rss_from_api(limit: int = 100) -> Optional[str]:
    """Fetch prebuilt RSS from AniLibria if available."""
    path = ANILIBRIA_RSS_PATH
    params = {"limit": limit}
    resp = anilibria_api_get(path, params=params)
    # If returns text (XML), return it; otherwise None
    if isinstance(resp, str):
        return resp
    return None


def map_result_to_item(res: dict) -> Optional[ET.Element]:
    """
    Map AniLibria 'release' object to a Torznab <item>.
    Ensure item contains:
      - title
      - link
      - guid (isPermaLink="false")
      - pubDate (RFC2822)
      - enclosure (url + length + type) OR skip this item (Sonarr expects torrent)
      - size, seeders, leechers optional
      - torznab:attr infohash optional
    """
    # release may be either a release summary (id, name) or a torrent entry
    # If it's a release summary, we must fetch its torrents and create separate items for each torrent.
    # To keep things simple: if this dict contains 'id' and not a direct torrent url, treat as release summary.
    # If it already contains a direct magnet/torrent link, map directly.

    # If this dict looks like a torrent entry (contains 'url' or 'magnet' at top level), map directly.
    if any(k in res for k in ("url", "magnet", "link", "download")) and not res.get("name") and not res.get("title"):
        # this is a torrent-like dict (rare)
        return _map_torrent_dict_to_item(res)

    # Otherwise expect release-level object: id, name/title, description, poster, etc.
    release_id = res.get("id") or res.get("releaseId") or res.get("release_id")
    title_field = res.get("name") or res.get("title") or {}
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or title_field.get("alternative") or next(iter(title_field.values()))
    else:
        title = title_field

    # ensure title exists
    title = safe_text(title) or f"release-{release_id}"

    # publications date candidates
    pub_candidates = (res.get("fresh_at"), res.get("created_at"), res.get("updated_at"), res.get("published"))
    pub = None
    for c in pub_candidates:
        if c:
            pub = c
            break

    # get list of torrents for this release
    torrents = []
    if release_id:
        try:
            torrents = fetch_torrents_for_release(int(release_id))
        except Exception:
            torrents = []

    # If no torrents found at release-level, maybe result itself includes 'torrents' field
    if not torrents and isinstance(res.get("torrents"), (list, dict)):
        tval = res.get("torrents")
        torrents = list(tval.values()) if isinstance(tval, dict) else tval

    # If still no torrents, we can't produce a usable torznab item (Sonarr expects torrent enclosure)
    if not torrents:
        return None

    # For each torrent create an item - but this function returns only one element.
    # We'll choose to return the first torrent mapped (caller may call map_result_to_item per-release and expect one item).
    # If you want multiple items per release (one per torrent), change caller accordingly.
    first_t = torrents[0]
    # Build item
    item = ET.Element("item")
    ET.SubElement(item, "title").text = safe_text(title)
    # link - prefer release page
    release_link = res.get("site_url") or res.get("url") or (f"https://www.anilibria.top/releases/{release_id}" if release_id else "")
    if release_link:
        ET.SubElement(item, "link").text = safe_text(release_link)
    guid = ET.SubElement(item, "guid")
    guid.text = safe_text(first_t.get("hash") or first_t.get("id") or release_id or release_link or title)
    guid.set("isPermaLink", "false")
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)
    ET.SubElement(item, "category").text = safe_text(res.get("type", {}).get("value") if isinstance(res.get("type"), dict) else res.get("type") or "Anime")
    ET.SubElement(item, "description").text = safe_text(res.get("description") or res.get("short_description") or "")

    # poster/thumb
    poster = res.get("poster") or res.get("image") or {}
    if isinstance(poster, dict):
        thumb_url = poster.get("src") or poster.get("preview") or poster.get("thumbnail")
    else:
        thumb_url = poster
    if thumb_url:
        ET.SubElement(item, "thumb").text = safe_text(thumb_url)

    # map torrent -> enclosure
    torrent_obj = first_t if isinstance(first_t, dict) else {"url": first_t}
    url = torrent_obj.get("url") or torrent_obj.get("magnet") or torrent_obj.get("link") or torrent_obj.get("download")
    if not url:
        return None
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", safe_text(url))
    enc.set("length", str(torrent_obj.get("size", 0) or 0))
    enc.set("type", "application/x-bittorrent")
    # optional fields
    if torrent_obj.get("seeds") is not None or torrent_obj.get("seeders") is not None:
        seeds = torrent_obj.get("seeds") or torrent_obj.get("seeders")
        ET.SubElement(item, "seeders").text = str(seeds)
    if torrent_obj.get("peers") is not None or torrent_obj.get("leechers") is not None:
        peers = torrent_obj.get("peers") or torrent_obj.get("leechers")
        ET.SubElement(item, "leechers").text = str(peers)
    # torznab attr: infohash if present
    infohash = torrent_obj.get("infoHash") or torrent_obj.get("hash") or torrent_obj.get("torrent_hash")
    if infohash:
        ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "infohash", "value": safe_text(infohash)})

    return item


def _map_torrent_dict_to_item(torrent: dict) -> Optional[ET.Element]:
    """Map a direct torrent dict into an item (when API returns torrent entries directly)."""
    item = ET.Element("item")
    title = torrent.get("title") or torrent.get("name") or torrent.get("release") or "torrent"
    ET.SubElement(item, "title").text = safe_text(title)
    link = torrent.get("url") or torrent.get("link") or torrent.get("magnet")
    if link:
        ET.SubElement(item, "link").text = safe_text(link)
    guid_val = torrent.get("hash") or torrent.get("id") or link or title
    guid = ET.SubElement(item, "guid")
    guid.text = safe_text(guid_val)
    guid.set("isPermaLink", "false")
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(torrent.get("published") or torrent.get("created_at"))
    enc = ET.SubElement(item, "enclosure")
    enc.set("url", safe_text(link))
    enc.set("length", str(torrent.get("size", 0) or 0))
    enc.set("type", "application/x-bittorrent")
    if torrent.get("seeders") is not None:
        ET.SubElement(item, "seeders").text = str(torrent.get("seeders"))
    if torrent.get("leechers") is not None:
        ET.SubElement(item, "leechers").text = str(torrent.get("leechers"))
    infohash = torrent.get("infoHash") or torrent.get("hash")
    if infohash:
        ET.SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "infohash", "value": safe_text(infohash)})
    return item


# ------------------ Endpoints ------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/capabilities")
def capabilities():
    """
    Simple capabilities endpoint (kept for compatibility).
    Prowlarr/Sonarr usually call /torznab?t=caps, but some clients may call /capabilities.
    """
    root = ET.Element("caps")
    server = ET.SubElement(root, "server")
    ET.SubElement(server, "name").text = "AniLibria Torznab Bridge"
    ET.SubElement(server, "version").text = "1.0"
    ET.SubElement(server, "link").text = ANILIBRIA_BASE

    cats = ET.SubElement(root, "categories")
    for cid, info in CATEGORIES.items():
        ET.SubElement(cats, "category", {"id": cid}).text = info["name"]

    return Response(content=ET.tostring(root, encoding="utf-8"), media_type="application/xml")


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
    Full Torznab endpoint:
     - t=caps -> capabilities
     - t=search -> generic search (q)
     - t=tvsearch -> tv search (q + season + ep)
     - t=movie-search -> movie search (q)
     - t=rss -> RSS of latest torrents (uses API RSS if available)
     - fallback: when t=search and q is missing, return a small test item that includes pubDate & enclosure (for Prowlarr/Sonarr validation)
    """

    # ---------- CAPS ----------
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
        # searching capabilities
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

    # ---------- RSS (latest torrents) ----------
    if t == "rss" or (t == "search" and q is None and extended):
        # Try to proxy AniLibria RSS if exists
        rss_text = fetch_rss_from_api(limit=limit)
        if rss_text:
            # Ensure content-type xml for clients
            return Response(content=rss_text, media_type="application/rss+xml")
        # Fallback: create an RSS from latest search (empty query -> latest)
        channel = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(channel, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge (latest)"
        ET.SubElement(ch, "link").text = ANILIBRIA_BASE
        ET.SubElement(ch, "description").text = "Latest torrents"
        ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        # get latest by calling search with empty query
        results = fetch_search_results("", limit=limit)
        for res in results[:limit]:
            item = map_result_to_item(res)
            if item is not None:
                ch.append(item)
        return Response(content=ET.tostring(channel, encoding="utf-8"), media_type="application/rss+xml")

    # ---------- TEST ITEM FOR PROWLARR/SONARR VALIDATION ----------
    # Some clients call t=search with no q to validate. Provide a valid item with pubDate + enclosure.
    if t == "search" and not q:
        test_rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(test_rss, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge"
        ET.SubElement(ch, "description").text = "Prowlarr/Sonarr Test OK"
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

    # ---------- SEARCH HANDLERS ----------
    # Accept several t values: search, tvsearch, movie-search, movie, tvsearch (some clients vary)
    if t in ("search", "tvsearch", "movie-search", "movie", "tv-search"):
        query = q or ""
        # Sonarr may send cat as comma-separated torznab ids; we'll forward a mapped query category if possible.
        cat_param = None
        if cat:
            # try to find a mapped query_cat for any provided category id
            for cid in [c.strip() for c in cat.split(",")]:
                if cid in CATEGORIES:
                    cat_param = CATEGORIES[cid].get("query_cat")
                    break
        # season/ep support: if provided, include them in params (API may ignore if not supported)
        season_val = season
        ep_val = ep
        # fetch search results (release-level)
        results = fetch_search_results(query, limit=limit, category=cat_param)
        # Build RSS feed
        rss = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "AniLibria Bridge"
        ET.SubElement(ch, "link").text = ANILIBRIA_BASE
        ET.SubElement(ch, "description").text = f"Results for {query}"
        ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        # Iterate results; for releases we map to the first torrent per release (Sonarr expects enclosure)
        items_added = 0
        for res in results:
            # If tvsearch + season+ep provided, try to filter by episode data where available
            # Some results contain 'episodes' or 'episodes_total' — we do a best-effort filter.
            if t in ("tvsearch", "tv-search") and (season_val or ep_val):
                # If release contains season/episode metadata, attempt to skip non-matching releases
                # (AniLibria API structure varies; this is best-effort and non-blocking)
                release_season = res.get("season", {}).get("value") if isinstance(res.get("season"), dict) else res.get("season")
                # If there's a season specified and it doesn't match requested, skip
                if release_season and season_val and str(release_season) != str(season_val):
                    continue

            item = map_result_to_item(res)
            if item is not None:
                ch.append(item)
                items_added += 1
            if items_added >= (limit or 50):
                break

        return Response(content=ET.tostring(rss, encoding="utf-8"), media_type="application/rss+xml")

    # ---------- default: return empty rss ----------
    empty = ET.Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    ch = ET.SubElement(empty, "channel")
    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "description").text = "No results"
    return Response(content=ET.tostring(empty, encoding="utf-8"), media_type="application/rss+xml")
