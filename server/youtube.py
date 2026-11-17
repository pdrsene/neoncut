"""
NEONCUT — vídeos e canais recomendados do YouTube (sem chave de API).

Usa os feeds RSS públicos do YouTube:
  - vídeos recentes:  /feeds/videos.xml?channel_id=UC...
  - Shorts do canal:  /feeds/videos.xml?playlist_id=UUSH...   (cortes verticais prontos)
O RSS traz título, data, miniatura e número de visualizações de cada vídeo.

A lista de canais fica no banco (tabela site_kv) e o admin edita pela tela "Minha conta".
Aceita @handle, link do canal ou o ID (UC...).
"""
from __future__ import annotations

import calendar
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

try:
    from . import accounts
except ImportError:  # executado como script
    import accounts  # type: ignore

router = APIRouter()

DEFAULT_CHANNELS = [
    "UCdlJx-w37viXFNq__qZ7ARQ",  # Paulinho o LOKO
    "@lipaogamer",               # LipaoGamer
]
CACHE_SECONDS = 30 * 60
META_DAYS = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}

accounts.db().execute("CREATE TABLE IF NOT EXISTS site_kv(k TEXT PRIMARY KEY, v TEXT, updated REAL)")


def kv_get(k: str, default=None):
    r = accounts.db().execute("SELECT v FROM site_kv WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except Exception:
        return default


def kv_set(k: str, v):
    accounts.db().execute("INSERT INTO site_kv(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                          (k, json.dumps(v), time.time()))


def _get(url: str, timeout: int = 15) -> str:
    r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9",
                                             "Cookie": "CONSENT=YES+1; SOCS=CAI"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def channels() -> list[str]:
    lst = kv_get("yt_channels")
    return lst if isinstance(lst, list) and lst else list(DEFAULT_CHANNELS)


def _page_url(entry: str) -> str:
    e = entry.strip()
    if re.fullmatch(r"UC[\w-]{22}", e):
        return "https://www.youtube.com/channel/" + e
    if e.startswith("http"):
        return e.split("?")[0]
    return "https://www.youtube.com/@" + e.lstrip("@")


def channel_meta(entry: str) -> dict | None:
    """Descobre ID, nome e foto do canal (fica em cache no banco por alguns dias)."""
    key = "ytmeta:" + entry.strip().lower()
    hit = kv_get(key)
    if hit and time.time() - hit.get("t", 0) < META_DAYS * 86400:
        return hit if hit.get("id") else None
    meta = {"t": time.time(), "entry": entry}
    m = re.search(r"(UC[\w-]{22})", entry) if "/channel/" in entry or entry.startswith("UC") else None
    if m:
        meta["id"] = m.group(1)
    try:
        html = _get(_page_url(entry))
        if "id" not in meta:
            mm = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
                  or re.search(r'"externalId":"(UC[\w-]{22})"', html)
                  or re.search(r'<meta itemprop="identifier" content="(UC[\w-]{22})"', html))
            if mm:
                meta["id"] = mm.group(1)
        t = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        h = re.search(r'"canonicalBaseUrl":"/(@[^"]+)"', html)
        if t:
            meta["name"] = _unescape(t.group(1))
        if img:
            meta["avatar"] = img.group(1)
        if h:
            meta["handle"] = urllib.parse.unquote(h.group(1))
    except Exception:
        if hit:  # sem internet agora: usa o que já sabia
            return hit if hit.get("id") else None
        meta["t"] = time.time() - META_DAYS * 86400 + 3600  # tenta de novo em 1 hora
    kv_set(key, meta)
    return meta if meta.get("id") else None


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
             .replace("&lt;", "<").replace("&gt;", ">"))


def _feed(query: str) -> list[dict]:
    xml = _get("https://www.youtube.com/feeds/videos.xml?" + query)
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if not vid:
            continue
        g = e.find("media:group", NS)
        views = 0
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        if g is not None:
            st = g.find("media:community/media:statistics", NS)
            if st is not None:
                views = int(st.get("views") or 0)
            th = g.find("media:thumbnail", NS)
            if th is not None and th.get("url"):
                thumb = th.get("url")
        out.append({
            "id": vid,
            "title": e.findtext("a:title", "", NS),
            "published": e.findtext("a:published", "", NS),
            "author": e.findtext("a:author/a:name", "", NS),
            "views": views,
            "thumbnail": thumb,
            "url": "https://www.youtube.com/watch?v=" + vid,
        })
    return out


def _ts(iso: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return 0.0


def build() -> dict:
    chans, videos, shorts = [], [], []
    for entry in channels():
        meta = channel_meta(entry)
        if not meta:
            continue
        cid = meta["id"]
        ch = {"id": cid, "name": meta.get("name", ""), "avatar": meta.get("avatar", ""),
              "handle": meta.get("handle", ""), "url": "https://www.youtube.com/channel/" + cid}
        try:
            sh = _feed("playlist_id=UUSH" + cid[2:])
        except Exception:
            sh = []
        short_ids = {s["id"] for s in sh}
        try:
            vs = [v for v in _feed("channel_id=" + cid) if v["id"] not in short_ids]
        except Exception:
            vs = []
        name = ch["name"] or (vs[0]["author"] if vs else sh[0]["author"] if sh else "")
        ch["name"] = name
        for v in vs:
            v.update(channel=name, channel_id=cid, avatar=ch["avatar"])
        for s in sh:
            s.update(channel=name, channel_id=cid, avatar=ch["avatar"], url="https://www.youtube.com/shorts/" + s["id"])
        ch["latest"] = vs[0]["published"] if vs else ""
        chans.append(ch)
        videos += vs[:6]
        shorts += sh
    videos.sort(key=lambda v: v["published"], reverse=True)
    now = time.time()
    recent = [s for s in shorts if now - _ts(s["published"]) < 21 * 86400] or shorts
    recent.sort(key=lambda s: s["views"], reverse=True)
    return {"channels": chans, "videos": videos[:12], "shorts": recent[:12], "updated": now}


@router.get("/api/yt/feed")
def feed():
    with _lock:
        hit = _cache.get("feed")
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        data = build()
    except Exception:
        if hit:
            return hit[1]
        return {"channels": [], "videos": [], "shorts": [], "error": "Não consegui carregar o YouTube agora."}
    if data["videos"] or data["shorts"] or not hit:
        stamp = time.time() if (data["videos"] or data["shorts"]) else time.time() - CACHE_SECONDS + 300
        with _lock:
            _cache["feed"] = (stamp, data)
    return data


class ChannelsReq(BaseModel):
    channels: list[str]


@router.get("/api/admin/yt-channels")
def admin_get(req: Request):
    accounts.require_admin(req)
    out = []
    for e in channels():
        m = kv_get("ytmeta:" + e.strip().lower()) or {}
        out.append({"entry": e, "ok": bool(m.get("id")), "name": m.get("name", ""), "checked": "t" in m})
    return {"channels": out}


@router.post("/api/admin/yt-channels")
def admin_set(body: ChannelsReq, req: Request):
    accounts.require_admin(req)
    clean = []
    for e in body.channels:
        e = e.strip()
        if not e:
            continue
        if not (re.fullmatch(r"@?[\w.\-]{2,60}", e) or re.fullmatch(r"UC[\w-]{22}", e)
                or re.match(r"https://(www\.|m\.)?youtube\.com/", e)):
            raise HTTPException(400, f"Não entendi o canal: {e}")
        if e not in clean:
            clean.append(e[:200])
    kv_set("yt_channels", clean[:30])
    with _lock:
        _cache.pop("feed", None)
    for e in clean:  # força checar de novo
        accounts.db().execute("DELETE FROM site_kv WHERE k=?", ("ytmeta:" + e.lower(),))
    data = feed()
    return {"ok": True, "found": len(data.get("channels", [])), "total": len(clean)}
