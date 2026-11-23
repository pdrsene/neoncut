"""NEONCUT Creator: opt-in portfolio, cosmetic XP and confirmed referrals."""
from __future__ import annotations
import json
import os
import re
import secrets
import time
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
try:
    from . import accounts as A
except ImportError:
    import accounts as A

router = APIRouter()
LEVELS = [
    (0, "Recruta", "Perfil e portfólio público opcional", "base"),
    (100, "Cortador", "Selo Cortador no portfólio", "base"),
    (300, "Especialista", "Tema verde Neon", "neon"),
    (750, "Elite", "Tema dourado Elite", "gold"),
    (1500, "Lenda", "Tema roxo Lenda", "legend"),
]
THEMES = {"base": 0, "neon": 300, "gold": 750, "legend": 1500}
PIXEL_AVATARS = [
    {"id":"rookie","name":"Pixel Rookie","tier":1,"role":"Explorador"},
    {"id":"fox","name":"Raposa Neon","tier":1,"role":"Explorador"},
    {"id":"scout","name":"Clip Scout","tier":2,"role":"Cortador"},
    {"id":"ninja","name":"Ninja do Corte","tier":2,"role":"Cortador"},
    {"id":"bot","name":"Cyber Bot","tier":3,"role":"Especialista"},
    {"id":"mage","name":"Mago dos Clipes","tier":3,"role":"Especialista"},
    {"id":"knight","name":"Cavaleiro XP","tier":4,"role":"Elite"},
    {"id":"racer","name":"Piloto Neon","tier":4,"role":"Elite"},
    {"id":"ghost","name":"Fantasma Viral","tier":5,"role":"Lenda"},
    {"id":"dragon","name":"Dragão Pixel","tier":5,"role":"Lenda"},
]
AVATAR_BY_ID = {item["id"]: item for item in PIXEL_AVATARS}
XP_PER_CLIP = 10
DAILY_CLIPS = 20
REFERRAL_XP = 100
MAX_REFERRALS = 20

def init():
    A.db().executescript("""
    CREATE TABLE IF NOT EXISTS creator_profiles(
      uid INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      token TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
      bio TEXT NOT NULL DEFAULT '', theme TEXT NOT NULL DEFAULT 'base',
      public INTEGER NOT NULL DEFAULT 0, links TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS creator_xp(
      uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, event_key TEXT NOT NULL, points INTEGER NOT NULL,
      created REAL NOT NULL, PRIMARY KEY(uid,kind,event_key)
    );
    CREATE INDEX IF NOT EXISTS ix_creator_xp_day ON creator_xp(uid,kind,created);
    CREATE TABLE IF NOT EXISTS creator_referrals(
      invited INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      inviter INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created REAL NOT NULL, CHECK(invited<>inviter)
    );
    CREATE INDEX IF NOT EXISTS ix_creator_inviter ON creator_referrals(inviter);
    """)
    cols = {r[1] for r in A.db().execute("PRAGMA table_info(creator_profiles)")}
    if "avatar_id" not in cols:
        A.db().execute("ALTER TABLE creator_profiles ADD COLUMN avatar_id TEXT NOT NULL DEFAULT 'rookie'")

init()

def ensure_profile(uid):
    A.db().execute("INSERT OR IGNORE INTO creator_profiles(uid,token) VALUES(?,?)",
                   (uid, secrets.token_urlsafe(12)))
    return A.db().execute("SELECT * FROM creator_profiles WHERE uid=?", (uid,)).fetchone()

# contas com todos os níveis liberados (separadas por vírgula; padrão: a conta do Pedro)
MAX_LEVEL_EMAILS = {e.strip().lower() for e in os.environ.get("CREATOR_MAX_EMAILS", "eusene1212@gmail.com").split(",") if e.strip()}


def points(uid):
    xp = int(A.db().execute("SELECT COALESCE(SUM(points),0) FROM creator_xp WHERE uid=?", (uid,)).fetchone()[0])
    if MAX_LEVEL_EMAILS:
        r = A.db().execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
        if r and (r["email"] or "").lower() in MAX_LEVEL_EMAILS:
            xp = max(xp, LEVELS[-1][0])
    return xp

def level_info(xp):
    i = max(i for i, l in enumerate(LEVELS) if xp >= l[0])
    floor, title, reward, _ = LEVELS[i]
    nxt = LEVELS[i+1][0] if i+1 < len(LEVELS) else None
    return {"xp": xp, "level": i+1, "title": title, "next": nxt, "floor": floor,
            "reward": reward, "levels": [{"level": j+1, "xp": l[0], "title": l[1], "reward": l[2]} for j,l in enumerate(LEVELS)]}

def sync_referrals(uid):
    c = A.db()
    # Confirmed backend payments, never clicks or signups. Cosmetic points only.
    rows = c.execute("""SELECT r.invited, MIN(p.created) AS paid
        FROM creator_referrals r JOIN payments p ON p.user_id=r.invited
        WHERE r.inviter=? AND p.status='approved' AND p.created>=r.created
        GROUP BY r.invited HAVING MIN(p.created)<=? ORDER BY paid, r.invited LIMIT ?""",
        (uid,time.time()-7*86400,MAX_REFERRALS)).fetchall()
    for row in rows:
        c.execute("INSERT OR IGNORE INTO creator_xp VALUES(?,?,?,?,?)",
                  (uid,"referral",str(row["invited"]),REFERRAL_XP,time.time()))

def profile_data(row, xp):
    lvl = level_info(xp)
    requested = row["avatar_id"] if "avatar_id" in row.keys() else "rookie"
    item = AVATAR_BY_ID.get(requested)
    avatar_id = requested if item and item["tier"] <= lvl["level"] else "rookie"
    return {"token": row["token"], "name": row["name"], "bio": row["bio"],
            "theme": row["theme"] if xp >= THEMES.get(row["theme"],10**9) else "base",
            "avatar_id":avatar_id, "avatars": PIXEL_AVATARS,
            "public": bool(row["public"]), "links": json.loads(row["links"]), **lvl}

class AvatarPick(BaseModel):
    avatar_id: str

@router.post("/api/creator/avatar")
def set_avatar(body: AvatarPick, req: Request):
    u = A.current_user(req)
    item = AVATAR_BY_ID.get(body.avatar_id)
    if not item:
        raise HTTPException(400, "Personagem inválido.")
    if item["tier"] > level_info(points(u["id"]))["level"]:
        raise HTTPException(403, "Esse personagem será liberado em outro nível.")
    ensure_profile(u["id"])
    A.db().execute("UPDATE creator_profiles SET avatar_id=? WHERE uid=?", (item["id"], u["id"]))
    return {"ok": True, "avatar_id": item["id"]}

@router.get("/api/creator/me")
def me(req: Request):
    u=A.current_user(req); uid=u["id"]
    row=ensure_profile(uid); sync_referrals(uid)
    data=profile_data(row,points(uid))
    data["invited"]=A.db().execute("SELECT COUNT(*) FROM creator_referrals WHERE inviter=?",(uid,)).fetchone()[0]
    data["qualified"]=A.db().execute("SELECT COUNT(*) FROM creator_xp WHERE uid=? AND kind='referral'",(uid,)).fetchone()[0]
    data["clips_today"]=A.db().execute("SELECT COUNT(*) FROM creator_xp WHERE uid=? AND kind='clip' AND created>=?",(uid,time.time()-86400)).fetchone()[0]
    return data

class ClipClaim(BaseModel):
    project_id: int
    index: int

@router.post("/api/creator/clip")
def clip_claim(body: ClipClaim, req: Request):
    u=A.current_user(req); uid=u["id"]; c=A.db()
    # The video is rendered locally; this is a client-reported export of an existing
    # saved cut, not proof of a download. No cash, plan quota or discount is granted.
    c.execute("BEGIN IMMEDIATE")
    try:
        row=c.execute("SELECT clips, meta FROM projects WHERE id=? AND user_id=?",(body.project_id,uid)).fetchone()
        if not row: raise HTTPException(404,"Projeto não encontrado.")
        clips=json.loads(row["clips"] or "[]")
        meta=json.loads(row["meta"] or "{}")
        if meta.get("hidden") or not 0<=body.index<min(len(clips),20):
            raise HTTPException(400,"Corte inválido.")
        clip=clips[body.index]
        try:
            valid = isinstance(clip,dict) and 1<=float(clip.get("end",0))-float(clip.get("start",0))<=180
        except (TypeError,ValueError):
            valid = False
        if not valid:
            raise HTTPException(400,"Trecho inválido.")
        key=f"{body.project_id}:{body.index}"
        n=c.execute("SELECT COUNT(*) FROM creator_xp WHERE uid=? AND kind='clip' AND created>=?",(uid,time.time()-86400)).fetchone()[0]
        added=0
        if n<DAILY_CLIPS:
            added=c.execute("INSERT OR IGNORE INTO creator_xp VALUES(?,?,?,?,?)",(uid,"clip",key,XP_PER_CLIP,time.time())).rowcount*XP_PER_CLIP
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return {"added":added,**level_info(points(uid))}

class Portfolio(BaseModel):
    name: str = ""
    bio: str = ""
    theme: str = "base"
    public: bool = False
    links: list[dict] = []

def safe_link(url):
    if not isinstance(url,str) or len(url)>500 or any(ord(c)<32 for c in url): return False
    try:
        p=urlsplit(url)
        return (p.scheme=="https" and p.netloc and not p.username and not p.password
                and (p.hostname or "").lower() in {"www.youtube.com","youtube.com","youtu.be","www.tiktok.com","tiktok.com","www.instagram.com","instagram.com","www.twitch.tv","twitch.tv","clips.twitch.tv","kick.com","www.kick.com","vimeo.com","www.vimeo.com"})
    except ValueError:
        return False

@router.post("/api/creator/portfolio")
def portfolio(body: Portfolio, req: Request):
    u=A.current_user(req); uid=u["id"]; ensure_profile(uid); xp=points(uid)
    if body.theme not in THEMES or xp<THEMES[body.theme]: raise HTTPException(400,"Tema ainda não desbloqueado.")
    if body.public and not body.name.strip(): raise HTTPException(400,"Escolha um nome público.")
    if len(body.links)>10: raise HTTPException(400,"Até 10 trabalhos.")
    links=[]
    for l in body.links:
        url=l.get("url",""); title=str(l.get("title","")).strip()[:100]
        if not safe_link(url) or not title: raise HTTPException(400,"Informe um título e link HTTPS de plataforma aceita.")
        links.append({"url":url,"title":title})
    A.db().execute("UPDATE creator_profiles SET name=?,bio=?,theme=?,public=?,links=? WHERE uid=?",
        (body.name.strip()[:60],body.bio.strip()[:280],body.theme,int(body.public),json.dumps(links),uid))
    return profile_data(ensure_profile(uid),xp)

@router.get("/api/creator/public/{token}")
def public_profile(token: str):
    row=A.db().execute("SELECT * FROM creator_profiles WHERE token=? AND public=1",(token,)).fetchone()
    if not row: raise HTTPException(404,"Portfólio privado ou indisponível.")
    d=profile_data(row,points(row["uid"]))
    # No email, account ID, projects, or private video URLs.
    return {k:d[k] for k in ("name","bio","theme","links","title","level","avatar_id")}


@router.get("/api/creator/ranking")
def ranking():
    """Ranking real dos clipadores: só quem deixou o portfólio público aparece (nome escolhido no perfil)."""
    rows = A.db().execute("""
        SELECT p.uid, p.name, p.token, p.avatar_id,
               COALESCE(SUM(x.points),0) AS xp,
               COALESCE(SUM(CASE WHEN x.kind='clip' THEN 1 ELSE 0 END),0) AS clips,
               COALESCE(SUM(CASE WHEN x.kind='clip' AND x.created>=? THEN 1 ELSE 0 END),0) AS week
        FROM creator_profiles p LEFT JOIN creator_xp x ON x.uid=p.uid
        WHERE p.public=1 AND TRIM(p.name)<>''
        GROUP BY p.uid HAVING xp>0 ORDER BY xp DESC, clips DESC LIMIT 20""", (time.time() - 7 * 86400,)).fetchall()
    out = []
    for r in rows:
        lv = level_info(int(r["xp"]))
        out.append({"name": r["name"][:28], "token": r["token"], "avatar": r["avatar_id"] or "rookie",
                    "xp": lv["xp"], "level": lv["level"], "title": lv["title"],
                    "clips": int(r["clips"]), "week": int(r["week"])})
    return {"ranking": out}
