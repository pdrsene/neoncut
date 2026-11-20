"""
NEONCUT — módulo de crescimento (independente: dá para desligar removendo o include_router em app.py).

- Vídeos de demonstração cadastrados pelo admin (arquivos próprios ou autorizados).
- Pesquisa opcional de abandono ("O que impediu você de criar seu corte?").
- Painel do funil completo: page_view → demo → upload → geração → cadastro → download → checkout → compra,
  por aparelho, sistema, navegador e origem, com erros, tempo de processamento, receita e retenção.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections import Counter, defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from . import accounts as A
except ImportError:  # executado como script
    import accounts as A  # type: ignore

router = APIRouter()

DEMO_DIR = A.DB_DIR / "demos"
DEMO_DIR.mkdir(parents=True, exist_ok=True)
DEMO_MAX_MB = int(os.environ.get("DEMO_MAX_MB", "80"))
DEMO_CATS = {"gtarp": "GTA RP • Humor", "acao": "Gameplay • Ação", "podcast": "Podcast • React", "outro": "Exemplo"}
# Gameplays fornecidos para demonstração da plataforma; os uploads do admin têm prioridade.
BUILTIN = [
    {"id": "builtin-drive", "cat": "gtarp", "title": "Distração ao volante", "credit": "Gameplay de demonstração",
     "url": "/demo/drive-demo.mp4", "builtin": True, "has_audio": True, "duration": 24,
     "description": "Teste com gameplay, reação e fala do streamer."},
    {"id": "builtin-robbery", "cat": "gtarp", "title": "Pix durante o assalto", "credit": "Gameplay de demonstração",
     "url": "/demo/robbery-demo.mp4", "builtin": True, "has_audio": True, "duration": 24,
     "description": "Uma situação inusitada com diálogo e reação."},
]

# custos estimados (só para o painel; ajuste no Railway)
MP_FEE_PCT = float(os.environ.get("MP_FEE_PCT", "4.99"))       # taxa média do Mercado Pago (%)
INFRA_MONTH_BRL = float(os.environ.get("INFRA_MONTH_BRL", "30"))  # servidor e domínio por mês (R$)


def init():
    A.db().executescript("""
    CREATE TABLE IF NOT EXISTS demos(
      id TEXT PRIMARY KEY, cat TEXT, title TEXT, credit TEXT, file TEXT, size INTEGER,
      active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0, created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS survey(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, vid TEXT, uid INTEGER,
      reason TEXT, comment TEXT, stage TEXT, dev TEXT, os TEXT, br TEXT, ref TEXT
    );
    """)


init()


# ---------------------------------------------------------------- demonstrações
def _demo_public(r) -> dict:
    return {"id": r["id"], "cat": r["cat"], "cat_name": DEMO_CATS.get(r["cat"], "Exemplo"), "title": r["title"],
            "credit": r["credit"], "url": f"/api/demos/{r['id']}/video", "builtin": False}


@router.get("/api/demos")
def list_demos():
    rows = A.db().execute("SELECT * FROM demos WHERE active=1 ORDER BY sort, created").fetchall()
    items = [_demo_public(r) for r in rows if (DEMO_DIR / r["file"]).exists()]
    if not items:
        items = [dict(b, cat_name=DEMO_CATS[b["cat"]]) for b in BUILTIN]
    return {"demos": items}


@router.get("/api/demos/{did}/video")
def demo_video(did: str):
    r = A.db().execute("SELECT * FROM demos WHERE id=? AND active=1", (did,)).fetchone()
    if not r or not (DEMO_DIR / r["file"]).exists():
        raise HTTPException(404, "Vídeo de demonstração indisponível.")
    return FileResponse(DEMO_DIR / r["file"], media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/admin/demos")
def admin_demos(req: Request):
    A.require_admin(req)
    rows = A.db().execute("SELECT * FROM demos ORDER BY sort, created").fetchall()
    return {"cats": DEMO_CATS, "max_mb": DEMO_MAX_MB,
            "demos": [dict(_demo_public(r), active=bool(r["active"]), size=r["size"]) for r in rows]}


@router.post("/api/admin/demos/upload")
async def admin_demo_upload(req: Request, cat: str = "outro", title: str = "", credit: str = ""):
    """Envio do arquivo no corpo da requisição (sem multipart). Só vídeos próprios ou autorizados."""
    A.require_admin(req)
    if cat not in DEMO_CATS:
        raise HTTPException(400, "Categoria inválida.")
    title = title.strip()[:80]
    credit = credit.strip()[:120]
    if not title or not credit:
        raise HTTPException(400, "Informe o título e a autorização/crédito do vídeo.")
    did = secrets.token_hex(6)
    path = DEMO_DIR / f"{did}.mp4"
    size, limit = 0, DEMO_MAX_MB * 1_000_000
    with open(path, "wb") as f:
        async for chunk in req.stream():
            size += len(chunk)
            if size > limit:
                f.close(); path.unlink(missing_ok=True)
                raise HTTPException(413, f"Arquivo maior que {DEMO_MAX_MB} MB. Corte o vídeo antes de enviar.")
            f.write(chunk)
    if size < 50_000:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "Arquivo vazio ou inválido.")
    A.db().execute("INSERT INTO demos(id,cat,title,credit,file,size,active,sort,created) VALUES(?,?,?,?,?,?,1,0,?)",
                   (did, cat, title, credit, path.name, size, time.time()))
    return {"ok": True, "id": did}


class DemoUpd(BaseModel):
    id: str
    active: bool | None = None
    delete: bool = False
    title: str | None = None
    credit: str | None = None
    cat: str | None = None


@router.post("/api/admin/demos/update")
def admin_demo_update(body: DemoUpd, req: Request):
    A.require_admin(req)
    r = A.db().execute("SELECT * FROM demos WHERE id=?", (body.id,)).fetchone()
    if not r:
        raise HTTPException(404, "Demonstração não encontrada.")
    if body.delete:
        (DEMO_DIR / r["file"]).unlink(missing_ok=True)
        A.db().execute("DELETE FROM demos WHERE id=?", (body.id,))
        return {"ok": True}
    if body.active is not None:
        A.db().execute("UPDATE demos SET active=? WHERE id=?", (1 if body.active else 0, body.id))
    if body.title is not None:
        A.db().execute("UPDATE demos SET title=? WHERE id=?", (body.title.strip()[:80], body.id))
    if body.credit is not None:
        A.db().execute("UPDATE demos SET credit=? WHERE id=?", (body.credit.strip()[:120], body.id))
    if body.cat in DEMO_CATS:
        A.db().execute("UPDATE demos SET cat=? WHERE id=?", (body.cat, body.id))
    return {"ok": True}


# ---------------------------------------------------------------- pesquisa de abandono
SURVEY_REASONS = {
    "sem_video": "Não tenho um vídeo para testar",
    "problema": "Tive um problema com a ferramenta",
    "celular": "Não consegui usar pelo celular",
    "nao_entendi": "Não entendi como funciona",
    "preco": "Achei o preço ou os limites pouco interessantes",
    "outro": "Outro motivo",
}


class SurveyReq(BaseModel):
    reason: str
    comment: str = ""
    stage: str = ""
    vid: str = ""
    ref: str = ""


@router.post("/api/survey")
def survey(body: SurveyReq, req: Request):
    A.throttle("survey:" + (req.client.host if req.client else "?"), 10, 3600)
    if body.reason not in SURVEY_REASONS:
        raise HTTPException(400, "Escolha uma opção.")
    u = A.current_user(req, required=False)
    ua = A.ua_info(req.headers.get("user-agent", ""))
    comment = re.sub(r"https?://\S+", "[url]", (body.comment or "").strip())[:500]
    A.db().execute("INSERT INTO survey(ts,vid,uid,reason,comment,stage,dev,os,br,ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), "".join(ch for ch in body.vid if ch.isalnum())[:40], u["id"] if u else None, body.reason,
                    comment, re.sub(r"[^a-z_]", "", body.stage.lower())[:30], ua["dev"], ua["os"], ua["br"], A._clean_ref(body.ref)))
    return {"ok": True}


# ---------------------------------------------------------------- painel do funil
FUNNEL = [
    ("page_view", "Visitaram", ("visit", "link", "page_view")),
    ("demo_started", "Abriram a demonstração", ("demo_started",)),
    ("source_selected", "Escolheram a origem", ("source_selected",)),
    ("video_uploaded", "Enviaram vídeo/link", ("video_uploaded",)),
    ("generation_started", "Começaram a gerar", ("generation_started",)),
    ("generation_completed", "Receberam cortes", ("generation_completed", "clip_generated")),
    ("signup_started", "Abriram o cadastro", ("signup_started", "signup_open")),
    ("sign_up", "Criaram conta", ("sign_up", "signup")),
    ("export_started", "Tentaram baixar", ("export_started",)),
    ("download_completed", "Baixaram", ("download_completed", "export")),
    ("begin_checkout", "Abriram o pagamento", ("begin_checkout", "checkout")),
    ("purchase", "Compraram", ("purchase",)),
]
KIND_TO_STEP = {k: step for step, _, ks in FUNNEL for k in ks}


@router.get("/api/admin/funnel")
def admin_funnel(req: Request, days: int = 30):
    A.require_admin(req)
    days = max(1, min(365, days))
    now = time.time()
    since = now - days * 86400
    c = A.db()
    rows = c.execute("SELECT ts, kind, vid, uid, ref, meta FROM events WHERE ts>=? ORDER BY ts", (since - 8 * 86400,)).fetchall()

    # liga contas a visitantes (eventos do servidor, como cadastro e compra, não têm o id do visitante)
    uid2vid: dict[int, str] = {}
    vinfo: dict[str, dict] = {}
    for r in rows:
        if r["vid"] and r["uid"] and r["uid"] not in uid2vid:
            uid2vid[r["uid"]] = r["vid"]
    for r in rows:
        v = r["vid"] or (uid2vid.get(r["uid"]) if r["uid"] else None)
        if not v:
            continue
        try:
            m = json.loads(r["meta"] or "{}")
        except Exception:
            m = {}
        d = vinfo.setdefault(v, {"dev": None, "os": None, "br": None, "src": None, "first": r["ts"]})
        for k in ("dev", "os", "br"):
            if not d[k] and m.get(k):
                d[k] = m[k] if k != "br" else ("interno: " + m[k] if m.get("inapp") else m[k])
        if not d["src"] and r["ref"]:
            d["src"] = r["ref"]

    steps_by_vid: dict[str, set] = defaultdict(set)
    errors, err_stage, proc_ms, export_fail = Counter(), Counter(), [], Counter()
    sessions = set()
    sources_utm = Counter()
    revenue, purchases = 0.0, 0
    tx_seen = set()
    for r in rows:
        if r["ts"] < since:
            continue
        v = r["vid"] or (uid2vid.get(r["uid"]) if r["uid"] else None) or (f"u{r['uid']}" if r["uid"] else None)
        try:
            m = json.loads(r["meta"] or "{}")
        except Exception:
            m = {}
        step = KIND_TO_STEP.get(r["kind"])
        if step == "purchase":
            tx = m.get("transaction_id")
            if tx in tx_seen:
                continue
            tx_seen.add(tx)
            revenue += float(m.get("value") or 0)
            purchases += 1
        if step and v:
            steps_by_vid[v].add(step)
        if r["kind"] in ("visit", "link", "page_view") and r["vid"]:
            sessions.add((r["vid"], int(r["ts"] // 1800)))
            if m.get("utm_campaign"):
                sources_utm[f"{m.get('utm_source') or r['ref'] or '?'} / {m.get('utm_campaign')}"] += 1
        if r["kind"] == "generation_failed":
            errors[(m.get("code") or "desconhecido") + (": " + m["msg"] if m.get("msg") else "")] += 1
            err_stage[m.get("stage") or "?"] += 1
        if r["kind"] == "export_failed":
            export_fail[(m.get("code") or "desconhecido") + (": " + m["msg"] if m.get("msg") else "")] += 1
        if r["kind"] == "js_error":
            errors["js: " + (m.get("msg") or "?")] += 1
        if r["kind"] in ("generation_completed",) and isinstance(m.get("ms"), (int, float)):
            proc_ms.append(m["ms"])

    # quem fez cadastro/compra mas não tem eventos no período ainda conta (vem do banco)
    def count(step):
        return sum(1 for s in steps_by_vid.values() if step in s)

    funnel = [{"step": s, "label": lbl, "n": count(s)} for s, lbl, _ in FUNNEL]

    def seg(key):
        out: dict[str, dict] = {}
        for v, st in steps_by_vid.items():
            info = vinfo.get(v, {})
            k = info.get(key) or ("direto" if key == "src" else "?")
            d = out.setdefault(k, {"key": k, "visitors": 0, "generated": 0, "signups": 0, "downloads": 0, "purchases": 0})
            if "page_view" in st or st:
                d["visitors"] += 1
            d["generated"] += "generation_completed" in st
            d["signups"] += "sign_up" in st
            d["downloads"] += "download_completed" in st
            d["purchases"] += "purchase" in st
        return sorted(out.values(), key=lambda d: -d["visitors"])

    started = count("generation_started")
    completed = count("generation_completed")
    failed_v = sum(1 for v, s in steps_by_vid.items() if "generation_started" in s and "generation_completed" not in s)

    # retenção: visitantes vistos pela 1ª vez no período que voltaram 1 dia depois (D1) ou entre 1 e 7 dias (D7)
    first_seen: dict[str, float] = {}
    seen_ts: dict[str, list] = defaultdict(list)
    for r in c.execute("SELECT vid, ts FROM events WHERE vid!='' AND kind IN ('visit','link','page_view') ORDER BY ts"):
        first_seen.setdefault(r["vid"], r["ts"])
        seen_ts[r["vid"]].append(r["ts"])
    def ret(lo, hi):
        cohort = [v for v, t in first_seen.items() if since <= t <= now - hi]
        back = sum(1 for v in cohort if any(lo <= t - first_seen[v] < hi for t in seen_ts[v]))
        return {"cohort": len(cohort), "back": back, "pct": round(100 * back / len(cohort)) if cohort else None}

    fee = revenue * MP_FEE_PCT / 100
    infra = INFRA_MONTH_BRL * days / 30
    sv = c.execute("SELECT reason, COUNT(*) n FROM survey WHERE ts>=? GROUP BY reason ORDER BY n DESC", (since,)).fetchall()
    sv_list = c.execute("SELECT * FROM survey ORDER BY ts DESC LIMIT 30").fetchall()
    shown = c.execute("SELECT COUNT(*) FROM events WHERE kind='survey_shown' AND ts>=?", (since,)).fetchone()[0]
    return {
        "days": days,
        "visitors": count("page_view") or len(steps_by_vid),
        "sessions": len(sessions),
        "funnel": funnel,
        "by_device": seg("dev"), "by_os": seg("os"), "by_browser": seg("br"), "by_source": seg("src"),
        "utm_campaigns": [{"k": k, "n": n} for k, n in sources_utm.most_common(10)],
        "generation": {"started": started, "completed": completed, "failed_users": failed_v,
                       "success_pct": round(100 * completed / started) if started else None,
                       "avg_ms": round(sum(proc_ms) / len(proc_ms)) if proc_ms else None,
                       "median_ms": sorted(proc_ms)[len(proc_ms) // 2] if proc_ms else None},
        "errors": [{"k": k, "n": n} for k, n in errors.most_common(12)],
        "error_stages": [{"k": k, "n": n} for k, n in err_stage.most_common()],
        "export_errors": [{"k": k, "n": n} for k, n in export_fail.most_common(8)],
        "money": {"purchases": purchases, "revenue": round(revenue, 2), "fees_est": round(fee, 2), "infra_est": round(infra, 2),
                  "result_est": round(revenue - fee - infra, 2), "fee_pct": MP_FEE_PCT, "infra_month": INFRA_MONTH_BRL},
        "retention": {"d1": ret(86400, 2 * 86400), "d7": ret(86400, 8 * 86400)},
        "survey": {"shown": shown, "answers": [{"k": SURVEY_REASONS.get(r["reason"], r["reason"]), "n": r["n"]} for r in sv],
                   "list": [{"ts": r["ts"], "reason": SURVEY_REASONS.get(r["reason"], r["reason"]), "comment": r["comment"],
                             "stage": r["stage"], "dev": r["dev"], "os": r["os"], "br": r["br"], "ref": r["ref"]} for r in sv_list]},
    }
