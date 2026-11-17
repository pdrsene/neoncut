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
import os
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

PRICE_MONTH = float(os.environ.get("PRICE_MONTH", "29.90"))
PRICE_YEAR = float(os.environ.get("PRICE_YEAR", "239.00"))
FREE_PROJECTS = int(os.environ.get("FREE_PROJECTS", "3"))       # vídeos por mês no grátis
PRO_PROJECTS = int(os.environ.get("PRO_PROJECTS", "100"))       # vídeos por mês no Pro
FREE_MAX_MIN = float(os.environ.get("FREE_MAX_MINUTES", "30"))  # trecho máximo por vídeo
PRO_MAX_MIN = float(os.environ.get("PRO_MAX_MINUTES", "90"))
FREE_LIVE_MIN = float(os.environ.get("FREE_LIVE_MINUTES", "10"))
PRO_LIVE_MIN = float(os.environ.get("PRO_LIVE_MINUTES", "30"))

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
    """)


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


def is_pro(u) -> bool:
    return (u["pro_until"] or 0) > time.time() or u["email"].lower() in ADMINS


def plan_info(u) -> dict:
    pro = is_pro(u)
    used = db().execute("SELECT COUNT(*) FROM projects WHERE user_id=? AND created>=? AND charged=1",
                        (u["id"], month_start())).fetchone()[0]
    return {
        "plan": "pro" if pro else "free",
        "pro_until": u["pro_until"] or 0,
        "used": used,
        "limit": PRO_PROJECTS if pro else FREE_PROJECTS,
        "max_minutes": PRO_MAX_MIN if pro else FREE_MAX_MIN,
        "max_live_minutes": PRO_LIVE_MIN if pro else FREE_LIVE_MIN,
        "watermark": not pro,
        "quality": 1080 if pro else 720,
        "all_templates": pro,
    }


def user_public(u) -> dict:
    try:
        settings = json.loads(u["settings"] or "{}")
    except Exception:
        settings = {}
    return {"id": u["id"], "email": u["email"], "name": u["name"] or u["email"].split("@")[0],
            "avatar": u["avatar"], "admin": u["email"].lower() in ADMINS, "settings": settings,
            **plan_info(u)}


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


router = APIRouter()


# ---------------------------------------------------------------- config pública
@router.get("/api/config")
def public_config():
    return {"brand": BRAND, "google": bool(GOOGLE_ID and GOOGLE_SECRET), "payments": bool(MP_TOKEN),
            "price_month": PRICE_MONTH, "price_year": PRICE_YEAR, "free_projects": FREE_PROJECTS,
            "pro_projects": PRO_PROJECTS, "free_max_minutes": FREE_MAX_MIN, "pro_max_minutes": PRO_MAX_MIN,
            "support_email": SUPPORT_EMAIL}


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
    set_session(resp, req, cur.lastrowid)
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
    r = RedirectResponse("/#criar")
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
                                 + ("Escolha um trecho menor." if p["plan"] == "pro" else "Assine o Pro para até "
                                    f"{int(PRO_LIVE_MIN if body.kind == 'live' else PRO_MAX_MIN)} minutos."))
    if p["used"] >= p["limit"]:
        raise HTTPException(402, f"Você usou os {p['limit']} vídeos do mês no plano "
                                 + ("Pro. A cota renova no dia 1." if p["plan"] == "pro" else "grátis. Assine o Pro para continuar."))
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
        db().execute("UPDATE projects SET clips=? WHERE id=?", (json.dumps(body.clips)[:60000], pid))
    if body.meta is not None:
        db().execute("UPDATE projects SET meta=? WHERE id=?", (json.dumps(body.meta)[:4000], pid))
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


# ---------------------------------------------------------------- pagamento (Mercado Pago)
PLANS = {"month": ("Pro mensal (30 dias)", lambda: PRICE_MONTH, 30), "year": ("Pro anual (365 dias)", lambda: PRICE_YEAR, 365)}


class CheckoutReq(BaseModel):
    plan: str = "month"


@router.post("/api/billing/checkout")
def checkout(body: CheckoutReq, req: Request):
    u = current_user(req)
    if body.plan not in PLANS:
        raise HTTPException(400, "Plano inválido.")
    if not MP_TOKEN:
        raise HTTPException(503, "Pagamentos ainda não foram ativados neste site.")
    title, price, _ = PLANS[body.plan]
    base = base_url(req)
    pref = http_json(MP_API + "/checkout/preferences", "POST", {
        "items": [{"id": f"pro-{body.plan}", "title": f"{BRAND} {title}", "quantity": 1,
                   "unit_price": round(price(), 2), "currency_id": "BRL"}],
        "payer": {"email": u["email"]},
        "external_reference": f"{u['id']}:{body.plan}",
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
        uid, plan = ref.split(":")
        uid = int(uid)
    except ValueError:
        return "invalid"
    if plan not in PLANS:
        return "invalid"
    exists = db().execute("SELECT status FROM payments WHERE id=?", (str(payment_id),)).fetchone()
    if exists and exists["status"] == "approved":
        return "approved"
    db().execute("INSERT OR REPLACE INTO payments(id,user_id,plan,amount,status,created) VALUES(?,?,?,?,?,?)",
                 (str(payment_id), uid, plan, p.get("transaction_amount") or 0, status, time.time()))
    if status == "approved":
        expected = round(PLANS[plan][1](), 2)
        if float(p.get("transaction_amount") or 0) + 0.01 < expected:
            return "amount_mismatch"
        u = db().execute("SELECT pro_until FROM users WHERE id=?", (uid,)).fetchone()
        if u:
            start = max(time.time(), u["pro_until"] or 0)
            db().execute("UPDATE users SET pro_until=? WHERE id=?", (start + PLANS[plan][2] * 86400, uid))
    return status


@router.get("/api/billing/return")
def billing_return(payment_id: str = "", collection_id: str = "", status: str = ""):
    st = "pending"
    try:
        st = apply_payment(payment_id or collection_id) if (payment_id or collection_id) else (status or "failure")
    except HTTPException:
        st = "pending"
    return RedirectResponse(f"/?pagamento={urllib.parse.quote(st)}#conta")


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
            pass
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
        "pro": q("SELECT COUNT(*) FROM users WHERE pro_until>?", now),
        "projects_month": q("SELECT COUNT(*) FROM projects WHERE created>=?", month_start()),
        "revenue_month": q("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created>=?", month_start()),
        "list": [{"email": u["email"], "name": u["name"], "created": u["created"], "pro_until": u["pro_until"],
                  "last_seen": u["last_seen"]} for u in users],
    }


class GrantReq(BaseModel):
    email: str
    days: int


@router.post("/api/admin/grant")
def admin_grant(body: GrantReq, req: Request):
    require_admin(req)
    u = db().execute("SELECT * FROM users WHERE email=?", (body.email.strip().lower(),)).fetchone()
    if not u:
        raise HTTPException(404, "Usuário não encontrado.")
    if body.days <= 0:
        db().execute("UPDATE users SET pro_until=0 WHERE id=?", (u["id"],))
    else:
        start = max(time.time(), u["pro_until"] or 0)
        db().execute("UPDATE users SET pro_until=? WHERE id=?", (start + body.days * 86400, u["id"]))
    return {"ok": True}
