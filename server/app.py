"""
NEONCUT — servidor de download de vídeos (YouTube, Twitch, Kick e outras).

O servidor só baixa o vídeo da plataforma e entrega um MP4 ao navegador.
Todo o resto (detecção de momentos, legendas, templates e exportação)
continua acontecendo no navegador de quem usa o site.

Rodar localmente:
    pip install -r server/requirements.txt
    uvicorn server.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import math
import selectors
import signal
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from . import accounts, growth, streams, youtube, creator, workspace
except ImportError:  # executado como script
    import accounts  # type: ignore
    import growth  # type: ignore
    import streams  # type: ignore
    import youtube  # type: ignore
    import creator  # type: ignore
    import workspace  # type: ignore

# ---------------------------------------------------------------- config
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA = Path(os.environ.get("DATA_DIR", "/tmp/neoncut"))
DATA.mkdir(parents=True, exist_ok=True)

MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "120"))        # tamanho máximo de um trecho
MAX_LIVE_MINUTES = float(os.environ.get("MAX_LIVE_MINUTES", "60"))  # teto técnico; o plano de cada pessoa define o limite real
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))          # downloads simultâneos
JOBS_PER_HOUR = int(os.environ.get("JOBS_PER_HOUR", "20"))        # por IP
FILE_TTL = int(os.environ.get("FILE_TTL", "1200"))               # apaga arquivos não buscados depois de 20 min
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "700"))         # espaço livre mínimo para começar um download
COOKIES = os.environ.get("YTDLP_COOKIES")                        # caminho de um cookies.txt (ajuda no YouTube)
if not COOKIES and os.environ.get("YTDLP_COOKIES_TEXT"):         # ou o conteúdo do cookies.txt colado numa variável do Railway
    import base64 as _b64
    _raw = os.environ["YTDLP_COOKIES_TEXT"].strip()
    if "\t" not in _raw and "\\t" not in _raw:
        try:
            _raw = _b64.b64decode(_raw).decode("utf-8")
        except Exception:
            pass
    _raw = _raw.replace("\\t", "\t").replace("\\n", "\n")
    if not _raw.startswith("# "):
        _raw = "# Netscape HTTP Cookie File\n" + _raw
    COOKIES = str(DATA / "cookies.txt")
    Path(COOKIES).write_text(_raw + "\n")
PROXY = os.environ.get("YTDLP_PROXY")                            # proxy residencial opcional
ALLOW_ANY_URL = os.environ.get("ALLOW_ANY_URL") == "1"           # só para testes
CORS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

PLATFORMS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "twitch": ("twitch.tv",),
    "kick": ("kick.com",),
}


def platform_of(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for name, hosts in PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return None


def youtube_canonical(url: str) -> str | None:
    try:
        u=urlparse(url.strip())
        if u.scheme not in ("http","https") or u.username or u.password or u.port:
            return None
        host=(u.hostname or "").lower()
        parts=[p for p in u.path.split("/") if p]
        vid=None
        if host=="youtu.be" and len(parts)==1:
            vid=parts[0]
        elif host in ("youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtube-nocookie.com","www.youtube-nocookie.com"):
            if u.path=="/watch":
                vid=parse_qs(u.query).get("v",[None])[0]
            elif len(parts)==2 and parts[0] in ("shorts","live","embed","v"):
                vid=parts[1]
        return "https://www.youtube.com/watch?v="+vid if vid and re.fullmatch(r"[A-Za-z0-9_-]{11}",vid) else None
    except (ValueError,AttributeError):
        return None


def youtube_reference(url: str) -> dict:
    # Official public metadata only; this does not grant access to media bytes.
    result={"platform":"youtube","url":url,"title":"Vídeo do YouTube","import_required":True,
            "import_message":"A importação direta não está disponível para este vídeo. Envie o arquivo original de um vídeo próprio ou autorizado."}
    try:
        endpoint="https://www.youtube.com/oembed?"+urlencode({"url":url,"format":"json"})
        with urlopen(endpoint,timeout=5) as response:
            data=json.loads(response.read(65536))
        result["title"]=str(data.get("title") or result["title"])[:200]
        result["uploader"]=str(data.get("author_name") or "")[:120]
    except Exception:
        pass
    return result


def check_url(url: str, allow_yt_beta: bool = False) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Cole um link completo, começando com https://")
    p = platform_of(url)
    if not p and not ALLOW_ANY_URL:
        raise HTTPException(400, "Por enquanto aceitamos links da Twitch e da Kick.")
    if p == "youtube":
        if not youtube_canonical(url):
            raise HTTPException(400,"Cole um link de vídeo, Short ou live específica do YouTube.")
        if not accounts.YT_ENABLED and not allow_yt_beta:
            raise HTTPException(400, accounts.YT_MAINT_MSG)
    return p or "outro"


YT_ALT_CLIENTS = "youtube:player_client=tv,web_safari,mweb"
try:  # provider novo (pip bgutil-ytdlp-pot-provider). Carregar os dois juntos quebra o yt-dlp
    import importlib.util as _ilu
    _PIP_POT = _ilu.find_spec("yt_dlp_plugins.extractor.getpot_bgutil") is not None
except Exception:
    _PIP_POT = False
_yt_alt_until = 0.0  # quando os clientes alternativos do YouTube funcionaram, usa eles por um tempo


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(^|\.)(youtube\.com|youtu\.be)", (urlparse(url).hostname or "") if url else ""))


def is_bot_check(text: str) -> bool:
    t = (text or "").lower()
    return "sign in to confirm" in t or "not a bot" in t


def ytdlp_base(url: str = "", alt: bool | None = None) -> list[str]:
    yt = bool(url and is_youtube(url))
    # Conservative retry/concurrency on YouTube; the datacenter IP may be rate-limited.
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--ignore-config", "--force-ipv4",
           "-N", "2" if yt else "8",
           "--socket-timeout", "20", "--retries", "2" if yt else "5",
           "--fragment-retries", "3" if yt else "10"]
    if url and is_youtube(url) and (alt if alt is not None else time.time() < _yt_alt_until):
        cmd += ["--extractor-args", YT_ALT_CLIENTS]
    if Path("/app/yt-plugins").exists() and not _PIP_POT:  # plugin antigo (Rust) só se o novo não estiver instalado
        cmd += ["--plugin-dirs", "/app/yt-plugins"]
    if COOKIES and Path(COOKIES).exists():
        cmd += ["--cookies", COOKIES]
    if PROXY:
        cmd += ["--proxy", PROXY]
    return cmd


def friendly_error(text: str) -> str:
    t = text.lower()
    if "sign in to confirm" in t or "not a bot" in t:
        return "O YouTube não liberou este vídeo para nós agora. Tente de novo em alguns minutos, use um link da Twitch ou da Kick, ou baixe o vídeo e envie pela aba \"Enviar arquivo\"."
    if "private" in t:
        return "Esse vídeo é privado."
    if "subscriber" in t or "sub-only" in t or "subscribers-only" in t:
        return "Esse VOD é só para inscritos do canal."
    if "age" in t and "restricted" in t:
        return "Vídeo com restrição de idade: precisa de cookies de uma conta logada no servidor."
    if "is offline" in t or "not currently live" in t:
        return "O canal não está ao vivo agora."
    if "unsupported url" in t:
        return "Esse link não é de um vídeo, VOD ou live."
    if "404" in t or "not found" in t or "does not exist" in t:
        return "Vídeo não encontrado. Confira o link."
    if "unable to connect to proxy" in t or "connection refused" in t or "timed out" in t or "name or service not known" in t:
        return "O servidor não conseguiu acessar a plataforma agora. Tente de novo em instantes."
    if "drm" in t:
        return "Esse vídeo é protegido (DRM) e não pode ser baixado."
    last = [l for l in text.strip().splitlines() if l.strip()]
    msg = last[-1] if last else "Falha ao baixar o vídeo."
    msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", msg)
    msg = re.sub(r";?\s*please report this issue.*$", "", msg, flags=re.I)
    return msg[:300]


# ---------------------------------------------------------------- app
app = FastAPI(title="NEONCUT API")
app.include_router(accounts.router)
app.include_router(streams.router)
app.include_router(youtube.router)
app.include_router(growth.router)
app.include_router(creator.router)
app.include_router(workspace.router)

@app.on_event('startup')
def start_workspace():
    workspace.start_worker()
    # One bounded, non-blocking YouTube import smoke test on every deployment.
    # Results are diagnostic only; the public feature flag remains independent.
    if os.environ.get('YT_SMOKE_TEST', '1') == '1':
        threading.Thread(target=youtube_smoke_test, daemon=True, name='youtube-smoke-test').start()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    # obriga o navegador a sempre usar HTTPS (só quando o acesso já veio por HTTPS)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.path != "/relay.html":
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    else:
        resp.headers["Content-Security-Policy"] = "frame-ancestors https://www.instagram.com https://www.tiktok.com"
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # isolamento de origem: libera vários núcleos para a IA de legendas (WASM multi-thread).
    # Só em navegadores Chromium, que suportam o modo "credentialless" sem quebrar fontes,
    # imagens e o player embutido. Os outros seguem como antes.
    ua = request.headers.get("user-agent", "")
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype and request.url.path != "/relay.html":
        resp.headers["Vary"] = "User-Agent"
        if "Chrome/" in ua and "Firefox" not in ua and "CriOS" not in ua:
            resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            resp.headers.setdefault("Cross-Origin-Embedder-Policy", "credentialless")
    return resp
app.add_middleware(CORSMiddleware, allow_origins=CORS, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"])

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
SLOTS = threading.Semaphore(MAX_PARALLEL)
HITS: dict[str, list[float]] = {}
VIDEO_INFO: dict[tuple[int,str],tuple[float,float]] = {}  # bounded, short-lived duration cache


def client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?"))


def rate_limit(ip: str):
    now = time.time()
    hits = [t for t in HITS.get(ip, []) if now - t < 3600]
    if len(hits) >= JOBS_PER_HOUR:
        raise HTTPException(429, "Limite de downloads por hora atingido. Tente de novo mais tarde.")
    hits.append(now)
    HITS[ip] = hits


@app.get("/api/health")
def health():
    ver = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
    return {"ok": True, "yt_dlp": ver, "jobs": len(JOBS), "max_minutes": MAX_MINUTES, "max_live_minutes": MAX_LIVE_MINUTES}


def youtube_failure_kind(error: str) -> str:
    t = (error or "").lower()
    if is_bot_check(t) or "403" in t or "429" in t or "login required" in t or "sign in" in t:
        return "access"
    if "private" in t or "drm" in t or "age-restricted" in t:
        return "restricted"
    if "requested format" in t or "no video formats" in t:
        return "format"
    if "timed out" in t or "connection" in t:
        return "timeout"
    return "extractor"


def youtube_import_fallback(url: str, reason: str) -> dict:
    # Log only a fixed category, never raw extractor output or credentials.
    import logging
    logging.getLogger("uvicorn.error").warning("YouTube import unavailable: %s", reason)
    result = youtube_reference(url)
    result["import_reason"] = reason
    messages = {
        "timeout": "O YouTube demorou para responder. Tente novamente ou envie o arquivo original autorizado.",
        "unavailable": "O serviço de importação está indisponível. Envie o arquivo original autorizado.",
        "access": "O YouTube não autorizou a importação direta. Envie o arquivo original de um vídeo próprio ou autorizado.",
        "restricted": "Este vídeo possui restrições de acesso. Envie o arquivo original autorizado.",
        "format": "Nenhum formato compatível foi disponibilizado. Envie o arquivo original autorizado.",
        "metadata": "Não foi possível interpretar as informações do vídeo. Tente novamente ou envie o arquivo autorizado.",
    }
    result["import_message"] = messages.get(reason, result["import_message"])
    return result


class InfoReq(BaseModel):
    url: str


@app.post("/api/info")
def info(body: InfoReq, req: Request):
    u = accounts.current_user(req)
    plan = accounts.plan_info(u)
    plat = check_url(body.url, allow_yt_beta=u["email"].lower() in accounts.ADMINS)
    if plat=="youtube":
        body.url=youtube_canonical(body.url)
        with LOCK:
            rate_limit("info-yt-"+str(u["id"]))
    global _yt_alt_until
    try:
        r = subprocess.run(ytdlp_base(body.url) + ["-J", "--skip-download", body.url],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and is_youtube(body.url) and is_bot_check(r.stderr + r.stdout) and time.time() >= _yt_alt_until:
            # o YouTube pediu a verificação anti-robô: tenta de novo com outros clientes do YouTube
            r2 = subprocess.run(ytdlp_base(body.url, alt=True) + ["-J", "--skip-download", body.url],
                                capture_output=True, text=True, timeout=60)
            if r2.returncode == 0:
                _yt_alt_until = time.time() + 1800
            r = r2
    except subprocess.TimeoutExpired:
        if plat=="youtube":return youtube_import_fallback(body.url, "timeout")
        raise HTTPException(504, "A plataforma demorou demais para responder.")
    except OSError:
        if plat=="youtube":return youtube_import_fallback(body.url, "unavailable")
        raise HTTPException(503, "O serviço de importação está indisponível.")
    if r.returncode != 0:
        if plat=="youtube":return youtube_import_fallback(body.url, youtube_failure_kind(r.stderr or r.stdout))
        raise HTTPException(400, friendly_error(r.stderr or r.stdout))
    try:
        j = json.loads(r.stdout)
        if not isinstance(j, dict):
            raise ValueError("invalid metadata")
    except (ValueError, TypeError):
        if plat=="youtube":return youtube_import_fallback(body.url, "metadata")
        raise HTTPException(502, "A plataforma retornou informações inválidas.")
    if j.get("_type") == "playlist" and j.get("entries"):
        j = j["entries"][0]
    video_url = j.get("webpage_url") or body.url
    duration=j.get("duration")
    if isinstance(duration,(int,float)) and math.isfinite(duration) and duration>0 and not j.get("is_live"):
        with LOCK:
            for key in {body.url,video_url}:
                VIDEO_INFO[(u["id"],key)]=(time.monotonic(),float(duration))
            while len(VIDEO_INFO)>256:
                VIDEO_INFO.pop(next(iter(VIDEO_INFO)))
    return {
        "platform": plat,
        "title": j.get("title") or "Vídeo",
        "uploader": j.get("uploader") or j.get("channel") or "",
        "duration": j.get("duration"),
        "is_live": bool(j.get("is_live")),
        "thumbnail": j.get("thumbnail"),
        "url": j.get("webpage_url") or body.url,
        "max_minutes": min(MAX_MINUTES, plan["max_minutes"]),
        "max_live_minutes": min(MAX_LIVE_MINUTES, plan["max_live_minutes"]),
    }


def youtube_smoke_test():
    """Probe the production egress IP at startup without user data or an open diagnostic endpoint.

    Tests public metadata and a tiny bounded media transfer. Never logs extractor output,
    URLs, cookies or tokens; and never changes the YouTube feature flag automatically.
    """
    import logging
    import tempfile
    logger = logging.getLogger("uvicorn.error")
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    time.sleep(8)  # let the local PO-token provider start
    provider = False
    try:
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:4416/ping", timeout=3) as res:
            provider = res.status == 200
    except Exception:
        pass
    runtime = bool(shutil.which("deno"))
    for name, alt in (("default", False), ("alternative", True)):
        try:
            cmd = ytdlp_base(url, alt=alt) + ["--simulate", "--no-playlist",
                                            "--print", "id", "-f", "b[height<=360]/b", url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            ok = result.returncode == 0 and "jNQXAC9IVRw" in result.stdout
            reason = "ok" if ok else youtube_failure_kind(result.stderr or result.stdout)
            logger.info("YT_SMOKE metadata=%s client=%s provider=%s deno=%s", reason, name, provider, runtime)
            if not ok:
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="yt-smoke-", dir=str(DATA)) as folder:
                    # --test limits transferred bytes; no video is retained.
                    dlcmd = ytdlp_base(url, alt=alt) + [
                        "--test", "--no-playlist", "--max-filesize", "3000000",
                        "-f", "b[height<=360]/b", "-o", str(Path(folder) / "test.%(ext)s"), url]
                    download = subprocess.run(dlcmd, capture_output=True, text=True, timeout=70)
                    got = any(f.is_file() and f.stat().st_size >= 1024 for f in Path(folder).iterdir())
                    status = "ok" if download.returncode == 0 and got else youtube_failure_kind(download.stderr or download.stdout)
                    logger.info("YT_SMOKE media=%s client=%s", status, name)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning("YT_SMOKE media=timeout client=%s", name)
            break
        except subprocess.TimeoutExpired:
            logger.warning("YT_SMOKE metadata=timeout client=%s", name)
        except OSError:
            logger.warning("YT_SMOKE metadata=extractor_unavailable client=%s", name)


@app.get("/api/diag/yt")
def diag_yt(req: Request, token: str = "", v: str = "jNQXAC9IVRw"):
    """Diagnóstico do YouTube no servidor (só com DIAG_TOKEN ou conta admin). Não mostra segredos."""
    tok = os.environ.get("DIAG_TOKEN", "")
    if not (tok and secrets.compare_digest(token, tok)):
        accounts.require_admin(req)
    out: dict = {"cookies": bool(COOKIES and Path(COOKIES).exists()), "proxy": bool(PROXY),
                 "plugins": Path("/app/yt-plugins/bgutil.zip").exists(), "alt_active": time.time() < _yt_alt_until}
    try:
        import urllib.request
        out["pot_server"] = urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=3).read().decode()[:80]
    except Exception as e:  # noqa
        out["pot_server"] = "off: " + str(e)[:60]
    out["deno"] = shutil.which("deno") or None
    url = "https://www.youtube.com/watch?v=" + re.sub(r"[^A-Za-z0-9_-]", "", v)[:15]
    for name, alt in (("padrao", False), ("alternativo", True)):
        try:
            r = subprocess.run(ytdlp_base(url, alt=alt) + ["-v", "--simulate", "-f", "b[height<=720]/b", url],
                               capture_output=True, text=True, timeout=90)
            lines = (r.stderr + r.stdout).splitlines()
            keep = [l[:220] for l in lines if re.search(r"yt-dlp version|JS runtimes|PO Token Providers|\[pot\]|ERROR|Sign in|bot|Downloading .*player|format\(s\)", l)]
            out[name] = {"ok": r.returncode == 0, "linhas": keep[-14:]}
        except subprocess.TimeoutExpired:
            out[name] = {"ok": False, "linhas": ["timeout"]}
    return out


class JobReq(BaseModel):
    url: str
    start: float = 0
    end: float | None = None
    live_minutes: float | None = None  # para lives: grava os próximos N minutos
    quality: int = Field(default=720)


def import_format(quality: int) -> str:
    # Explicitly requested 480p has no silent higher-resolution fallback.
    return f"bv*[height<={quality}][vcodec^=avc1]+ba[ext=m4a]/b[height<={quality}][vcodec^=avc1]/bv*[height<={quality}]+ba/b[height<={quality}]" + ("/bv*+ba/b" if quality>=720 else "")


def whole_download_allowed(duration, start, end):
    return bool(duration and math.isfinite(duration) and duration<=900 and duration<=max(1,end-start)*1.25+5)


def whole_requested(duration,start,end):
    return whole_download_allowed(duration,start,end) and start<=.01 and end>=duration-.55


@app.post("/api/jobs")
def create_job(body: JobReq, req: Request):
    u = accounts.current_user(req)
    plan = accounts.plan_info(u)
    max_min, max_live = min(MAX_MINUTES, plan["max_minutes"]), min(MAX_LIVE_MINUTES, plan["max_live_minutes"])
    plat = check_url(body.url, allow_yt_beta=u["email"].lower() in accounts.ADMINS)
    if plat=="youtube":body.url=youtube_canonical(body.url)
    if body.quality==1080 and plan.get("plan","free")=="free":
        raise HTTPException(403,"Importação em 1080p é dos planos Pro e Pro+.")
    if body.quality not in (480,720,1080) or not math.isfinite(body.start) or (body.end is not None and not math.isfinite(body.end)):
        raise HTTPException(400,"Qualidade ou trecho inválido.")
    if body.live_minutes is not None:
        if not (0.5 <= body.live_minutes <= max_live):
            raise HTTPException(400, f"No seu plano, grave entre 1 e {int(max_live)} minutos da live.")
    else:
        if body.start < 0 or (body.end is not None and body.end <= body.start):
            raise HTTPException(400, "Trecho inválido: o fim precisa ser depois do início.")
        if body.end is None:
            raise HTTPException(400, "Informe o fim do trecho.")
        if body.end - body.start > max_min * 60 + 1:
            raise HTTPException(400, f"No seu plano, escolha um trecho de até {int(max_min)} minutos.")
    with LOCK:
        active = [j for j in JOBS.values() if j.get("uid") == u["id"] and j["status"] in ("queued", "downloading", "processing")]
        if len(active) >= 2:
            raise HTTPException(429, "Você já tem downloads em andamento. Espere terminarem.")
        rate_limit(f"u{u['id']}")
        jid = uuid.uuid4().hex[:12]
        cached=VIDEO_INFO.get((u["id"],body.url))
        duration=cached[1] if cached and time.monotonic()-cached[0]<900 else None
        job = {"id": jid, "ip": client_ip(req), "uid": u["id"], "url": body.url, "platform": plat, "status": "queued","known_duration":duration,
               "progress": 0.0, "message": "Na fila…", "error": None, "file": None, "size": 0,
               "created": time.time(), "req": body.model_dump()}
        JOBS[jid] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return public(job)


def public(job: dict) -> dict:
    result={k: job[k] for k in ("id", "status", "progress", "message", "error", "size", "platform")}
    if job["status"]=="downloading" and job.get("dir"):
        try:
            result["received_bytes"]=sum(p.stat().st_size for p in Path(job["dir"]).glob("raw.*") if p.is_file())
        except OSError:pass
    return result


@app.get("/api/jobs/{jid}")
def get_job(jid: str, req: Request):
    job = JOBS.get(jid)
    u = accounts.current_user(req)
    if not job or job.get("uid") != u["id"]:
        raise HTTPException(404, "Download não encontrado (pode ter expirado).")
    return public(job)


@app.get("/api/jobs/{jid}/file")
def get_file(jid: str, req: Request):
    job = JOBS.get(jid)
    u = accounts.current_user(req)
    if not job or job.get("uid") != u["id"] or job["status"] != "done" or not job["file"] or not Path(job["file"]).exists():
        raise HTTPException(404, "Arquivo indisponível.")
    # o navegador busca o vídeo uma vez só e guarda no aparelho: depois de enviar, apaga do servidor
    # (o disco do servidor é pequeno; sem isso, poucos vídeos longos lotavam o disco e travavam os downloads)
    def _drop():
        shutil.rmtree(job.get("dir") or (DATA / jid), ignore_errors=True)
        job["file"] = None
    return FileResponse(job["file"], media_type="video/mp4", filename=f"neoncut-{jid}.mp4", background=BackgroundTask(_drop))


# ---------------------------------------------------------------- download
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def stream_proc(cmd: list[str], job: dict, total_secs: float | None, label: str, timeout: float) -> tuple[int, str]:
    """Roda um processo, lê o progresso (yt-dlp % ou ffmpeg time=) e devolve (código, log)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=job["dir"], start_new_session=True)
    log, buf = [], b""
    deadline=time.monotonic()+timeout
    def consume(raw):
        line=raw.decode("utf-8","replace").strip()
        if not line:return
        log.append(line[:2000]);log[:]=log[-80:]
        m=PCT_RE.search(line)
        q=float(m.group(1))/100 if m else None
        m=TIME_RE.search(line)
        if m and total_secs:
            q=(int(m.group(1))*3600+int(m.group(2))*60+float(m.group(3)))/total_secs
        if q is not None:
            job["progress"]=max(job.get("progress",0),min(.99,max(0,q)))
        job["message"]=f"{label} {int(job.get('progress',0)*100)}%"
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(cmd,timeout)
                if not sel.select(min(1,remaining)):continue
                data=os.read(proc.stdout.fileno(),16384)
                if not data:break
                parts=re.split(br"[\r\n]",buf+data)
                buf=parts.pop()[-8192:]
                for line in parts:consume(line)
            if buf:consume(buf)
            proc.wait(timeout=max(.01,deadline-time.monotonic()))
    except BaseException:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    return proc.returncode,"\n".join(log)


def ffprobe_codec(path: Path) -> tuple[str, bool]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        streams = json.loads(r.stdout).get("streams", [])
    except Exception:
        streams = []
    v = next((s["codec_name"] for s in streams if s.get("codec_type") == "video"), "")
    a = any(s.get("codec_type") == "audio" for s in streams)
    return v, a


def faststart_mp4(path: Path) -> bool:
    """Conservative check for an already streamable, non-fragmented MP4."""
    try:
        size=path.stat().st_size
        if path.suffix.lower()!=".mp4":return False
        found_ftyp=found_moov=found_mdat=False
        with path.open("rb") as f:
            pos=0
            for _ in range(256):
                if pos==size:return found_ftyp and found_moov and found_mdat
                if pos+8>size:return False
                f.seek(pos)
                header=f.read(8)
                n=int.from_bytes(header[:4],"big"); tag=header[4:]
                head=8
                if n==1:
                    n=int.from_bytes(f.read(8),"big");head=16
                elif n==0:n=size-pos
                if n<head or pos+n>size:return False
                if tag==b"ftyp":found_ftyp=True
                elif tag==b"moov":
                    if found_mdat:return False
                    found_moov=True
                elif tag==b"mdat":
                    if not found_moov:return False
                    found_mdat=True
                elif tag in (b"moof",b"mfra"):return False
                pos+=n
        return False
    except OSError:return False


def browser_ready_mp4(path: Path) -> bool:
    if not faststart_mp4(path):return False
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type,codec_name,pix_fmt","-of","json",str(path)],capture_output=True,text=True,timeout=15)
        if r.returncode:return False
        streams=json.loads(r.stdout).get("streams",[])
        video=[s for s in streams if s.get("codec_type")=="video"]
        audio=[s for s in streams if s.get("codec_type")=="audio"]
        return len(video)==1 and video[0].get("codec_name")=="h264" and video[0].get("pix_fmt") in ("yuv420p","yuvj420p") and all(s.get("codec_name")=="aac" for s in audio)
    except (OSError,ValueError,subprocess.TimeoutExpired):return False


def probe_duration(path: Path) -> float | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out) or None
    except ValueError:
        return None


def find_raw(d: Path) -> Path | None:
    files = [p for p in d.iterdir() if p.name.startswith("raw.") and p.suffix not in (".part", ".ytdl") and ".f" not in p.stem[3:]]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[0] if files else None


def valid_video(p: Path | None) -> bool:
    if not p or not p.exists() or p.stat().st_size < 50_000:
        return False
    v, _ = ffprobe_codec(p)
    return bool(v)


def run_job(job: dict):
    d = DATA / job["id"]; d.mkdir(parents=True, exist_ok=True); job["dir"] = str(d)
    r = job["req"]
    with SLOTS:
        try:
            wait_for_disk(job)
            job["status"] = "downloading"; job["message"] = "Conectando na plataforma…"
            quality=r.get("quality",720)
            fmt=import_format(quality)
            if r.get("live_minutes"):
                secs = r["live_minutes"] * 60
                g = subprocess.run(ytdlp_base(job["url"]) + ["-g", "-f", f"b[height<={quality}]"+("/b" if quality>=720 else ""), job["url"]],
                                   capture_output=True, text=True, timeout=60)
                if g.returncode != 0 or not g.stdout.strip():
                    raise RuntimeError(friendly_error(g.stderr or g.stdout))
                src = g.stdout.strip().splitlines()[0]
                cmd = ["ffmpeg", "-hide_banner", "-y", "-i", src, "-t", str(secs), "-c", "copy",
                       "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", "raw.mp4"]
                code, log = stream_proc(cmd, job, secs, "Gravando a live…", secs + 300)
            else:
                start, end = float(r.get("start") or 0), float(r["end"])
                tail = ["-f", fmt, "-S", f"res:{quality},vcodec:h264,acodec:m4a", "--merge-output-format", "mp4", "--newline", "-o", "raw.%(ext)s"]
                cmd = ytdlp_base(job["url"]) + tail
                duration=job.get("known_duration")
                native=whole_requested(duration,start,end)
                section=[] if native else ["--download-sections", f"*{start:.2f}-{end:.2f}"]
                timeout=min(MAX_MINUTES*120+600,max(120,(end-start)*2+120))
                code, log = stream_proc(cmd + section + [job["url"]],
                                        job, end - start, "Baixando…", timeout)
                if not valid_video(find_raw(d)) and is_youtube(job["url"]) and is_bot_check(log):
                    # verificação anti-robô do YouTube: tenta de novo com outros clientes do YouTube
                    global _yt_alt_until
                    for p in d.iterdir():
                        if p.name.startswith("raw."):
                            p.unlink(missing_ok=True)
                    cmd = ytdlp_base(job["url"], alt=True) + tail
                    job["progress"]=0
                    code, log = stream_proc(cmd + section + [job["url"]],
                                            job, end - start, "Baixando…", timeout)
                    if valid_video(find_raw(d)):
                        _yt_alt_until = time.time() + 1800
                raw = find_raw(d)
                if not valid_video(raw) and not native and whole_download_allowed(duration,start,end) and not any(x in log.lower() for x in ("sign in","private","drm","not a bot","403","429")):
                    # Only retry with a whole download when its known length is close to the requested range; never fetch hours silently.
                    for p in d.iterdir():
                        if p.name.startswith("raw."):
                            p.unlink(missing_ok=True)
                    job["message"] = "Baixando o vídeo inteiro…"; job["progress"] = 0
                    code, log = stream_proc(cmd + [job["url"]], job, None, "Baixando…", timeout)
                    full = find_raw(d)
                    if valid_video(full):
                        cut = d / "raw.cut.mp4"
                        subprocess.run(["ffmpeg", "-hide_banner", "-y", "-ss", str(start), "-i", str(full), "-t", str(end - start),
                                        "-c", "copy", "-avoid_negative_ts", "make_zero", str(cut)], capture_output=True, timeout=1800)
                        full.unlink(missing_ok=True)
                        cut.rename(d / "raw.mp4")
            raw = find_raw(d)
            if not valid_video(raw):
                raise RuntimeError(friendly_error(log) if code != 0 else "Não consegui baixar o trecho. Tente um intervalo menor ou envie o arquivo original.")
            actual=probe_duration(raw)
            if not actual or not math.isfinite(actual) or actual<=0:
                raise RuntimeError("O vídeo recebido está incompleto.")
            if not r.get("live_minutes"):
                expected=min(end-start,max(0,duration-start)) if duration else end-start
                if code!=0 or actual<max(.5,expected-min(5,expected*.1)) or actual>end-start+10:
                    raise RuntimeError("O download não entregou o trecho completo. Tente um intervalo menor ou envie o arquivo original.")
            job["status"] = "processing"; job["message"] = "Preparando o vídeo…"; job["progress"] = 0
            out = d / "video.mp4"
            if browser_ready_mp4(raw):
                raw.replace(out)
                job.update(status="done",progress=1.0,message="Pronto",file=str(out),size=out.stat().st_size)
                return
            vcodec, has_audio = ffprobe_codec(raw)
            if vcodec in ("h264", "vp9", "av1") and raw.suffix == ".mp4":
                cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(raw), "-c", "copy", "-movflags", "+faststart", str(out)]
            else:  # codecs que o navegador não abre (ex.: HEVC) → converte
                cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(raw), "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
            dur = probe_duration(raw)
            code, log = stream_proc(cmd, job, dur, "Preparando o vídeo…", 3600)
            if code != 0 or not out.exists():
                raise RuntimeError("Falha ao preparar o vídeo: " + friendly_error(log))
            raw.unlink(missing_ok=True)
            job.update(status="done", progress=1.0, message="Pronto", file=str(out), size=out.stat().st_size)
        except subprocess.TimeoutExpired:
            job.update(status="error", error="A plataforma demorou demais para responder.", message="Erro")
        except Exception as e:  # noqa
            job.update(status="error", error=str(e)[:400], message="Erro")
        finally:
            if job["status"]=="error":shutil.rmtree(d,ignore_errors=True)


def free_mb() -> float:
    try:
        return shutil.disk_usage(DATA).free / 1e6
    except Exception:
        return 1e9


def sweep_old(min_age: float):
    now = time.time()
    for jid, job in list(JOBS.items()):
        if now - job["created"] > min_age and job["status"] in ("done", "error"):
            shutil.rmtree(job.get("dir") or (DATA / jid), ignore_errors=True)
            JOBS.pop(jid, None)


def wait_for_disk(job: dict, max_wait: int = 600):
    """Pouco espaço no disco: limpa o que der e espera na fila em vez de falhar no meio do download."""
    if free_mb() >= MIN_FREE_MB:
        return
    sweep_old(300)
    t0 = time.time()
    while free_mb() < MIN_FREE_MB:
        if time.time() - t0 > max_wait:
            raise RuntimeError("O servidor está cheio agora (muita gente baixando ao mesmo tempo). Tente de novo em alguns minutos.")
        job["status"] = "queued"; job["message"] = "Muita gente cortando agora: aguardando espaço no servidor…"
        time.sleep(5)
        sweep_old(300)


def janitor():
    while True:
        time.sleep(120)
        now = time.time()
        for jid, job in list(JOBS.items()):
            if now - job["created"] > FILE_TTL and job["status"] in ("done", "error"):
                shutil.rmtree(job.get("dir") or (DATA / jid), ignore_errors=True)
                JOBS.pop(jid, None)


threading.Thread(target=janitor, daemon=True).start()

# o site (index.html) é servido pelo mesmo servidor
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
