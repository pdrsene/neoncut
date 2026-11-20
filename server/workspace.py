"""Workspace: publishing planner, moderated opportunities and bounded cloud export."""
from __future__ import annotations
import datetime as dt
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
try:
    from . import accounts as A, creator as C
except ImportError:
    import accounts as A
    import creator as C

router=APIRouter()
CLOUD_DIR=A.DB_DIR/"cloud_exports"
CLOUD_DIR.mkdir(parents=True,exist_ok=True)
MAX_BYTES=50_000_000
TTL=24*3600
CLOUD_LIMIT={"free":1,"pro":3,"proplus":5}
PLATFORMS={"TikTok","Instagram Reels","YouTube Shorts"}
def init():
    A.db().executescript("""
    CREATE TABLE IF NOT EXISTS content_calendar(
      id INTEGER PRIMARY KEY, uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      day TEXT NOT NULL, platform TEXT NOT NULL,title TEXT NOT NULL,
      description TEXT NOT NULL,tags TEXT NOT NULL,status TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_calendar_uid ON content_calendar(uid,day);
    CREATE TABLE IF NOT EXISTS opportunities(
      id INTEGER PRIMARY KEY, title TEXT NOT NULL,creator TEXT NOT NULL,platform TEXT NOT NULL,
      requirements TEXT NOT NULL,budget TEXT NOT NULL,url TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
      created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cloud_exports(
      id TEXT PRIMARY KEY,uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
      start REAL NOT NULL, length REAL NOT NULL, layout TEXT NOT NULL, watermark INTEGER NOT NULL,
      error TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_cloud_user ON cloud_exports(uid,created);
    """)
init()

class CalendarItem(BaseModel):
    day:str
    platform:str
    title:str
    description:str=""
    tags:str=""
    status:str="planejado"

def calendar_values(body):
    try:
        date=dt.date.fromisoformat(body.day)
        if date.isoformat()!=body.day: raise ValueError()
    except ValueError: raise HTTPException(400,"Data inválida.")
    if body.platform not in PLATFORMS or body.status not in {"planejado","publicado"} or not body.title.strip():
        raise HTTPException(400,"Preencha título, plataforma e status válidos.")
    return (body.day,body.platform,body.title.strip()[:120],body.description[:2000],body.tags[:500],body.status)

@router.get("/api/workspace/calendar")
def calendar(req:Request):
    uid=A.current_user(req)["id"]
    return [dict(r) for r in A.db().execute("SELECT id,day,platform,title,description,tags,status FROM content_calendar WHERE uid=? ORDER BY day,id LIMIT 500",(uid,))]

@router.post("/api/workspace/calendar")
def calendar_add(body:CalendarItem,req:Request):
    uid=A.current_user(req)["id"]; c=A.db(); vals=calendar_values(body)
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT COUNT(*) FROM content_calendar WHERE uid=?",(uid,)).fetchone()[0]>=500: raise HTTPException(400,"Limite de 500 publicações.")
        pid=c.execute("INSERT INTO content_calendar(uid,day,platform,title,description,tags,status) VALUES(?,?,?,?,?,?,?)",(uid,*vals)).lastrowid
        c.execute("COMMIT")
    except Exception: c.execute("ROLLBACK"); raise
    return {"id":pid}

@router.put("/api/workspace/calendar/{pid}")
def calendar_edit(pid:int,body:CalendarItem,req:Request):
    uid=A.current_user(req)["id"]
    if not A.db().execute("UPDATE content_calendar SET day=?,platform=?,title=?,description=?,tags=?,status=? WHERE id=? AND uid=?",(*calendar_values(body),pid,uid)).rowcount: raise HTTPException(404,"Publicação não encontrada.")
    return {"ok":True}

@router.delete("/api/workspace/calendar/{pid}")
def calendar_delete(pid:int,req:Request):
    uid=A.current_user(req)["id"]
    if not A.db().execute("DELETE FROM content_calendar WHERE id=? AND uid=?",(pid,uid)).rowcount: raise HTTPException(404,"Publicação não encontrada.")
    return {"ok":True}

class Opportunity(BaseModel):
    title:str
    creator:str
    platform:str
    requirements:str
    budget:str=""
    url:str

def opportunity_values(b):
    try:
        p=urlsplit(b.url)
        ok=p.scheme=="https" and p.hostname and not p.username and not p.password and not any(ord(x)<33 for x in b.url)
    except ValueError: ok=False
    if not ok or len(b.url)>500 or not all(x.strip() for x in (b.title,b.creator,b.platform,b.requirements)):
        raise HTTPException(400,"Preencha os dados reais e um link HTTPS de candidatura.")
    return (b.title.strip()[:120],b.creator.strip()[:80],b.platform.strip()[:50],b.requirements.strip()[:1500],b.budget.strip()[:100],b.url)

@router.get("/api/workspace/opportunities")
def opportunities(req:Request):
    A.current_user(req)
    return [dict(r) for r in A.db().execute("SELECT id,title,creator,platform,requirements,budget,url,created FROM opportunities WHERE active=1 ORDER BY created DESC LIMIT 100")]

@router.post("/api/admin/opportunities")
def opportunity_add(body:Opportunity,req:Request):
    A.require_admin(req)
    pid=A.db().execute("INSERT INTO opportunities(title,creator,platform,requirements,budget,url,created) VALUES(?,?,?,?,?,?,?)",(*opportunity_values(body),time.time())).lastrowid
    return {"id":pid}

@router.delete("/api/admin/opportunities/{pid}")
def opportunity_delete(pid:int,req:Request):
    A.require_admin(req)
    A.db().execute("UPDATE opportunities SET active=0 WHERE id=?",(pid,))
    return {"ok":True}

def missions(uid):
    now=dt.datetime.now(dt.timezone.utc)
    monday=(now-dt.timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    start=monday.timestamp(); week=monday.date().isoformat()
    c=A.db()
    n=c.execute("SELECT COUNT(*) FROM creator_xp WHERE uid=? AND kind='clip' AND created>=?",(uid,start)).fetchone()[0]
    total=c.execute("SELECT COUNT(*) FROM creator_xp WHERE uid=? AND kind='clip'",(uid,)).fetchone()[0]
    items=[]
    for key,title,target,bonus,progress in [("weekly5","Semana produtiva",5,30,n),("weekly15","Maratona de cortes",15,70,n),("first","Primeiro corte",1,20,total),("fifty","50 cortes",50,150,total)]:
        event=key+":"+week if key.startswith("weekly") else key
        if progress>=target:
            c.execute("INSERT OR IGNORE INTO creator_xp VALUES(?,?,?,?,?)",(uid,"mission",event,bonus,time.time()))
        earned=bool(c.execute("SELECT 1 FROM creator_xp WHERE uid=? AND kind='mission' AND event_key=?",(uid,event)).fetchone())
        items.append({"title":title,"target":target,"progress":min(progress,target),"bonus":bonus,"earned":earned,"weekly":key.startswith("weekly")})
    return items

@router.get("/api/workspace/missions")
def mission_status(req:Request):
    uid=A.current_user(req)["id"]
    data=missions(uid)
    return {"missions":data,**C.level_info(C.points(uid))}

def cloud_public(row):
    return {k:row[k] for k in ("id","created","status","start","length","layout","error","size")}

def cloud_paths(jid):
    folder=CLOUD_DIR/jid
    return folder,folder/"input.mp4",folder/"output.mp4"

def validate_cloud(start,length,layout):
    if not math.isfinite(start) or not math.isfinite(length) or not 0<=start<=600 or not 1<=length<=45 or layout not in ("vertical","original"):
        raise HTTPException(400,"Use início de 0 a 600 s, duração de 1 a 45 s e formato válido.")

def reserve_cloud(uid,plan,start,length,layout):
    validate_cloud(start,length,layout); c=A.db(); now=time.time()
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT COUNT(*) FROM cloud_exports WHERE uid=? AND created>=?",(uid,now-TTL)).fetchone()[0]>=CLOUD_LIMIT[plan]: raise HTTPException(429,"Limite de testes em nuvem nas últimas 24 horas.")
        if c.execute("SELECT 1 FROM cloud_exports WHERE uid=? AND status IN ('uploading','queued','running')",(uid,)).fetchone(): raise HTTPException(409,"Você já tem uma tarefa em andamento.")
        if c.execute("SELECT COUNT(*) FROM cloud_exports WHERE status IN ('uploading','queued','running')").fetchone()[0]>=3: raise HTTPException(503,"Fila ocupada. Tente novamente mais tarde.")
        # bound private volume even when many accounts create completed jobs
        if c.execute("SELECT COUNT(*) FROM cloud_exports WHERE status IN ('uploading','queued','running','done')").fetchone()[0]>=12 or shutil.disk_usage(CLOUD_DIR).free<350_000_000: raise HTTPException(503,"Armazenamento temporário ocupado.")
        jid=secrets.token_hex(16)
        c.execute("INSERT INTO cloud_exports(id,uid,created,updated,status,start,length,layout,watermark) VALUES(?,?,?,?,?,?,?,?,?)",(jid,uid,now,now,"uploading",start,length,layout,int(plan=="free")))
        c.execute("COMMIT")
    except Exception: c.execute("ROLLBACK"); raise
    return jid

@router.post("/api/workspace/cloud")
async def cloud_submit(req:Request,start:float=0,length:float=30,layout:str="vertical"):
    u=A.current_user(req)
    if req.headers.get("x-neoncut-upload")!="1" or req.headers.get("content-type","").split(";")[0]!="application/octet-stream":
        raise HTTPException(400,"Envie um arquivo usando a interface NEONCUT.")
    try: size=int(req.headers.get("content-length","0"))
    except ValueError: raise HTTPException(400,"Tamanho inválido.")
    if size>MAX_BYTES or size<0: raise HTTPException(413,"Máximo de 50 MB.")
    jid=reserve_cloud(u["id"],A.plan_of(u),start,length,layout)
    folder,src,_=cloud_paths(jid)
    try:
        folder.mkdir(mode=0o700)
        n=0; deadline=time.monotonic()+180
        with src.open("xb") as f:
            async for chunk in req.stream():
                n+=len(chunk)
                if n>MAX_BYTES: raise HTTPException(413,"Máximo de 50 MB.")
                if time.monotonic()>deadline: raise HTTPException(408,"Envio demorou demais.")
                f.write(chunk)
        if n<1024: raise HTTPException(400,"Arquivo inválido.")
        A.db().execute("UPDATE cloud_exports SET status='queued',updated=? WHERE id=? AND status='uploading'",(time.time(),jid))
    except BaseException:
        A.db().execute("UPDATE cloud_exports SET status='error',error='Envio interrompido ou inválido.',updated=? WHERE id=?",(time.time(),jid))
        shutil.rmtree(folder,ignore_errors=True)
        raise
    return {"id":jid}

@router.get("/api/workspace/cloud")
def cloud_list(req:Request):
    uid=A.current_user(req)["id"]
    return {"jobs":[cloud_public(r) for r in A.db().execute("SELECT * FROM cloud_exports WHERE uid=? AND created>=? ORDER BY created DESC LIMIT 30",(uid,time.time()-TTL))],"daily_limit":CLOUD_LIMIT[A.plan_of(A.current_user(req))]}

@router.get("/api/workspace/cloud/{jid}/file")
def cloud_file(jid:str,req:Request):
    uid=A.current_user(req)["id"]
    row=A.db().execute("SELECT * FROM cloud_exports WHERE id=? AND uid=? AND status='done' AND created>=?",(jid,uid,time.time()-TTL)).fetchone()
    if not row: raise HTTPException(404,"Arquivo indisponível.")
    _,_,out=cloud_paths(row["id"])
    if not out.is_file(): raise HTTPException(404,"Arquivo indisponível.")
    return FileResponse(out,media_type="video/mp4",filename="neoncut-nuvem.mp4",headers={"Cache-Control":"private, no-store"})

def run_cloud(row):
    folder,src,out=cloud_paths(row["id"])
    try:
        # Force local MP4/MOV demuxer, disallow network protocols and external references.
        pre=["ffprobe","-v","error","-protocol_whitelist","file","-f","mov","-enable_drefs","0","-show_entries","format=duration:stream=codec_type,width,height","-of","json",str(src)]
        probe=subprocess.run(pre,capture_output=True,text=True,timeout=15,check=True)
        meta=json.loads(probe.stdout); duration=float(meta["format"]["duration"])
        video=next(x for x in meta["streams"] if x["codec_type"]=="video")
        if not math.isfinite(duration) or duration>645 or duration<row["start"]+row["length"]-0.15 or not 1<=video["width"]<=3840 or not 1<=video["height"]<=2160: raise ValueError("bounds")
        vf="scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280" if row["layout"]=="vertical" else "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2"
        if row["watermark"]: vf+=",drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text=NEONCUT:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.6:x=w-tw-12:y=h-th-12"
        cmd=["ffmpeg","-nostdin","-v","error","-y","-threads","1","-protocol_whitelist","file","-f","mov","-enable_drefs","0","-ss",str(row["start"]),"-i",str(src),"-t",str(row["length"]),"-map","0:v:0","-map","0:a:0?","-vf",vf,"-filter_threads","1","-threads","1","-r","30","-c:v","libx264","-preset","ultrafast","-b:v","1800k","-maxrate","2200k","-bufsize","4400k","-c:a","aac","-b:a","96k","-movflags","+faststart","-fs","18000000",str(out)]
        subprocess.run(cmd,capture_output=True,timeout=180,check=True)
        if not out.is_file() or out.stat().st_size<1024: raise ValueError("empty")
        A.db().execute("UPDATE cloud_exports SET status='done',size=?,updated=? WHERE id=?",(out.stat().st_size,time.time(),row["id"]))
        src.unlink(missing_ok=True)
    except Exception:
        A.db().execute("UPDATE cloud_exports SET status='error',error='Não foi possível converter o MP4/MOV ou o trecho informado.',updated=? WHERE id=?",(time.time(),row["id"]))
        shutil.rmtree(folder,ignore_errors=True)

def cloud_loop():
    while True:
        try:
            c=A.db(); now=time.time()
            for r in c.execute("SELECT id FROM cloud_exports WHERE created<? OR (status='uploading' AND updated<?)",(now-TTL,now-900)).fetchall():
                shutil.rmtree(cloud_paths(r["id"])[0],ignore_errors=True)
                c.execute("DELETE FROM cloud_exports WHERE id=?",(r["id"],))
            c.execute("BEGIN IMMEDIATE")
            try:
                row=c.execute("SELECT * FROM cloud_exports WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:c.execute("UPDATE cloud_exports SET status='running',updated=? WHERE id=?",(now,row["id"]))
                c.execute("COMMIT")
            except Exception:c.execute("ROLLBACK");raise
            if row:run_cloud(row)
        except Exception: pass
        time.sleep(2)

def start_worker():
    # Deployment uses one uvicorn process. Resume jobs after container restarts.
    A.db().execute("UPDATE cloud_exports SET status='queued' WHERE status='running'")
    threading.Thread(target=cloud_loop,daemon=True,name="neoncut-cloud").start()
