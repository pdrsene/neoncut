"""
NEONCUT — contas, planos, cotas, projetos e pagamento (Mercado Pago).

Tudo em SQLite (arquivo único). No Railway, monte um Volume em /data
para o banco não ser apagado a cada deploy.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- config
DB_DIR = Path(os.environ.get("DB_DIR", "/data" if Path("/data").is_dir() else "/tmp/neoncut-db"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "neoncut.db"

def _f(name, default):
    return float(os.environ.get(name, default))


INTRO_MONTHS = int(os.environ.get("INTRO_MONTHS", "1"))          # meses com preço promocional
# preço promocional (primeiros meses) e preço normal de cada plano
PRICES = {
    "pro": (_f("PRO_INTRO_PRICE", "12.90"), _f("PRO_PRICE", "19.90")),
    "proplus": (_f("PROPLUS_INTRO_PRICE", "29.90"), _f("PROPLUS_PRICE", "29.90")),
}
HD_FREE_USES = int(os.environ.get("HD_FREE_USES", "10"))         # exportações 1080p/mês no Grátis e no Pro
# limites de cada plano: vídeos/mês, minutos por vídeo, minutos de live
LIMITS = {
    "free": (int(os.environ.get("FREE_PROJECTS", "3")), _f("FREE_MAX_MINUTES", "30"), _f("FREE_LIVE_MINUTES", "10")),
    "pro": (int(os.environ.get("PRO_PROJECTS", "100")), _f("PRO_MAX_MINUTES", "90"), _f("PRO_LIVE_MINUTES", "30")),
    "proplus": (int(os.environ.get("PROPLUS_PROJECTS", "250")), _f("PROPLUS_MAX_MINUTES", "120"), _f("PROPLUS_LIVE_MINUTES", "60")),
}
PLAN_NAMES = {"free": "Grátis", "pro": "Pro", "proplus": "Pro+"}
# opções de corte de cada plano: máx. de cortes por vídeo, duração máx. de cada corte (s), modelos de legenda
OPTIONS = {
    "free": {"max_clips": 5, "max_duration": 45, "models": ["none", "tiny", "base"]},
    "pro": {"max_clips": 12, "max_duration": 60, "models": ["none", "tiny", "base"]},
    "proplus": {"max_clips": 20, "max_duration": 120, "models": ["none", "tiny", "base", "small"]},
}
HD_USES = {"free": 0, "pro": HD_FREE_USES}  # exportações 1080p por mês (Pro+ é ilimitado)

MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()
MP_API = os.environ.get("MP_API_BASE", "https://api.mercadopago.com").rstrip("/")
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ADMINS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
BRAND = os.environ.get("BRAND_NAME", "NEONCUT")

SESSION_DAYS = 30
COOKIE = "nc_session"


def _secret() -> bytes:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode()
    f = DB_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


SECRET = _secret()

# ---------------------------------------------------------------- db
_local = threading.local()


def db() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init_db():
    db().executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      name TEXT DEFAULT '',
      pw TEXT,
      google_sub TEXT,
      avatar TEXT DEFAULT '',
      pro_until REAL DEFAULT 0,
      settings TEXT DEFAULT '{}',
      created REAL NOT NULL,
      last_seen REAL
    );
    CREATE TABLE IF NOT EXISTS projects(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created REAL NOT NULL,
      source TEXT, kind TEXT, title TEXT, duration REAL,
      meta TEXT DEFAULT '{}', clips TEXT DEFAULT '[]', charged INTEGER DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS ix_proj_user ON projects(user_id, created);
    CREATE TABLE IF NOT EXISTS payments(
      id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, amount REAL, status TEXT, created REAL
    );
    CREATE TABLE IF NOT EXISTS hd_uses(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reviews(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL, name TEXT, handle TEXT,
      plan TEXT, rating INTEGER, text TEXT, status TEXT DEFAULT 'pendente', created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tickets(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, kind TEXT,
      subject TEXT, message TEXT, page TEXT, status TEXT DEFAULT 'aberto', created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
      vid TEXT, uid INTEGER, ref TEXT, meta TEXT DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts, kind);
    CREATE INDEX IF NOT EXISTS ix_ev_vid ON events(vid, kind, ts);
    CREATE TABLE IF NOT EXISTS feedback(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, uid INTEGER, email TEXT,
      rating INTEGER, comment TEXT, plan TEXT, ref TEXT
    );
    """)
    cols = [r[1] for r in db().execute("PRAGMA table_info(users)").fetchall()]
    if "plan" not in cols:
        db().execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'pro'")
    if "ref" not in cols:
        db().execute("ALTER TABLE users ADD COLUMN ref TEXT DEFAULT ''")
    if "first_free" not in cols:
        db().execute("ALTER TABLE users ADD COLUMN first_free REAL DEFAULT 0")
    pcols = [r[1] for r in db().execute("PRAGMA table_info(payments)").fetchall()]
    if "reported" not in pcols:
        db().execute("ALTER TABLE payments ADD COLUMN reported INTEGER DEFAULT 0")


init_db()

# ---------------------------------------------------------------- helpers
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_pw(pw: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    _, s, h = stored.split("$")
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(s), 240_000)
    return hmac.compare_digest(calc, base64.b64decode(h))


def sign(data: str) -> str:
    sig = hmac.new(SECRET, data.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{data}.{sig}"


def unsign(token: str | None, max_age: float) -> str | None:
    if not token or "." not in token:
        return None
    data, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sign(data).rsplit(".", 1)[1], sig):
        return None
    try:
        _, ts = data.rsplit(":", 1)
        if time.time() - float(ts) > max_age:
            return None
    except ValueError:
        return None
    return data


def base_url(req: Request) -> str:
    if BASE_URL:
        return BASE_URL
    proto = req.headers.get("x-forwarded-proto", req.url.scheme)
    host = req.headers.get("x-forwarded-host", req.headers.get("host", req.url.netloc))
    return f"{proto}://{host}"


def set_session(resp: Response, req: Request, uid: int):
    secure = base_url(req).startswith("https")
    resp.set_cookie(COOKIE, sign(f"{uid}:{time.time()}"), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=secure, path="/")


def month_start() -> float:
    t = time.gmtime()
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def plan_of(u) -> str:
    if u["email"].lower() in ADMINS:
        return "proplus"
    if (u["pro_until"] or 0) > time.time():
        return u["plan"] if u["plan"] in ("pro", "proplus") else "pro"
    return "free"


def is_pro(u) -> bool:
    return plan_of(u) != "free"


def paid_months(uid: int, plan: str) -> int:
    return db().execute("SELECT COUNT(*) FROM payments WHERE user_id=? AND plan=? AND status='approved'",
                        (uid, plan)).fetchone()[0]


def price_for(uid: int, plan: str) -> float:
    intro, regular = PRICES[plan]
    return intro if paid_months(uid, plan) < INTRO_MONTHS else regular


def hd_used(uid: int) -> int:
    return db().execute("SELECT COUNT(*) FROM hd_uses WHERE user_id=? AND created>=?", (uid, month_start())).fetchone()[0]


def plan_info(u) -> dict:
    plan = plan_of(u)
    used = db().execute("SELECT COUNT(*) FROM projects WHERE user_id=? AND created>=? AND charged=1",
                        (u["id"], month_start())).fetchone()[0]
    limit, max_min, live_min = LIMITS[plan]
    return {
        "plan": plan,
        "plan_name": PLAN_NAMES[plan],
        "pro_until": u["pro_until"] or 0,
        "used": used,
        "limit": limit,
        "max_minutes": max_min,
        "max_live_minutes": live_min,
        "watermark": plan == "free",
        "hd_unlimited": plan == "proplus",
        "hd_left": None if plan == "proplus" else max(0, HD_USES[plan] - hd_used(u["id"])),
        "hd_total": None if plan == "proplus" else HD_USES[plan],
        "options": OPTIONS[plan],
        "all_templates": plan != "free",
        "next_price": {"pro": price_for(u["id"], "pro"), "proplus": price_for(u["id"], "proplus")},
        "intro_left": {k: max(0, INTRO_MONTHS - paid_months(u["id"], k)) for k in PRICES},
        "first_free": bool(FIRST_FREE and plan == "free" and not (u["first_free"] if "first_free" in u.keys() else 0)),
    }


def user_public(u) -> dict:
    try:
        settings = json.loads(u["settings"] or "{}")
    except Exception:
        settings = {}
    return {"id": u["id"], "email": u["email"], "name": u["name"] or u["email"].split("@")[0],
            "avatar": u["avatar"], "admin": u["email"].lower() in ADMINS, "settings": settings,
            "buyer": is_buyer(u["id"]), **plan_info(u)}


def is_buyer(uid: int) -> bool:
    return bool(db().execute("SELECT 1 FROM payments WHERE user_id=? AND status='approved' LIMIT 1", (uid,)).fetchone())


def current_user(req: Request, required: bool = True):
    data = unsign(req.cookies.get(COOKIE), SESSION_DAYS * 86400)
    u = None
    if data:
        uid = int(data.split(":")[0])
        u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u and required:
        raise HTTPException(401, "Entre na sua conta para continuar.")
    if u:
        db().execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]))
    return u


_fails: dict[str, list[float]] = {}


def throttle(key: str, limit: int = 8, window: int = 600):
    now = time.time()
    arr = [t for t in _fails.get(key, []) if now - t < window]
    if len(arr) >= limit:
        raise HTTPException(429, "Muitas tentativas. Espere alguns minutos.")
    arr.append(now)
    _fails[key] = arr


def http_json(url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None, form: bool = False):
    body = None
    h = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = {"message": str(e)}
        raise HTTPException(502, f"Serviço externo respondeu erro: {detail.get('message') or detail}")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPException(502, "Não consegui falar com o serviço externo agora. Tente de novo em instantes.")


router = APIRouter()


# ---------------------------------------------------------------- config pública
# YouTube em manutenção: o IP do servidor é bloqueado pelo YouTube. Volta sozinho se configurar
# cookies (YTDLP_COOKIES / YTDLP_COOKIES_TEXT) ou proxy (YTDLP_PROXY), ou com YT_ENABLED=1.
YT_ENABLED = os.environ.get("YT_ENABLED", "") == "1" or bool(os.environ.get("YTDLP_COOKIES") or os.environ.get("YTDLP_COOKIES_TEXT") or os.environ.get("YTDLP_PROXY"))
GA4_ID = os.environ.get("GA4_ID", "").strip()                  # ex.: G-XXXXXXX
GADS_ID = os.environ.get("GADS_ID", "").strip()                # ex.: AW-123456789
GADS_PURCHASE_LABEL = os.environ.get("GADS_PURCHASE_LABEL", "").strip()
GADS_DOWNLOAD_LABEL = os.environ.get("GADS_DOWNLOAD_LABEL", "").strip()
FIRST_FREE = os.environ.get("FIRST_FREE", "1") == "1"         # 1º corte grátis em 1080p e sem marca d'água
YT_MAINT_MSG = 'O YouTube está em manutenção no NEONCUT. Por enquanto use um link da Twitch ou da Kick, ou baixe o vídeo e envie pela aba "Enviar arquivo".'


@router.get("/api/config")
def public_config():
    return {"brand": BRAND, "google": bool(GOOGLE_ID and GOOGLE_SECRET), "payments": bool(MP_TOKEN), "youtube": YT_ENABLED,
            "intro_months": INTRO_MONTHS, "first_free": FIRST_FREE,
            "analytics": {"ga4": GA4_ID, "gads": GADS_ID, "gads_purchase": GADS_PURCHASE_LABEL, "gads_download": GADS_DOWNLOAD_LABEL},
            "hd_free_uses": HD_FREE_USES, "support_email": SUPPORT_EMAIL,
            "prices": {k: {"intro": v[0], "regular": v[1]} for k, v in PRICES.items()},
            "limits": {k: {"projects": v[0], "max_minutes": v[1], "live_minutes": v[2], **OPTIONS[k],
                           "hd": None if k == "proplus" else HD_USES[k]} for k, v in LIMITS.items()}}


# ---------------------------------------------------------------- auth
class SignupReq(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginReq(BaseModel):
    email: str
    password: str


def _valid_email(e: str) -> str:
    e = (e or "").strip().lower()
    if "@" not in e or "." not in e.split("@")[-1] or len(e) > 200:
        raise HTTPException(400, "E-mail inválido.")
    return e


def record_creator_invite(req: Request, uid: int):
    code = req.cookies.get("nc_invite", "")[:40]
    if not code:
        return
    c = db()
    # The creator module creates these tables at app startup.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='creator_profiles'").fetchone():
        return
    p = c.execute("SELECT uid FROM creator_profiles WHERE token=?", (code,)).fetchone()
    if p and p["uid"] != uid:
        c.execute("INSERT OR IGNORE INTO creator_referrals(invited,inviter,created) VALUES(?,?,?)",
                  (uid,p["uid"],time.time()))


@router.post("/api/auth/signup")
def signup(body: SignupReq, req: Request, resp: Response):
    throttle("signup:" + (req.client.host if req.client else "?"), 10, 3600)
    email = _valid_email(body.email)
    if len(body.password) < 6:
        raise HTTPException(400, "A senha precisa ter pelo menos 6 caracteres.")
    if db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise HTTPException(409, "Já existe uma conta com esse e-mail. Faça login.")
    cur = db().execute("INSERT INTO users(email,name,pw,created,last_seen) VALUES(?,?,?,?,?)",
                       (email, body.name.strip()[:60], hash_pw(body.password), time.time(), time.time()))
    record_creator_invite(req, cur.lastrowid)
    set_session(resp, req, cur.lastrowid)
    log_event("sign_up", uid=cur.lastrowid, meta={"via": "email"})
    return user_public(db().execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())


@router.post("/api/auth/login")
def login(body: LoginReq, req: Request, resp: Response):
    email = _valid_email(body.email)
    throttle("login:" + email)
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_pw(body.password, u["pw"]):
        if u and not u["pw"] and u["google_sub"]:
            raise HTTPException(401, "Essa conta usa login com Google.")
        raise HTTPException(401, "E-mail ou senha incorretos.")
    set_session(resp, req, u["id"])
    return user_public(u)


@router.post("/api/auth/logout")
def logout(resp: Response):
    resp.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def me(req: Request):
    u = current_user(req, required=False)
    return {"user": user_public(u) if u else None}


class PwReq(BaseModel):
    current: str = ""
    new: str


@router.post("/api/me/password")
def change_pw(body: PwReq, req: Request):
    u = current_user(req)
    if u["pw"] and not check_pw(body.current, u["pw"]):
        raise HTTPException(400, "Senha atual incorreta.")
    if len(body.new) < 6:
        raise HTTPException(400, "A nova senha precisa ter pelo menos 6 caracteres.")
    db().execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(body.new), u["id"]))
    return {"ok": True}


class SettingsReq(BaseModel):
    settings: dict


@router.post("/api/me/settings")
def save_settings(body: SettingsReq, req: Request):
    u = current_user(req)
    s = json.dumps(body.settings)[:4000]
    db().execute("UPDATE users SET settings=? WHERE id=?", (s, u["id"]))
    return {"ok": True}


# --- Google
@router.get("/api/auth/google")
def google_start(req: Request):
    if not (GOOGLE_ID and GOOGLE_SECRET):
        raise HTTPException(400, "Login com Google ainda não foi configurado.")
    state = sign(f"{secrets.token_hex(8)}:{time.time()}")
    q = urllib.parse.urlencode({
        "client_id": GOOGLE_ID, "redirect_uri": base_url(req) + "/api/auth/google/callback",
        "response_type": "code", "scope": "openid email profile", "state": state, "prompt": "select_account"})
    r = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q)
    r.set_cookie("nc_oauth", state, max_age=600, httponly=True, samesite="lax", path="/")
    return r


@router.get("/api/auth/google/callback")
def google_cb(req: Request, code: str = "", state: str = ""):
    if not state or state != req.cookies.get("nc_oauth") or not unsign(state, 600):
        return RedirectResponse("/?erro=login#entrar")
    tok = http_json("https://oauth2.googleapis.com/token", "POST", {
        "code": code, "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
        "redirect_uri": base_url(req) + "/api/auth/google/callback", "grant_type": "authorization_code"}, form=True)
    info = http_json("https://openidconnect.googleapis.com/v1/userinfo",
                     headers={"Authorization": "Bearer " + tok.get("access_token", "")})
    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified", True):
        return RedirectResponse("/?erro=google#entrar")
    u = db().execute("SELECT * FROM users WHERE google_sub=? OR email=?", (info.get("sub"), email)).fetchone()
    if u:
        db().execute("UPDATE users SET google_sub=?, avatar=?, name=COALESCE(NULLIF(name,''),?) WHERE id=?",
                     (info.get("sub"), info.get("picture", ""), info.get("name", ""), u["id"]))
        uid = u["id"]
    else:
        uid = db().execute("INSERT INTO users(email,name,google_sub,avatar,created,last_seen) VALUES(?,?,?,?,?,?)",
                           (email, info.get("name", ""), info.get("sub"), info.get("picture", ""),
                            time.time(), time.time())).lastrowid
        record_creator_invite(req, uid)
        log_event("sign_up", uid=uid, meta={"via": "google"})
        u = None
    r = RedirectResponse("/#criar" if u else "/?cadastro=google#criar")
    set_session(r, req, uid)
    r.delete_cookie("nc_oauth", path="/")
    return r


# ---------------------------------------------------------------- projetos e cota
class ProjectReq(BaseModel):
    source: str = ""
    kind: str = "file"          # file | link | live
    title: str = ""
    minutes: float = 0
    reuse_id: int | None = None


@router.post("/api/projects")
def start_project(body: ProjectReq, req: Request):
    u = current_user(req)
    p = plan_info(u)
    if body.reuse_id:
        old = db().execute("SELECT * FROM projects WHERE id=? AND user_id=?", (body.reuse_id, u["id"])).fetchone()
        if old and time.time() - old["created"] < 7 * 86400:
            return {"id": old["id"], "plan": p, "charged": False}
    limit_min = p["max_live_minutes"] if body.kind == "live" else p["max_minutes"]
    if body.minutes > limit_min + 0.5:
        raise HTTPException(402, f"No seu plano o limite é {int(limit_min)} minutos por vídeo. "
                                 + ("Escolha um trecho menor." if p["plan"] == "proplus" else "Faça upgrade para vídeos maiores."))
    if p["used"] >= p["limit"]:
        raise HTTPException(402, f"Você usou os {p['limit']} vídeos do mês no plano "
                                 + (p["plan_name"] + ". A cota renova no dia 1." + (" No Pro+ são mais vídeos por mês." if p["plan"] == "pro" else "") if p["plan"] != "free" else "Grátis. Assine o Pro para continuar."))
    pid = db().execute("INSERT INTO projects(user_id,created,source,kind,title,duration) VALUES(?,?,?,?,?,?)",
                       (u["id"], time.time(), body.source[:500], body.kind, body.title[:200], body.minutes * 60)).lastrowid
    return {"id": pid, "plan": plan_info(u), "charged": True}


class ProjectUpdate(BaseModel):
    title: str | None = None
    duration: float | None = None
    clips: list | None = None
    meta: dict | None = None


@router.put("/api/projects/{pid}")
def update_project(pid: int, body: ProjectUpdate, req: Request):
    u = current_user(req)
    row = db().execute("SELECT id FROM projects WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Projeto não encontrado.")
    if body.title is not None:
        db().execute("UPDATE projects SET title=? WHERE id=?", (body.title[:200], pid))
    if body.duration is not None:
        db().execute("UPDATE projects SET duration=? WHERE id=?", (body.duration, pid))
    if body.clips is not None:
        db().execute("UPDATE projects SET clips=? WHERE id=?", (json.dumps(body.clips)[:800000], pid))
    if body.meta is not None:
        db().execute("UPDATE projects SET meta=? WHERE id=?", (json.dumps(body.meta)[:20000], pid))
    return {"ok": True}


@router.get("/api/projects")
def list_projects(req: Request):
    u = current_user(req)
    rows = db().execute("SELECT * FROM projects WHERE user_id=? ORDER BY created DESC LIMIT 60", (u["id"],)).fetchall()
    return [{"id": r["id"], "created": r["created"], "source": r["source"], "kind": r["kind"], "title": r["title"],
             "duration": r["duration"], "meta": json.loads(r["meta"] or "{}"), "clips": json.loads(r["clips"] or "[]")}
            for r in rows]


@router.delete("/api/projects/{pid}")
def delete_project(pid: int, req: Request):
    u = current_user(req)
    # o registro some da lista, mas continua contando na cota do mês
    db().execute("UPDATE projects SET clips='[]', meta='{\"hidden\":true}' WHERE id=? AND user_id=?", (pid, u["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- 1080p
@router.post("/api/usage/hd")
def use_hd(req: Request):
    """Gasta 1 exportação em 1080p (ilimitado no Pro+)."""
    u = current_user(req)
    p = plan_info(u)
    if p["hd_unlimited"]:
        return {"ok": True, "hd_left": None}
    if p["plan"] == "free":
        raise HTTPException(402, "Exportar em 1080p é do Pro e do Pro+. No Pro+ é ilimitado.")
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        used = hd_used(u["id"])
        if used >= HD_USES["pro"]:
            raise HTTPException(402, f"Você já usou as {HD_USES['pro']} exportações em 1080p deste mês. "
                                     "No Pro+ o 1080p é ilimitado.")
        c.execute("INSERT INTO hd_uses(user_id, created) VALUES(?,?)", (u["id"], time.time()))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {"ok": True, "hd_left": HD_USES["pro"] - used - 1}


@router.post("/api/usage/first")
def use_first_free(req: Request):
    """Gasta o 1º corte grátis (1080p e sem marca d'água). Só uma vez por conta."""
    u = current_user(req)
    if not FIRST_FREE or plan_of(u) != "free":
        raise HTTPException(400, "Oferta indisponível para esta conta.")
    cur = db().execute("UPDATE users SET first_free=? WHERE id=? AND COALESCE(first_free,0)=0", (time.time(), u["id"]))
    if cur.rowcount != 1:
        raise HTTPException(402, "Você já usou o seu 1º corte grátis em 1080p. No Pro os cortes saem sem marca d'água.")
    log_event("first_free_used", uid=u["id"])
    return {"ok": True}


# ---------------------------------------------------------------- pagamento (Mercado Pago)
PLAN_DAYS = 30
_PAY_LOCK = threading.Lock()


class CheckoutReq(BaseModel):
    plan: str = "pro"


@router.post("/api/billing/checkout")
def checkout(body: CheckoutReq, req: Request):
    u = current_user(req)
    if body.plan not in PRICES:
        raise HTTPException(400, "Plano inválido.")
    if not MP_TOKEN:
        raise HTTPException(503, "Pagamentos ainda não foram ativados neste site.")
    price = price_for(u["id"], body.plan)
    log_event("begin_checkout", uid=u["id"], ref=u["ref"] if "ref" in u.keys() else "", meta={"plan": body.plan, "price": price})
    base = base_url(req)
    pref = http_json(MP_API + "/checkout/preferences", "POST", {
        "items": [{"id": body.plan, "title": f"{BRAND} {PLAN_NAMES[body.plan]} (30 dias)", "quantity": 1,
                   "unit_price": round(price, 2), "currency_id": "BRL"}],
        "payer": {"email": u["email"]},
        "external_reference": f"{u['id']}:{body.plan}:{int(round(price * 100))}",
        "back_urls": {"success": base + "/api/billing/return", "pending": base + "/api/billing/return",
                      "failure": base + "/api/billing/return"},
        "auto_return": "approved",
        "notification_url": base + "/api/billing/webhook",
        "statement_descriptor": BRAND[:13],
    }, headers={"Authorization": "Bearer " + MP_TOKEN})
    return {"url": pref.get("init_point")}


def apply_payment(payment_id: str) -> str:
    """Consulta o pagamento no Mercado Pago e libera o Pro se aprovado. Idempotente."""
    if not MP_TOKEN or not payment_id or not str(payment_id).isdigit():
        return "invalid"
    p = http_json(f"{MP_API}/v1/payments/{payment_id}",
                  headers={"Authorization": "Bearer " + MP_TOKEN})
    status = p.get("status", "")
    ref = p.get("external_reference") or ""
    try:
        parts = ref.split(":")
        uid, plan = int(parts[0]), parts[1]
        expected = int(parts[2]) / 100 if len(parts) > 2 else None
    except (ValueError, IndexError):
        return "invalid"
    if plan in ("month", "year"):  # compras feitas na versão antiga
        plan = "pro"
    if plan not in PRICES:
        return "invalid"
    try:
        amount = float(p.get("transaction_amount") or 0)
    except (TypeError, ValueError):
        return "invalid"
    if not math.isfinite(amount) or amount <= 0 or str(p.get("id")) != str(payment_id) or p.get("currency_id") != "BRL":
        return "invalid"
    if not db().execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return "invalid"
    c = db()
    with _PAY_LOCK:  # retorno do checkout e webhook podem chegar juntos: só um libera os 30 dias
        c.execute("BEGIN IMMEDIATE")
        try:
            exists = c.execute("SELECT status FROM payments WHERE id=?", (str(payment_id),)).fetchone()
            if exists and exists["status"] == "approved":
                c.execute("COMMIT")
                return "approved"
            if status == "approved":
                floor = expected if expected is not None else min(PRICES[plan])
                if not math.isfinite(floor) or floor <= 0 or amount + 0.01 < floor:
                    status = "amount_mismatch"   # não conta como venda nem libera o plano
            c.execute("INSERT OR REPLACE INTO payments(id,user_id,plan,amount,status,created,reported) VALUES(?,?,?,?,?,?,0)",
                      (str(payment_id), uid, plan, amount, status, time.time()))
            if status == "approved":
                u = c.execute("SELECT pro_until, ref FROM users WHERE id=?", (uid,)).fetchone()
                if u:
                    start = max(time.time(), u["pro_until"] or 0)
                    c.execute("UPDATE users SET pro_until=?, plan=? WHERE id=?", (start + PLAN_DAYS * 86400, plan, uid))
                log_event("purchase", uid=uid, ref=(u["ref"] if u else "") or "",
                          meta={"transaction_id": str(payment_id), "value": round(amount, 2), "currency": "BRL", "plan": plan,
                                "method": p.get("payment_type_id") or ""})
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return status


@router.get("/api/billing/return")
def billing_return(payment_id: str = "", collection_id: str = "", status: str = ""):
    st = "pending"
    try:
        st = apply_payment(payment_id or collection_id) if (payment_id or collection_id) else (status or "failure")
    except HTTPException:
        st = "pending"
    return RedirectResponse(f"/?pagamento={urllib.parse.quote(st)}#conta")


@router.get("/api/billing/conversions")
def billing_conversions(req: Request):
    u = current_user(req, required=False)
    if not u:
        return {"purchases": []}
    rows = db().execute("SELECT id, plan, amount FROM payments WHERE user_id=? AND status='approved' AND COALESCE(reported,0)=0",
                        (u["id"],)).fetchall()
    out = []
    for r in rows:
        if db().execute("UPDATE payments SET reported=1 WHERE id=? AND COALESCE(reported,0)=0", (r["id"],)).rowcount == 1:
            out.append({"transaction_id": r["id"], "value": round(r["amount"] or 0, 2), "currency": "BRL", "plan": r["plan"]})
    return {"purchases": out}


@router.post("/api/billing/webhook")
async def billing_webhook(req: Request):
    pid = req.query_params.get("data.id") or req.query_params.get("id")
    try:
        body = await req.json()
        pid = (body.get("data") or {}).get("id") or pid
        if body.get("type") not in (None, "payment") and not req.query_params.get("data.id"):
            return {"ok": True}
    except Exception:
        pass
    if pid:
        try:
            apply_payment(str(pid))
        except HTTPException:
            # Return retryable status instead of acknowledging a failed upstream lookup.
            raise HTTPException(503, "Confirmação temporariamente indisponível.")
    return {"ok": True}


# ---------------------------------------------------------------- tickets (suporte)
TICKET_KINDS = {"bug": "Bug", "duvida": "Dúvida", "ideia": "Ideia", "outro": "Outro"}


class TicketReq(BaseModel):
    kind: str = "duvida"
    subject: str = ""
    message: str
    email: str = ""
    page: str = ""


@router.post("/api/tickets")
def create_ticket(body: TicketReq, req: Request):
    throttle("ticket:" + (req.client.host if req.client else "?"), 12, 3600)
    u = current_user(req, required=False)
    msg = (body.message or "").strip()
    if len(msg) < 5:
        raise HTTPException(400, "Escreva um pouco mais na mensagem.")
    email = (u["email"] if u else body.email).strip().lower()
    if not u:
        if "@" not in email or "." not in email.split("@")[-1]:
            raise HTTPException(400, "Informe um e-mail para resposta.")
    kind = body.kind if body.kind in TICKET_KINDS else "outro"
    db().execute("INSERT INTO tickets(user_id,email,kind,subject,message,page,created) VALUES(?,?,?,?,?,?,?)",
                 (u["id"] if u else None, email[:200], kind, body.subject.strip()[:160], msg[:4000], body.page[:200], time.time()))
    return {"ok": True}


# ---------------------------------------------------------------- admin
def require_admin(req: Request):
    u = current_user(req)
    if u["email"].lower() not in ADMINS:
        raise HTTPException(403, "Só administradores.")
    return u


@router.get("/api/admin/stats")
def admin_stats(req: Request):
    require_admin(req)
    now = time.time()
    q = lambda sql, *a: db().execute(sql, a).fetchone()[0]
    users = db().execute("SELECT * FROM users ORDER BY created DESC LIMIT 200").fetchall()
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "pro": q("SELECT COUNT(*) FROM users WHERE pro_until>? AND COALESCE(plan,'pro')='pro'", now),
        "proplus": q("SELECT COUNT(*) FROM users WHERE pro_until>? AND plan='proplus'", now),
        "projects_month": q("SELECT COUNT(*) FROM projects WHERE created>=?", month_start()),
        "revenue_month": q("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created>=?", month_start()),
        "tickets_open": q("SELECT COUNT(*) FROM tickets WHERE status='aberto'"),
        "list": [{"email": u["email"], "name": u["name"], "created": u["created"], "pro_until": u["pro_until"],
                  "plan": u["plan"], "last_seen": u["last_seen"]} for u in users],
    }


@router.get("/api/admin/tickets")
def admin_tickets(req: Request):
    require_admin(req)
    rows = db().execute("SELECT * FROM tickets ORDER BY (status='aberto') DESC, created DESC LIMIT 200").fetchall()
    return [{"id": r["id"], "email": r["email"], "kind": r["kind"], "subject": r["subject"],
             "message": r["message"], "page": r["page"], "status": r["status"], "created": r["created"]} for r in rows]


class TicketStatus(BaseModel):
    id: int
    status: str


@router.post("/api/admin/tickets/status")
def admin_ticket_status(body: TicketStatus, req: Request):
    require_admin(req)
    st = body.status if body.status in ("aberto", "resolvido") else "aberto"
    db().execute("UPDATE tickets SET status=? WHERE id=?", (st, body.id))
    return {"ok": True}


class GrantReq(BaseModel):
    email: str
    days: int
    plan: str = "pro"


@router.post("/api/admin/grant")
def admin_grant(body: GrantReq, req: Request):
    require_admin(req)
    u = db().execute("SELECT * FROM users WHERE email=?", (body.email.strip().lower(),)).fetchone()
    if not u:
        raise HTTPException(404, "Usuário não encontrado.")
    if body.days <= 0:
        db().execute("UPDATE users SET pro_until=0 WHERE id=?", (u["id"],))
    else:
        plan = body.plan if body.plan in PRICES else "pro"
        start = max(time.time(), u["pro_until"] or 0)
        db().execute("UPDATE users SET pro_until=?, plan=? WHERE id=?", (start + body.days * 86400, plan, u["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- avaliações (só de quem pagou de verdade)
class ReviewReq(BaseModel):
    rating: int = 5
    text: str
    handle: str = ""


@router.get("/api/reviews")
def public_reviews():
    rows = db().execute("""SELECT r.*, u.avatar FROM reviews r JOIN users u ON u.id=r.user_id
                           WHERE r.status='aprovado' ORDER BY r.created DESC LIMIT 24""").fetchall()
    buyers = db().execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status='approved'").fetchone()[0]
    return {"buyers": buyers, "reviews": [{"name": r["name"], "handle": r["handle"], "plan": r["plan"], "rating": r["rating"],
                                           "text": r["text"], "avatar": r["avatar"] or "", "created": r["created"]} for r in rows]}


@router.get("/api/reviews/mine")
def my_review(req: Request):
    u = current_user(req)
    r = db().execute("SELECT * FROM reviews WHERE user_id=?", (u["id"],)).fetchone()
    return {"buyer": is_buyer(u["id"]), "review": dict(r) if r else None}


@router.post("/api/reviews")
def post_review(body: ReviewReq, req: Request):
    u = current_user(req)
    pay = db().execute("SELECT plan FROM payments WHERE user_id=? AND status='approved' ORDER BY created DESC LIMIT 1", (u["id"],)).fetchone()
    if not pay:
        raise HTTPException(403, "Só quem já assinou pode avaliar. Assim todas as avaliações são de compras reais.")
    text = body.text.strip()
    if len(text) < 10:
        raise HTTPException(400, "Escreva pelo menos uma frase.")
    name = (u["name"] or u["email"].split("@")[0]).strip()[:60]
    db().execute("""INSERT INTO reviews(user_id,name,handle,plan,rating,text,status,created) VALUES(?,?,?,?,?,?,'pendente',?)
                    ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, handle=excluded.handle, plan=excluded.plan,
                    rating=excluded.rating, text=excluded.text, status='pendente', created=excluded.created""",
                 (u["id"], name, body.handle.strip()[:40], pay["plan"], max(1, min(5, body.rating)), text[:600], time.time()))
    return {"ok": True}


@router.get("/api/admin/reviews")
def admin_reviews(req: Request):
    require_admin(req)
    rows = db().execute("""SELECT r.*, u.email FROM reviews r JOIN users u ON u.id=r.user_id
                           ORDER BY (r.status='pendente') DESC, r.created DESC LIMIT 200""").fetchall()
    return [dict(r) for r in rows]


class ReviewStatus(BaseModel):
    id: int
    status: str


@router.post("/api/admin/reviews/status")
def admin_review_status(body: ReviewStatus, req: Request):
    require_admin(req)
    st = body.status if body.status in ("pendente", "aprovado", "oculto") else "pendente"
    db().execute("UPDATE reviews SET status=? WHERE id=?", (st, body.id))
    return {"ok": True}


# ---------------------------------------------------------------- métricas
TRACK_KINDS = {"visit", "link", "clip_generated", "export", "upgrade_open", "signup_open", "feedback_shown",
               # funil completo (kit de crescimento)
               "page_view", "demo_started", "source_selected", "video_uploaded", "generation_started", "generation_failed",
               "generation_completed", "signup_started", "export_started", "export_failed", "download_completed",
               "survey_shown", "inapp_open_browser", "js_error"}
SAFE_META = {"n", "dur", "mode", "guest", "tpl", "why", "path", "login", "src", "kind", "code", "stage", "ms", "msg", "q",
             "demo", "first_free", "share", "focus", "quick", "size_mb", "cat", "plan", "utm_source", "utm_medium",
             "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "ttclid", "lp", "vw", "id"}


def ua_info(ua: str) -> dict:
    """Aparelho, sistema e navegador a partir do User-Agent (sem guardar o User-Agent inteiro)."""
    u = ua or ""
    low = u.lower()
    os_ = ("ios" if re.search(r"iphone|ipad|ipod", low) else "android" if "android" in low else "windows" if "windows" in low
           else "mac" if "mac os" in low else "linux" if "linux" in low else "outro")
    if "instagram" in low:
        br = "instagram"
    elif re.search(r"musical_ly|bytedancewebview|tiktok|trill", low):
        br = "tiktok"
    elif re.search(r"fban|fbav|fb_iab", low):
        br = "facebook"
    elif "edg/" in low or "edga/" in low or "edgios/" in low:
        br = "edge"
    elif "samsungbrowser" in low:
        br = "samsung"
    elif "firefox" in low or "fxios" in low:
        br = "firefox"
    elif "opr/" in low or "opera" in low:
        br = "opera"
    elif "crios" in low or "chrome/" in low:
        br = "chrome"
    elif "safari" in low:
        br = "safari"
    else:
        br = "outro"
    dev = "mobile" if (os_ in ("ios", "android") or "mobi" in low) else "desktop"
    if "ipad" in low or ("android" in low and "mobile" not in low):
        dev = "tablet"
    return {"dev": dev, "os": os_, "br": br, "inapp": br in ("instagram", "tiktok", "facebook")}


def clean_meta(meta: dict) -> dict:
    out = {}
    for k, v in (meta or {}).items():
        if k not in SAFE_META:
            continue
        if isinstance(v, (int, float, bool)):
            out[k] = v
        elif isinstance(v, str):
            v = re.sub(r"https?://\S+", "[url]", v)                   # nunca guarda URLs (podem ser privadas)
            v = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", v)      # nem e-mails
            out[k] = v[:120]
    return out


def _clean_ref(r: str) -> str:
    r = (r or "").strip().lower()[:40]
    return "".join(ch for ch in r if ch.isalnum() or ch in "-_.") or ""


def log_event(kind: str, vid: str = "", uid=None, ref: str = "", meta: dict | None = None):
    try:
        db().execute("INSERT INTO events(ts,kind,vid,uid,ref,meta) VALUES(?,?,?,?,?,?)",
                     (time.time(), kind, (vid or "")[:40], uid, _clean_ref(ref), json.dumps(meta or {})[:800]))
    except Exception:
        pass


class TrackReq(BaseModel):
    kind: str
    vid: str = ""
    ref: str = ""
    first_ref: str = ""
    meta: dict = {}


@router.post("/api/track")
def track(body: TrackReq, req: Request):
    if body.kind not in TRACK_KINDS:
        return {"ok": False}
    throttle("track:" + (req.client.host if req.client else "?"), 240, 3600)
    u = current_user(req, required=False)
    uid = u["id"] if u else None
    vid = "".join(ch for ch in body.vid if ch.isalnum())[:40]
    # origem do usuário = primeira origem em que o visitante chegou (para saber de onde vêm as compras)
    if u and not (u["ref"] if "ref" in u.keys() else ""):
        fr = _clean_ref(body.first_ref or body.ref)
        if fr:
            db().execute("UPDATE users SET ref=? WHERE id=?", (fr, uid))
    # visita e clique no link: no máximo 1 por visitante a cada 30 min (recarregar a página não infla)
    if body.kind in ("visit", "link") and vid:
        last = db().execute("SELECT ts FROM events WHERE vid=? AND kind=? ORDER BY ts DESC LIMIT 1", (vid, body.kind)).fetchone()
        if last and time.time() - last["ts"] < 1800:
            return {"ok": True}
    if body.kind in ("demo_started", "signup_started", "survey_shown", "inapp_open_browser") and vid:
        last = db().execute("SELECT ts FROM events WHERE vid=? AND kind=? ORDER BY ts DESC LIMIT 1", (vid, body.kind)).fetchone()
        if last and time.time() - last["ts"] < 60:
            return {"ok": True}
    meta = clean_meta(body.meta)
    meta.update(ua_info(req.headers.get("user-agent", "")))
    log_event(body.kind, vid, uid, body.ref, meta)
    return {"ok": True}


class FeedbackReq(BaseModel):
    rating: int
    comment: str = ""
    ref: str = ""


@router.post("/api/feedback")
def feedback(body: FeedbackReq, req: Request):
    throttle("fb:" + (req.client.host if req.client else "?"), 20, 3600)
    u = current_user(req, required=False)
    plan = plan_info(u)["plan"] if u else "anon"
    db().execute("INSERT INTO feedback(ts,uid,email,rating,comment,plan,ref) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), u["id"] if u else None, u["email"] if u else "", max(1, min(5, int(body.rating))),
                  (body.comment or "").strip()[:800], plan, _clean_ref(body.ref)))
    return {"ok": True}


@router.get("/api/admin/metrics")
def admin_metrics(req: Request, days: int = 30):
    require_admin(req)
    days = max(1, min(365, days))
    now = time.time(); since = now - days * 86400
    one = lambda sql, *a: db().execute(sql, a).fetchone()[0] or 0
    ALIAS = {"clip_generated": ("clip_generated", "generation_completed"), "export": ("export", "download_completed"),
             "checkout": ("checkout", "begin_checkout")}
    def _in(kind):
        ks = ALIAS.get(kind, (kind,))
        return "kind IN (" + ",".join("?" * len(ks)) + ")", ks
    def ev(kind):
        w, ks = _in(kind); return one(f"SELECT COUNT(*) FROM events WHERE {w} AND ts>=?", *ks, since)
    def evu(kind):
        w, ks = _in(kind); return one(f"SELECT COUNT(DISTINCT COALESCE(CAST(uid AS TEXT), vid)) FROM events WHERE {w} AND ts>=?", *ks, since)

    visitors = one("SELECT COUNT(DISTINCT vid) FROM events WHERE kind IN ('visit','link') AND ts>=?", since)
    signups = one("SELECT COUNT(*) FROM users WHERE created>=?", since)
    gen_users = evu("clip_generated")
    checkout_users = one("SELECT COUNT(DISTINCT uid) FROM events WHERE kind IN ('checkout','begin_checkout') AND ts>=?", since)
    paid = db().execute("SELECT COUNT(*) n, COUNT(DISTINCT user_id) b, COALESCE(SUM(amount),0) r FROM payments WHERE status='approved' AND created>=?", (since,)).fetchone()

    # origens: cliques no link (?ref=) e visitas, cadastros, compradores e receita por origem
    src = {}
    for r in db().execute("""SELECT COALESCE(NULLIF(ref,''),'direto') s, SUM(kind='link') c, COUNT(DISTINCT vid) v FROM events
                             WHERE kind IN ('visit','link') AND ts>=? GROUP BY s""", (since,)):
        src.setdefault(r["s"], {"src": r["s"], "clicks": 0, "visitors": 0, "signups": 0, "buyers": 0, "revenue": 0.0})
        src[r["s"]]["clicks"] += r["c"] or 0; src[r["s"]]["visitors"] = max(src[r["s"]]["visitors"], r["v"])
    for r in db().execute("SELECT COALESCE(NULLIF(ref,''),'direto') s, COUNT(*) c FROM users WHERE created>=? GROUP BY s", (since,)):
        src.setdefault(r["s"], {"src": r["s"], "clicks": 0, "visitors": 0, "signups": 0, "buyers": 0, "revenue": 0.0})["signups"] = r["c"]
    for r in db().execute("""SELECT COALESCE(NULLIF(u.ref,''),'direto') s, COUNT(DISTINCT p.user_id) b, COALESCE(SUM(p.amount),0) rv
                             FROM payments p JOIN users u ON u.id=p.user_id WHERE p.status='approved' AND p.created>=? GROUP BY s""", (since,)):
        d = src.setdefault(r["s"], {"src": r["s"], "clicks": 0, "visitors": 0, "signups": 0, "buyers": 0, "revenue": 0.0})
        d["buyers"] = r["b"]; d["revenue"] = round(r["rv"], 2)

    # série por dia (horário de Brasília)
    off = -3 * 3600
    daykey = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t + off))
    series = {}
    for i in range(min(days, 90) - 1, -1, -1):
        series[daykey(now - i * 86400)] = {"visitors": set(), "signups": 0, "generated": 0, "purchases": 0, "revenue": 0.0}
    for r in db().execute("SELECT ts, vid FROM events WHERE kind IN ('visit','link') AND ts>=?", (now - 90 * 86400,)):
        k = daykey(r["ts"])
        if k in series: series[k]["visitors"].add(r["vid"])
    for r in db().execute("SELECT ts FROM events WHERE kind IN ('clip_generated','generation_completed') AND ts>=?", (now - 90 * 86400,)):
        k = daykey(r["ts"])
        if k in series: series[k]["generated"] += 1
    for r in db().execute("SELECT created FROM users WHERE created>=?", (now - 90 * 86400,)):
        k = daykey(r["created"])
        if k in series: series[k]["signups"] += 1
    for r in db().execute("SELECT created, amount FROM payments WHERE status='approved' AND created>=?", (now - 90 * 86400,)):
        k = daykey(r["created"])
        if k in series: series[k]["purchases"] += 1; series[k]["revenue"] += r["amount"] or 0

    today0 = now - ((now + off) % 86400)
    fb_rows = db().execute("SELECT * FROM feedback ORDER BY ts DESC LIMIT 100").fetchall()
    fb_dist = {str(i): one("SELECT COUNT(*) FROM feedback WHERE rating=? AND ts>=?", i, since) for i in range(1, 6)}
    fb_n = sum(fb_dist.values())
    buys = db().execute("""SELECT p.created, p.amount, p.plan, p.status, u.email, COALESCE(NULLIF(u.ref,''),'direto') ref
                           FROM payments p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.created DESC LIMIT 50""").fetchall()
    return {
        "days": days,
        "today": {
            "visitors": one("SELECT COUNT(DISTINCT vid) FROM events WHERE kind IN ('visit','link') AND ts>=?", today0),
            "signups": one("SELECT COUNT(*) FROM users WHERE created>=?", today0),
            "generated": one("SELECT COUNT(*) FROM events WHERE kind IN ('clip_generated','generation_completed') AND ts>=?", today0),
            "purchases": one("SELECT COUNT(*) FROM payments WHERE status='approved' AND created>=?", today0),
            "revenue": round(one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created>=?", today0), 2),
        },
        "totals": {
            "visitors": visitors, "link_clicks": ev("link"), "signups": signups,
            "generated": ev("clip_generated"), "gen_users": gen_users, "exports": ev("export"),
            "upgrade_open": ev("upgrade_open"), "checkout": ev("checkout"), "checkout_users": checkout_users,
            "purchases": paid["n"], "buyers": paid["b"], "revenue": round(paid["r"], 2),
            "pending": one("SELECT COUNT(*) FROM payments WHERE status IN ('pending','in_process') AND created>=?", since),
            "users_total": one("SELECT COUNT(*) FROM users"),
            "pro_active": one("SELECT COUNT(*) FROM users WHERE pro_until>? AND COALESCE(plan,'pro')='pro'", now),
            "proplus_active": one("SELECT COUNT(*) FROM users WHERE pro_until>? AND plan='proplus'", now),
        },
        "funnel": [["Visitantes", visitors], ["Criaram conta", signups], ["Geraram cortes", gen_users],
                   ["Clicaram em assinar", checkout_users], ["Compraram", paid["b"]]],
        "sources": sorted(src.values(), key=lambda d: (-d["revenue"], -d["clicks"])),
        "series": [{"day": k, "visitors": len(v["visitors"]), "signups": v["signups"], "generated": v["generated"],
                    "purchases": v["purchases"], "revenue": round(v["revenue"], 2)} for k, v in series.items()],
        "purchases": [{"ts": r["created"], "amount": r["amount"], "plan": r["plan"], "status": r["status"],
                       "email": r["email"] or "?", "ref": r["ref"]} for r in buys],
        "feedback": {"n": fb_n, "avg": round(sum(int(k) * v for k, v in fb_dist.items()) / fb_n, 2) if fb_n else None,
                     "dist": fb_dist, "liked_pct": round(100 * (fb_dist["4"] + fb_dist["5"]) / fb_n) if fb_n else None,
                     "shown": ev("feedback_shown"),
                     "list": [{"ts": r["ts"], "email": r["email"] or "anônimo", "rating": r["rating"], "comment": r["comment"],
                               "plan": r["plan"]} for r in fb_rows]},
    }
