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
    "2000": {"name": "Anime"},
    "2010": {"name": "Anime Raw"},
    "2020": {"name": "Anime Subbed"},
    "2030": {"name": "Anime Dubbed"},
}

USER_AGENT = os.getenv("USER_AGENT", "anilibria-torznab-bridge/1.0")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def safe_text(value) -> str:
    if isinstance(value, str):
        return value
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
        return []

    if isinstance(data, dict):
        for key in ("data", "results", "items", "titles"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]

    if isinstance(data, list):
        return data

    return []


def map_result_to_item(res: dict) -> Optional[ET.Element]:
    item = ET.Element("item")

    title_field = res.get("title") or res.get("name") or "Unknown title"
    if isinstance(title_field, dict):
        title = title_field.get("english") or title_field.get("main") or next(iter(title_field.values()))
    else:
        title = title_field
    ET.SubElement(item, "title").text = safe_text(title)

    link = res.get("site_url") or f"https://www.anilibria.top/releases/{res.get('id')}"
    ET.SubElement(item, "link").text = safe_text(link)

    guid = ET.SubElement(item, "guid")
    guid.text = safe_text(res.get("id") or link)
    guid.set("isPermaLink", "false")

    pub = res.get("published") or datetime.utcnow().isoformat()
    ET.SubElement(item, "pubDate").text = iso_to_rfc2822(pub)

    desc = res.get("description") or ""
    ET.SubElement(item, "description").text = safe_text(desc)

    cat_field = res.get("genre") or "Anime"
    cat_text = cat_field.get("name") if isinstance(cat_field, dict) else cat_field
    ET.SubElement(item, "category").text = safe_text(cat_text)

    poster_field = res.get("poster")
    poster_url = None
    if isinstance(poster_field, dict):
        poster_url = poster_field.get("src") or poster_field.get("preview") or poster_field.get("thumbnail")
    else:
        poster_url = poster_field

    if poster_url:
        ET.SubElement(item, "thumb").text = safe_text(poster_url)

    torrents = res.get(ANILIBRIA_TORRENT_FIELD) or []

    if isinstance(torrents, dict):
        torrents = list(torrents.values())

    enclosure_url = None
    size = 0

    for t in torrents:
        if isinstance(t, dict):
            for k in ("url", "magnet", "link", "download"):
                if t.get(k):
                    enclosure_url = t[k]
                    size = t.get("size", 0)
                    break
        elif isinstance(t, str):
            enclosure_url = t

        if enclosure_url:
            break

    if not enclosure_url:
        return None

    e = ET.SubElement(item, "enclosure")
    e.set("url", safe_text(enclosure_url))
    e.set("length", str(size))
    e.set("type", "application/x-bittorrent")

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
            ep: Optional[str] = Query(None)):

    # ====== TORZNAB CAPS ======
    if t == "caps":
        caps = ET.Element("caps")

        server = ET.SubElement(caps, "server", {
            "version": "1.0",
            "title": "AniLibria Torznab"
        })

        ET.SubElement(caps, "limits", {
            "default": "100",
            "max": "500"
        })

        searching = ET.SubElement(caps, "searching")
        ET.SubElement(searching, "search", {
            "available": "yes",
            "supportedParams": "q"
        })
        ET.SubElement(searching, "tv-search", {
            "available": "yes",
            "supportedParams": "q,season,ep"
        })
        ET.SubElement(searching, "movie-search", {
            "available": "yes",
            "supportedParams": "q"
        })

        cats = ET.SubElement(caps, "categories")
        for cid, info in CATEGORIES.items():
            ET.SubElement(cats, "category", {"id": cid, "name": info["name"]})

        return Response(
            content=ET.tostring(caps, encoding="utf-8"),
            media_type="application/xml"
        )

    # ===== TEST REQUEST (used by Prowlarr) =====
    if t == "search" and not q:
        test_rss = ET.Element("rss", {
            "version": "2.0",
            "xmlns:torznab": "http://torznab.com/schemas/2015/feed"
        })
        ch = ET.SubElement(test_rss, "channel")

        ET.SubElement(ch, "title").text = "AniLibria Bridge"
        ET.SubElement(ch, "description").text = "Prowlarr Test OK"

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = "Prowlarr Test Entry"

        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = "test-12345"

        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url", "https://example.com/test.torrent")
        enclosure.set("length", "123456")
        enclosure.set("type", "application/x-bittorrent")

        # 🔥 ВАЖНО: ЭТО НУЖНО PROWLARR
        ET.SubElement(item, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

        return Response(
            content=ET.tostring(test_rss, encoding="utf-8"),
            media_type="application/xml"
        )

    # ===== NORMAL SEARCH =====
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:torznab": "http://torznab.com/schemas/2015/feed"
    })
    ch = ET.SubElement(rss, "channel")

    ET.SubElement(ch, "title").text = "AniLibria Bridge"
    ET.SubElement(ch, "description").text = f"Results for {q or ''}"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    results = fetch_anilibria(q or "", limit, cat, season, ep)

    for res in results[:limit]:
        item = map_result_to_item(res)
        if item:
            ch.append(item)

    return Response(
        content=ET.tostring(rss, encoding="utf-8"),
        media_type="application/rss+xml"
    )
