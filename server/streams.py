"""
NEONCUT — streams recomendadas (lives ao vivo da Twitch).

Usa a API oficial da Twitch (Helix) com um app próprio:
crie em dev.twitch.tv/console e coloque TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET no Railway.
Sem essas variáveis a seção some do site.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter

TWITCH_ID = os.environ.get("TWITCH_CLIENT_ID", "").strip()
TWITCH_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
LANG = os.environ.get("STREAMS_LANGUAGE", "pt")
ID_BASE = os.environ.get("TWITCH_ID_BASE", "https://id.twitch.tv").rstrip("/")
API_BASE = os.environ.get("TWITCH_API_BASE", "https://api.twitch.tv").rstrip("/")
CACHE_SECONDS = 120
GTA_NAMES = ["Grand Theft Auto VI", "Grand Theft Auto V"]

router = APIRouter()
_lock = threading.Lock()
_token = {"value": "", "exp": 0.0}
_cache: dict[str, tuple[float, dict]] = {}
_game_ids: dict[str, str | None] = {}


def _req(url: str, method: str = "GET", headers: dict | None = None, data: bytes | None = None):
    r = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read().decode() or "{}")


def _app_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 300:
        return _token["value"]
    q = urllib.parse.urlencode({"client_id": TWITCH_ID, "client_secret": TWITCH_SECRET, "grant_type": "client_credentials"})
    t = _req(ID_BASE + "/oauth2/token?" + q, "POST", data=b"")
    _token.update(value=t["access_token"], exp=time.time() + int(t.get("expires_in", 3600)))
    return _token["value"]


def _helix(path: str, params: list[tuple[str, str]]):
    url = API_BASE + "/helix/" + path + "?" + urllib.parse.urlencode(params)
    return _req(url, headers={"Client-Id": TWITCH_ID, "Authorization": "Bearer " + _app_token()})


def _game_id(name: str) -> str | None:
    if name not in _game_ids:
        data = _helix("games", [("name", name)]).get("data") or []
        _game_ids[name] = data[0]["id"] if data else None
    return _game_ids[name]


def _fetch(cat: str) -> dict:
    params: list[tuple[str, str]] = [("first", "24"), ("language", LANG), ("type", "live")]
    if cat == "gta":
        ids = [g for g in (_game_id(n) for n in GTA_NAMES) if g]
        params += [("game_id", g) for g in ids]
    streams = _helix("streams", params).get("data") or []
    streams.sort(key=lambda s: s.get("viewer_count", 0), reverse=True)
    streams = streams[:12]
    avatars = {}
    if streams:
        users = _helix("users", [("id", s["user_id"]) for s in streams]).get("data") or []
        avatars = {u["id"]: u.get("profile_image_url", "") for u in users}
    out = []
    for s in streams:
        out.append({
            "platform": "twitch",
            "login": s["user_login"],
            "name": s["user_name"],
            "title": s.get("title", ""),
            "game": s.get("game_name", ""),
            "viewers": s.get("viewer_count", 0),
            "started_at": s.get("started_at", ""),
            "language": s.get("language", ""),
            "tags": (s.get("tags") or [])[:2],
            "thumbnail": (s.get("thumbnail_url") or "").replace("{width}", "440").replace("{height}", "248"),
            "avatar": avatars.get(s["user_id"], ""),
            "url": "https://www.twitch.tv/" + s["user_login"],
        })
    return {"enabled": True, "category": cat, "streams": out, "updated": time.time()}


@router.get("/api/streams")
def recommended(cat: str = "gta"):
    if not (TWITCH_ID and TWITCH_SECRET):
        return {"enabled": False, "streams": []}
    cat = cat if cat in ("gta", "all") else "gta"
    with _lock:
        hit = _cache.get(cat)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        data = _fetch(cat)
        if cat == "gta" and not data["streams"]:  # sem live de GTA agora: mostra as mais vistas
            data = _fetch("all")
            data["category"] = "all"
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError):
        if hit:
            return hit[1]
        return {"enabled": True, "streams": [], "error": "Não consegui carregar as lives agora."}
    with _lock:
        _cache[cat] = (time.time(), data)
    return data
