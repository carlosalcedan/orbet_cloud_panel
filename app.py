# app.py — ORBET Cloud Panel + Tickets Vehiculares
# Funciones:
# - Login + Roles (admin/cliente)
# - Multi-tenant por usuario (carpetas por owner)
# - /files protegido
# - Filtros, categorías, paginación, export CSV
# - Tickets vehiculares vinculados a capturas:
#   * Capturas sin ticket => estado "PENDIENTE" (🔴)
#   * Capturas con ticket => estado "COMPLETO" (🟢 Ticket #N)
#   * Formulario de ticket al hacer clic en la foto
#   * Numeración diaria por owner + fecha
#   * Impresión automática al guardar y botón de reimpresión
# - Reporte diario HTML de tickets
#
# Requisitos: fastapi uvicorn python-multipart itsdangerous Pillow
#
# Variables de entorno recomendadas (Render):
#   SECRET_KEY       = <cadena larga aleatoria>
#   USERS            = "admin:admin123:admin,cliente1:1234:cliente"
#   ORBET_TOKEN      = "ORBET_2025_Seguridad_ARES"
#   ORBET_TOKENS     = "admin:AAA111,cliente1:BBB222,cliente2:CCC333"
#   DATA_DIR         = "data"
#   RETENTION_DAYS   = "0"   # 0 = sin limpieza, o días a retener
#
# Subida desde ORBET local:
#   POST /upload (multipart/form-data)
#     token=<token del cliente>
#     kind=captura|ticket
#     owner=<usuario destino>
#     file=@archivo.jpg

import os, io, csv, time, shutil, sqlite3
from pathlib import Path
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "ORBET Cloud Panel"

# -------------------- Config --------------------
ORBET_TOKEN = os.getenv("ORBET_TOKEN", "ORBET_2025_Seguridad_ARES")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "0"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()

# BD de tickets vehiculares (va dentro de DATA_DIR)
DB_FILE = DATA_DIR / "tickets_vehiculares.db"

# Login + Roles
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret_in_render")
USERS_ENV = os.getenv("USERS", "admin:admin123:admin")  # "user:pass[:rol],user2:pass[:rol]"
USERS: Dict[str, Dict[str, str]] = {}
for pair in [p.strip() for p in USERS_ENV.split(",") if p.strip()]:
    parts = pair.split(":")
    if len(parts) >= 2:
        u, p = parts[0].strip(), parts[1].strip()
        role = (parts[2].strip().lower() if len(parts) >= 3 else "admin")
        if role not in ("admin", "cliente"):
            role = "admin"
        USERS[u] = {"password": p, "role": role}

# Tokens por owner (multi-cliente)
ORBET_TOKENS_ENV = os.getenv("ORBET_TOKENS", "").strip()
TOKENS: Dict[str, str] = {}
if ORBET_TOKENS_ENV:
    for pair in [p.strip() for p in ORBET_TOKENS_ENV.split(",") if p.strip()]:
        parts = pair.split(":")
        if len(parts) >= 2:
            owner_name = parts[0].strip()
            tok = parts[1].strip()
            TOKENS[owner_name] = tok

# -------------------- Utils & FS --------------------
def ensure_base_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def secure_name(name: str) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(c if c in keep else "_" for c in name)

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _category_from_name(name: str) -> str:
    n = name.lower()
    if "persona" in n: return "PERSONA"
    if any(x in n for x in ("auto", "car", "vehic", "truck", "bus", "camion")): return "AUTO"
    if any(x in n for x in ("animal", "dog", "cat", "bird")): return "ANIMAL"
    return "OTROS"

def is_image(filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))

def make_thumb(dest: Path):
    if not is_image(dest.name):
        return
    try:
        from PIL import Image
        im = Image.open(dest)
        im.thumbnail((480, 480))
        thumb = dest.with_suffix(dest.suffix + ".thumb.jpg")
        im.save(thumb, quality=80)
    except Exception:
        pass

def cleanup_old(days: int):
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for owner_dir in DATA_DIR.iterdir():
        if not owner_dir.is_dir():
            continue
        for kind_dir in owner_dir.iterdir():   # capturas / tickets
            if not kind_dir.is_dir():
                continue
            for day_dir in kind_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    ts = time.mktime(time.strptime(day_dir.name, "%Y-%m-%d"))
                except Exception:
                    continue
                if ts < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)

def owner_root(owner: str) -> Path:
    p = DATA_DIR / secure_name(owner)
    p.mkdir(parents=True, exist_ok=True)
    return p

def today_dir(owner: str, kind: str) -> Path:
    base = owner_root(owner) / ("capturas" if kind == "capturas" else "tickets")
    day = datetime.now().strftime("%Y-%m-%d")
    p = base / day
    p.mkdir(parents=True, exist_ok=True)
    return p

def list_items(owner: str, kind: str, start: Optional[date], end: Optional[date],
               q: Optional[str], cat: Optional[str]) -> List[Dict]:
    base = owner_root(owner) / ("capturas" if kind == "capturas" else "tickets")
    qnorm = (q or "").strip().lower()
    cat = (cat or "").strip().upper()
    items: List[Dict] = []

    if not base.exists():
        return items

    for day_dir in sorted([d for d in base.iterdir() if d.is_dir()], reverse=True):
        try:
            d = parse_ymd(day_dir.name)
        except Exception:
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue

        for f in sorted([x for x in day_dir.iterdir() if x.is_file()], reverse=True):
            name = f.name
            c = _category_from_name(name)
            if cat and c != cat:
                continue
            if qnorm:
                if qnorm not in name.lower() and qnorm not in c.lower():
                    continue
            items.append({
                "date": d.isoformat(),
                "name": name,
                "url": f"/files/{secure_name(owner)}/{kind}/{day_dir.name}/{name}",
                "kind": kind,
                "cat": c,
                "owner": owner
            })
    return items

# -------------------- BD TICKETS VEHICULARES --------------------
def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets_vehiculares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            captura_owner TEXT NOT NULL,
            captura_kind TEXT NOT NULL,
            captura_day TEXT NOT NULL,
            captura_name TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            n_corr_dia INTEGER NOT NULL,
            tipo_mov TEXT,
            placa TEXT,
            chofer TEXT,
            dni TEXT,
            empresa TEXT,
            guia TEXT,
            material TEXT,
            observaciones TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def _db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_ticket_for_capture(owner: str, kind: str, day: str, name: str) -> Optional[Dict]:
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM tickets_vehiculares
        WHERE captura_owner=? AND captura_kind=? AND captura_day=? AND captura_name=?
        ORDER BY id DESC LIMIT 1
        """,
        (owner, kind, day, name),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def attach_tickets(owner: str, kind: str, items: List[Dict], status: Optional[str] = None) -> List[Dict]:
    status = (status or "").lower()
    out: List[Dict] = []
    for it in items:
        t = get_ticket_for_capture(owner, kind, it["date"], it["name"])
        if t:
            it["ticket"] = {
                "id": t["id"],
                "n_corr_dia": t["n_corr_dia"],
                "placa": t.get("placa") or "",
                "chofer": t.get("chofer") or "",
                "tipo_mov": t.get("tipo_mov") or "",
                "guia": t.get("guia") or "",
            }
        else:
            it["ticket"] = None

        if status == "pendientes" and it["ticket"] is not None:
            continue
        if status == "completos" and it["ticket"] is None:
            continue
        out.append(it)
    return out

def delete_tickets_for_capture(owner: str, kind: str, day: str, name: str):
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM tickets_vehiculares
        WHERE captura_owner=? AND captura_kind=? AND captura_day=? AND captura_name=?
        """,
        (owner, kind, day, name),
    )
    conn.commit()
    conn.close()

# -------------------- Token validation --------------------
def _validate_token(owner: str, token: str):
    owner = owner.strip()
    if TOKENS:  # modo multi-cliente por owner
        expected = TOKENS.get(owner)
        if not expected or expected != token:
            raise HTTPException(status_code=401, detail="Token inválido para este owner")
    else:       # modo global antiguo
        if token != ORBET_TOKEN:
            raise HTTPException(status_code=401, detail="Token inválido")

# -------------------- App & Auth --------------------
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60*60*12)  # 12h

@app.on_event("startup")
def _on_start():
    ensure_base_dirs()
    init_db()
    try:
        cleanup_old(RETENTION_DAYS)
    except Exception:
        pass

def current_user(req: Request) -> Optional[str]:
    return req.session.get("user")

def current_role(req: Request) -> Optional[str]:
    return req.session.get("role")

def require_login(req: Request):
    if not current_user(req):
        raise HTTPException(status_code=401, detail="No autorizado")

def require_role(req: Request, allowed: Tuple[str, ...]):
    require_login(req)
    role = current_role(req)
    if role not in allowed:
        raise HTTPException(status_code=403, detail="Permisos insuficientes")

# -------------------- Login / Logout --------------------
LOGIN_HTML = """
<!doctype html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ingresar – ORBET</title>
<style>
body{{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:22px;width:360px;box-shadow:0 6px 18px rgba(0,0,0,.25)}}
h1{{font-size:20px;margin:0 0 12px}}
label{{display:block;font-size:12px;opacity:.9;margin:8px 0 4px}}
input,button{{width:100%;background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:10px}}
button{{cursor:pointer;margin-top:10px}}
.msg{{color:#ff9b9b;font-size:12px;margin:8px 0}}
.small{{opacity:.8;font-size:12px;margin-top:8px}}
</style>
</head><body>
  <form class="card" method="post" action="/login">
    <h1>ORBET – Ingresar</h1>
    <label>Usuario</label>
    <input name="username" autocomplete="username" required>
    <label>Contraseña</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Entrar</button>
    {msg}
    <div class="small">Acceso protegido • Sesión 12h</div>
  </form>
</body></html>
"""

@app.get("/login", response_class=HTMLResponse)
def login_form():
    return LOGIN_HTML.replace("{msg}", "")

@app.post("/login")
def login(req: Request, username: str = Form(...), password: str = Form(...)):
    info = USERS.get(username)
    if info and info["password"] == password:
        req.session["user"] = username
        req.session["role"] = info["role"]
        return RedirectResponse(url="/panel", status_code=302)
    html = LOGIN_HTML.replace("{msg}", '<div class="msg">Usuario o contraseña inválidos</div>')
    return HTMLResponse(html, status_code=401)

@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse(url="/login", status_code=302)

@app.get("/me")
def me(req: Request):
    require_login(req)
    return {"user": current_user(req), "role": current_role(req)}

# -------------------- Health --------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": APP_NAME}

# -------------------- Upload (token, no requiere login) --------------------
@app.post("/upload")
async def upload(
    token: str = Form(...),
    kind: str = Form(...),  # "captura" | "ticket"
    file: UploadFile = File(...),
    owner: str = Form("admin"),
    background: BackgroundTasks = None,
):
    _validate_token(owner, token)

    if kind not in ("captura", "ticket"):
        raise HTTPException(status_code=400, detail="kind debe ser 'captura' o 'ticket'")

    kind_dir = "capturas" if kind == "captura" else "tickets"
    target_dir = today_dir(owner, kind_dir)

    original = secure_name(file.filename or f"file_{int(time.time())}")
    stamp = datetime.now().strftime("%H-%M-%S")
    dest = target_dir / f"{stamp}_{original}"

    data = await file.read()
    with open(dest, "wb") as fh:
        fh.write(data)

    if background:
        background.add_task(make_thumb, dest)
    else:
        make_thumb(dest)

    url = f"/files/{secure_name(owner)}/{kind_dir}/{target_dir.name}/{dest.name}"
    return {"ok": True, "url": url, "name": dest.name, "owner": owner}

# -------------------- /files protegido (requiere login) --------------------
def _safe_path(owner: str, kind: str, day: str, name: str) -> Path:
    if kind not in ("capturas", "tickets"):
        raise HTTPException(status_code=404, detail="No encontrado")
    owner = secure_name(owner)
    day = secure_name(day)
    name = secure_name(name)
    base = DATA_DIR / owner / kind
    path = (base / day / name).resolve()
    if not str(path).startswith(str((DATA_DIR / owner).resolve())):
        raise HTTPException(status_code=403, detail="Ruta inválida")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return path

def _can_access_owner(req: Request, owner: str) -> bool:
    role = current_role(req)
    user = current_user(req)
    if role == "admin":
        return True
    return owner == user

@app.get("/files/{owner}/{kind}/{day}/{name}")
def get_file(req: Request, owner: str, kind: str, day: str, name: str):
    require_login(req)
    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    path = _safe_path(owner, kind, day, name)
    media = "application/octet-stream"
    if is_image(path.name):
        media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(path, media_type=media)

# -------------------- API list (requiere login) --------------------
@app.get("/api/list", summary="Lista archivos con filtros, categoría, estado y paginación")
def api_list(
    req: Request,
    owner: Optional[str] = Query(None, description="Owner (solo admin; si no, se usa el propio usuario)"),
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    cat: Optional[str] = Query(None, description="PERSONA|AUTO|ANIMAL|OTROS"),
    status: Optional[str] = Query(None, description="pendientes|completos"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    require_login(req)
    role = current_role(req)
    user = current_user(req)

    if role != "admin":
        owner = user
    else:
        owner = owner or user

    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None

    items_all = list_items(owner, kind, s, e, q, cat)
    items_all = attach_tickets(owner, kind, items_all, status=status)
    total = len(items_all)

    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    items = items_all[start_idx:end_idx]

    counts = {"PERSONA": 0, "AUTO": 0, "ANIMAL": 0, "OTROS": 0}
    for it in items_all:
        counts[it["cat"]] = counts.get(it["cat"], 0) + 1

    return {
        "ok": True,
        "owner": owner,
        "count": len(items),
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "items": items,
        "counts": counts
    }

# -------------------- Export CSV (requiere login) --------------------
@app.get("/api/export.csv")
def export_csv(
    req: Request,
    owner: Optional[str] = Query(None),
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    cat: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    require_login(req)
    role = current_role(req)
    user = current_user(req)
    if role != "admin":
        owner = user
    else:
        owner = owner or user

    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None
    items = list_items(owner, kind, s, e, q, cat)
    items = attach_tickets(owner, kind, items, status=status)

    def gen():
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow([
            "owner", "fecha", "tipo_archivo", "categoria", "nombre_archivo", "url",
            "ticket_id", "n_corr_dia", "tipo_mov", "placa", "chofer", "guia", "material", "observaciones"
        ])
        for it in items:
            t = it.get("ticket") or {}
            writer.writerow([
                it["owner"], it["date"], it["kind"], it["cat"], it["name"], it["url"],
                t.get("id") or "",
                t.get("n_corr_dia") or "",
                t.get("tipo_mov") or "",
                t.get("placa") or "",
                t.get("chofer") or "",
                t.get("guia") or "",
                t.get("material") or "",
                t.get("observaciones") or "",
            ])
        yield sio.getvalue()

    headers = {"Content-Disposition": f'attachment; filename="orbet_{owner}_{kind}.csv"'}
    return StreamingResponse(gen(), headers=headers, media_type="text/csv; charset=utf-8")

# -------------------- Delete (solo admin) --------------------
@app.delete("/api/delete")
def api_delete(
    req: Request,
    owner: str = Query(...),
    kind: str = Query(..., pattern="^(capturas|tickets)$"),
    day: str = Query(...),
    name: str = Query(...),
):
    require_role(req, ("admin",))
    path = _safe_path(owner, kind, day, name)
    try:
        path.unlink(missing_ok=True)
        thumb = path.with_suffix(path.suffix + ".thumb.jpg")
        thumb.unlink(missing_ok=True)
        delete_tickets_for_capture(owner, kind, day, name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo borrar: {e}")
    return {"ok": True}

# -------------------- UI: Vista rápida (requiere login) --------------------
INDEX_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORBET – Galería rápida</title>
<style>
body{{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}}
.wrap{{max-width:1100px;margin:auto}}
h1{{font-size:22px;margin:0 0 12px}}
.card{{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}}
.row{{display:flex;gap:12px;flex-wrap:wrap;align-items:center}}
select,input,button{{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:6px 10px}}
.badge{{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.item{{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease}}
.item.show{{opacity:1}}
.item.pendiente{{border-color:#ff4d4f}}
.item.completo{{border-color:#2ecc71}}
.item img{{width:100%;display:block;border-radius:10px}}
.meta{{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px;align-items:center}}
.btn{{background:#24305a;border:none;border-radius:8px;padding:4px 8px;color:#e7e7ee;text-decoration:none;cursor:pointer}}
a{{color:#9ecbff}}
.right{{margin-left:auto}}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería en vivo <span id="who"></span> <a class="right" href="/logout">Salir</a></h1>
  <div class="card">
    <div class="row">
      <label>Owner <input id="owner" placeholder="(admin puede cambiar)"></label>
      <label>Tipo
        <select id="kind"><option value="capturas">Capturas</option><option value="tickets">Tickets</option></select>
      </label>
      <label>Categoría
        <select id="cat"><option value="">Todas</option><option>PERSONA</option><option>AUTO</option><option>ANIMAL</option><option>OTROS</option></select>
      </label>
      <label>Estado
        <select id="status"><option value="">Todos</option><option value="pendientes">Solo pendientes</option><option value="completos">Solo completos</option></select>
      </label>
      <span id="count" class="badge">0</span>
      <a href="/panel">Ir al Panel con filtros</a>
    </div>
  </div>
  <div class="card"><div id="grid" class="grid"></div></div>
  <div class="card">
    <div class="row">
      <button id="prev">⬅ Anterior</button>
      <span id="pageInfo" class="badge">1/1</span>
      <button id="next">Siguiente ➡</button>
    </div>
  </div>
</div>
<script>
let canDelete=false, myRole="cliente", myUser="";
const $=s=>document.querySelector(s);
const grid=$("#grid"), countEl=$("#count"), kind=$("#kind"), who=$("#who"), cat=$("#cat");
const owner=$("#owner"), prev=$("#prev"), next=$("#next"), pageInfo=$("#pageInfo"), statusSel=$("#status");
let page=1, pages=1, limit=50;

const cardId=url=>"i_"+btoa(url).replace(/=/g,"");

function buildBadge(it){
  if(it.ticket && it.ticket.id){
    const n = it.ticket.n_corr_dia || "";
    return "🟢 Ticket #"+n;
  }
  return "🔴 Sin ticket";
}
function buildPrintBtn(it){
  if(it.ticket && it.ticket.id){
    return `<a class="btn" target="_blank" href="/ticket/print/${it.ticket.id}">🧾</a>`;
  }
  return "";
}
function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  const hasTicket = !!(it.ticket && it.ticket.id);
  const badge = buildBadge(it);
  const isImg=/\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
  const thumb=isImg?`<img loading="lazy" src="${it.url}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
  const delBtn = canDelete? `<button class="btn" data-del="${it.url}" data-owner="${it.owner}" data-kind="${it.kind}" data-date="${it.date}" data-name="${it.name}">🗑</button>` : "";
  const printBtn = buildPrintBtn(it);
  const ticketLink = `<a class="btn" href="/ticket/form?owner=${encodeURIComponent(it.owner)}&kind=${it.kind}&day=${it.date}&name=${encodeURIComponent(it.name)}">Ticket</a>`;
  if(!el){
    el=document.createElement("div");
    el.id=id;
    grid.appendChild(el);
  }
  el.className="item "+(hasTicket?"completo":"pendiente");
  el.innerHTML=thumb+`<div class="meta"><span>${it.date} · ${it.cat} · ${badge}</span><div><a class="btn" target="_blank" href="${it.url}">Abrir</a> ${ticketLink} ${printBtn} ${delBtn}</div></div>`;
  requestAnimationFrame(()=>el.classList.add("show"));
  return el;
}
function bindDeletes(){
  if(!canDelete) return;
  grid.querySelectorAll("[data-del]").forEach(btn=>{
    btn.onclick=async ()=>{
      if(!confirm("¿Borrar este archivo (y sus tickets)?")) return;
      const qs=new URLSearchParams({
        owner: btn.dataset.owner,
        kind: btn.dataset.kind,
        day: btn.dataset.date,
        name: btn.dataset.name
      });
      const r=await fetch("/api/delete?"+qs.toString(), {method:"DELETE"});
      if(r.ok){ btn.closest(".item")?.remove(); }
    };
  });
}
function diffRender(items){
  grid.innerHTML="";
  items.forEach(it=>ensureCard(it));
  bindDeletes();
}
async function load(){
  const p=new URLSearchParams();
  p.set("kind", kind.value);
  p.set("page", String(page));
  p.set("limit", String(limit));
  if(cat.value) p.set("cat", cat.value);
  if(owner.value) p.set("owner", owner.value);
  if(statusSel.value) p.set("status", statusSel.value);

  const r=await fetch("/api/list?"+p.toString());
  if(r.status==401){ location.href="/login"; return; }
  const j=await r.json();
  countEl.textContent=j.total;
  pages=j.pages||1; page=j.page||1;
  pageInfo.textContent=`${page}/${pages}`;
  diffRender(j.items||[]);
}
async function boot(){
  const me=await fetch("/me"); if(me.status==401){ location.href="/login"; return; }
  const info=await me.json(); myRole=info.role; myUser=info.user;
  canDelete=(myRole==="admin");
  who.textContent=`· ${myUser} (${myRole})`;
  if(myRole!=="admin"){ owner.value=myUser; owner.disabled=true; }
  await load(); setInterval(load, 5000);
}
kind.addEventListener("change", ()=>{ page=1; load(); });
cat.addEventListener("change", ()=>{ page=1; load(); });
statusSel.addEventListener("change", ()=>{ page=1; load(); });
owner.addEventListener("change", ()=>{ page=1; load(); });
prev.addEventListener("click", ()=>{ if(page>1){ page--; load(); }});
next.addEventListener("click", ()=>{ if(page<pages){ page++; load(); }});
boot();
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
def index(req: Request):
    require_login(req)
    return INDEX_HTML

# -------------------- UI: Panel con filtros (requiere login) --------------------
PANEL_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORBET – Galería con filtros</title>
<style>
body{{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}}
.wrap{{max-width:1200px;margin:auto}}
h1{{font-size:26px;margin:0 0 14px}}
.card{{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}}
.row{{display:flex;gap:12px;flex-wrap:wrap;align-items:end}}
.row label{{display:flex;flex-direction:column;font-size:12px;opacity:.9}}
input,select,button{{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}}
button{{cursor:pointer}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
.item{{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease}}
.item.show{{opacity:1}}
.item.pendiente{{border-color:#ff4d4f}}
.item.completo{{border-color:#2ecc71}}
.item img{{width:100%;display:block;border-radius:10px}}
.meta{{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px;align-items:center}}
.badge{{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}}
.stats{{display:flex;gap:8px;flex-wrap:wrap}}
.btn{{background:#24305a;border:none;border-radius:8px;padding:4px 8px;color:#e7e7ee;text-decoration:none;cursor:pointer}}
a{{color:#9ecbff}}
.right{{margin-left:auto}}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería con filtros <span id="who"></span> <a class="right" href="/logout">Salir</a></h1>

  <div class="card">
    <form id="f" class="row">
      <label>Owner <input id="owner" placeholder="(admin puede cambiar)"></label>
      <label>Tipo
        <select id="kind">
          <option value="capturas">Capturas</option>
          <option value="tickets">Tickets</option>
        </select>
      </label>
      <label>Categoría
        <select id="cat">
          <option value="">Todas</option><option>PERSONA</option><option>AUTO</option><option>ANIMAL</option><option>OTROS</option>
        </select>
      </label>
      <label>Estado
        <select id="status">
          <option value="">Todos</option>
          <option value="pendientes">Solo pendientes</option>
          <option value="completos">Solo completos</option>
        </select>
      </label>
      <label>Desde <input type="date" id="start"></label>
      <label>Hasta <input type="date" id="end"></label>
      <label>Buscar (texto) <input type="text" id="q" placeholder="placa / guía / texto"></label>
      <label>Página <input type="number" id="page" min="1" value="1" style="width:90px"></label>
      <label>Por página
        <select id="limit"><option>25</option><option selected>50</option><option>100</option><option>200</option></select>
      </label>
      <button type="submit">Aplicar</button>
      <a href="/">Vista rápida</a>
      <a id="dl" href="#" download>⬇️ CSV</a>
      <a id="repDia" href="#">📄 Reporte diario</a>
      <span id="count" class="badge">0</span>
      <span id="pages" class="badge">1/1</span>
    </form>
  </div>

  <div class="card">
    <div class="stats">
      <span class="badge" id="cP">PERSONA: 0</span>
      <span class="badge" id="cA">AUTO: 0</span>
      <span class="badge" id="cN">ANIMAL: 0</span>
      <span class="badge" id="cO">OTROS: 0</span>
    </div>
  </div>

  <div class="card"><div id="grid" class="grid"></div></div>
</div>

<script>
let canDelete=false, myRole="cliente", myUser="";
const $=sel=>document.querySelector(sel);
const grid=$("#grid"), countEl=$("#count"), pagesEl=$("#pages"), dl=$("#dl"), repDia=$("#repDia");
const kind=$("#kind"), start=$("#start"), end=$("#end"), q=$("#q"), cat=$("#cat"), statusSel=$("#status");
const owner=$("#owner"), page=$("#page"), limit=$("#limit");
const cP=$("#cP"), cA=$("#cA"), cN=$("#cN"), cO=$("#cO");

const cardId=url=>"i_"+btoa(url).replace(/=/g,"");

function buildBadge(it){
  if(it.ticket && it.ticket.id){
    const n = it.ticket.n_corr_dia || "";
    return "🟢 Ticket #"+n;
  }
  return "🔴 Sin ticket";
}
function buildPrintBtn(it){
  if(it.ticket && it.ticket.id){
    return `<a class="btn" target="_blank" href="/ticket/print/${it.ticket.id}">🧾</a>`;
  }
  return "";
}
function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  const hasTicket = !!(it.ticket && it.ticket.id);
  const isImg=/\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
  const thumb=isImg?`<img loading="lazy" src="${it.url}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
  const delBtn = canDelete? `<button class="btn" data-del="${it.url}" data-owner="${it.owner}" data-kind="${it.kind}" data-date="${it.date}" data-name="${it.name}">🗑</button>` : "";
  const printBtn = buildPrintBtn(it);
  const badge = buildBadge(it);
  const ticketLink = `<a class="btn" href="/ticket/form?owner=${encodeURIComponent(it.owner)}&kind=${it.kind}&day=${it.date}&name=${encodeURIComponent(it.name)}">Ticket</a>`;
  if(!el){
    el=document.createElement("div");
    el.id=id;
    grid.appendChild(el);
  }
  el.className="item "+(hasTicket?"completo":"pendiente");
  el.innerHTML=thumb+`<div class="meta"><span>${it.date} · ${it.cat} · ${badge}</span><div><a class="btn" target="_blank" href="${it.url}">Abrir</a> ${ticketLink} ${printBtn} ${delBtn}</div></div>`;
  requestAnimationFrame(()=>el.classList.add("show"));
  return el;
}
function bindDeletes(){
  if(!canDelete) return;
  grid.querySelectorAll("[data-del]").forEach(btn=>{
    btn.onclick=async ()=>{
      if(!confirm("¿Borrar este archivo (y sus tickets)?")) return;
      const qs=new URLSearchParams({
        owner: btn.dataset.owner,
        kind: btn.dataset.kind,
        day: btn.dataset.date,
        name: btn.dataset.name
      });
      const r=await fetch("/api/delete?"+qs.toString(), {method:"DELETE"});
      if(r.ok){ btn.closest(".item")?.remove(); }
    };
  });
}
function diffRender(items){
  grid.innerHTML="";
  (items||[]).forEach(it=>ensureCard(it));
  bindDeletes();
}
function applyCounts(counts){
  cP.textContent="PERSONA: "+(counts["PERSONA"]||0);
  cA.textContent="AUTO: "+(counts["AUTO"]||0);
  cN.textContent="ANIMAL: "+(counts["ANIMAL"]||0);
  cO.textContent="OTROS: "+(counts["OTROS"]||0);
}
function buildParams(){
  const p=new URLSearchParams();
  p.set("kind", kind.value);
  p.set("page", page.value||"1");
  p.set("limit", limit.value||"50");
  if(start.value) p.set("start", start.value);
  if(end.value) p.set("end", end.value);
  if(q.value.trim()) p.set("q", q.value.trim());
  if(cat.value) p.set("cat", cat.value);
  if(owner.value) p.set("owner", owner.value);
  if(statusSel.value) p.set("status", statusSel.value);
  return p;
}
async function load(){
  const r=await fetch("/api/list?"+buildParams().toString());
  if(r.status==401){ location.href="/login"; return; }
  const j=await r.json();
  countEl.textContent=j.total||0;
  pagesEl.textContent=(j.page||1)+"/"+(j.pages||1);
  diffRender(j.items||[]); applyCounts(j.counts||{});
  dl.href="/api/export.csv?"+buildParams().toString();
  if(start.value){
    repDia.href="/reporte/dia?owner="+encodeURIComponent(owner.value||"")+"&fecha="+encodeURIComponent(start.value);
  }else{
    repDia.href="#";
  }
}
async function boot(){
  const me=await fetch("/me"); if(me.status==401){ location.href="/login"; return; }
  const info=await me.json(); canDelete=(info.role==="admin");
  const whoSpan=document.querySelector("#who");
  if(whoSpan) whoSpan.textContent=`· ${info.user} (${info.role})`;
  if(info.role!=="admin"){ owner.value=info.user; owner.disabled=true; }
  load();
}
$("#f").addEventListener("submit", e=>{ e.preventDefault(); load(); });
boot();
</script>
</body></html>
"""

@app.get("/panel", response_class=HTMLResponse)
def panel(req: Request):
    require_login(req)
    return PANEL_HTML

# -------------------- Ticket Vehicular – Formulario --------------------
TICKET_FORM_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ticket vehicular ORBET</title>
<style>
body{{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}}
.wrap{{max-width:900px;margin:auto}}
h1{{font-size:24px;margin:0 0 12px}}
.card{{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0}}
img{{max-width:100%;border-radius:10px}}
label{{display:flex;flex-direction:column;font-size:13px;margin-bottom:8px}}
input,select,textarea{{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:8px;padding:6px 8px;font-size:14px}}
textarea{{min-height:70px;resize:vertical}}
.row{{display:flex;gap:12px;flex-wrap:wrap}}
.row > label{{flex:1}}
button{{background:#24305a;border:none;border-radius:10px;padding:10px 16px;color:#e7e7ee;cursor:pointer;margin-right:8px}}
a.btn{{background:#2b825b;border-radius:10px;padding:10px 16px;color:#e7e7ee;text-decoration:none}}
small{{opacity:.8}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Ticket vehicular ORBET</h1>
  <div class="card">
    <div><strong>Owner:</strong> {owner} &nbsp;&nbsp; <strong>Fecha:</strong> {fecha} &nbsp;&nbsp; <strong>Hora:</strong> {hora}</div>
    <div><strong>Captura:</strong> {day} / {name}</div>
    <div><strong>Estado:</strong> {estado}</div>
  </div>
  <div class="card">
    <img src="{file_url}" alt="Captura vehículo">
  </div>
  <div class="card">
    <form method="post" action="/ticket/save">
      <input type="hidden" name="owner" value="{owner}">
      <input type="hidden" name="kind" value="{kind}">
      <input type="hidden" name="day" value="{day}">
      <input type="hidden" name="name" value="{name}">
      <input type="hidden" name="ticket_id" value="{ticket_id}">
      <div class="row">
        <label>Tipo de movimiento
          <select name="tipo_mov" required>
            <option value="">-- seleccionar --</option>
            <option value="ENTRADA" {sel_ent}>ENTRADA</option>
            <option value="SALIDA" {sel_sal}>SALIDA</option>
          </select>
        </label>
        <label>Placa
          <input name="placa" value="{placa}" required>
        </label>
        <label>N° Guía de remisión
          <input name="guia" value="{guia}" required>
        </label>
      </div>
      <div class="row">
        <label>Nombre del chofer
          <input name="chofer" value="{chofer}">
        </label>
        <label>DNI del chofer
          <input name="dni" value="{dni}">
        </label>
        <label>Empresa transportista
          <input name="empresa" value="{empresa}">
        </label>
      </div>
      <label>Material (qué ingresa / qué retira)
        <input name="material" value="{material}">
      </label>
      <label>Observaciones
        <textarea name="observaciones">{observaciones}</textarea>
      </label>
      <div style="margin-top:10px">
        <button type="submit">Guardar y imprimir</button>
        <a class="btn" href="/panel">Volver al panel</a>
        <div><small>Al guardar se genera/actualiza el ticket y se abre la vista de impresión.</small></div>
      </div>
    </form>
  </div>
</div>
</body></html>
"""

@app.get("/ticket/form", response_class=HTMLResponse)
def ticket_form(
    req: Request,
    owner: str,
    kind: str,
    day: str,
    name: str,
):
    require_login(req)
    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    path = _safe_path(owner, kind, day, name)
    stat = os.stat(path)
    dt = datetime.fromtimestamp(stat.st_mtime)
    fecha = dt.date().isoformat()
    hora = dt.time().strftime("%H:%M:%S")
    file_url = f"/files/{secure_name(owner)}/{kind}/{day}/{name}"

    ticket = get_ticket_for_capture(owner, kind, day, name)
    estado = "COMPLETO" if ticket else "PENDIENTE"
    ctx = {
        "owner": owner,
        "kind": kind,
        "day": day,
        "name": name,
        "file_url": file_url,
        "fecha": fecha,
        "hora": hora,
        "estado": estado,
        "ticket_id": ticket["id"] if ticket else "",
        "tipo_mov": ticket.get("tipo_mov") if ticket else "",
        "placa": ticket.get("placa") if ticket else "",
        "chofer": ticket.get("chofer") if ticket else "",
        "dni": ticket.get("dni") if ticket else "",
        "empresa": ticket.get("empresa") if ticket else "",
        "guia": ticket.get("guia") if ticket else "",
        "material": ticket.get("material") if ticket else "",
        "observaciones": ticket.get("observaciones") if ticket else "",
    } if ticket else {
        "owner": owner, "kind": kind, "day": day, "name": name,
        "file_url": file_url, "fecha": fecha, "hora": hora,
        "estado": estado, "ticket_id": "",
        "tipo_mov": "", "placa": "", "chofer": "", "dni": "",
        "empresa": "", "guia": "", "material": "", "observaciones": "",
    }
    html = TICKET_FORM_HTML.format(
        owner=ctx["owner"],
        kind=ctx["kind"],
        day=ctx["day"],
        name=ctx["name"],
        file_url=ctx["file_url"],
        fecha=ctx["fecha"],
        hora=ctx["hora"],
        estado=ctx["estado"],
        ticket_id=ctx["ticket_id"],
        placa=ctx["placa"],
        chofer=ctx["chofer"],
        dni=ctx["dni"],
        empresa=ctx["empresa"],
        guia=ctx["guia"],
        material=ctx["material"],
        observaciones=ctx["observaciones"],
        sel_ent="selected" if ctx["tipo_mov"] == "ENTRADA" else "",
        sel_sal="selected" if ctx["tipo_mov"] == "SALIDA" else "",
    )
    return HTMLResponse(html)

# -------------------- Ticket Vehicular – Guardar + imprimir --------------------
@app.post("/ticket/save")
def ticket_save(
    req: Request,
    owner: str = Form(...),
    kind: str = Form(...),
    day: str = Form(...),
    name: str = Form(...),
    tipo_mov: str = Form(...),
    placa: str = Form(...),
    guia: str = Form(...),
    chofer: str = Form(""),
    dni: str = Form(""),
    empresa: str = Form(""),
    material: str = Form(""),
    observaciones: str = Form(""),
    ticket_id: str = Form(""),
):
    require_login(req)
    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    path = _safe_path(owner, kind, day, name)
    stat = os.stat(path)
    dt = datetime.fromtimestamp(stat.st_mtime)
    fecha = dt.date().isoformat()
    hora = dt.time().strftime("%H:%M:%S")
    now_s = datetime.now().isoformat(timespec="seconds")

    conn = _db_conn()
    cur = conn.cursor()
    if ticket_id:
        cur.execute(
            """
            UPDATE tickets_vehiculares
            SET tipo_mov=?, placa=?, chofer=?, dni=?, empresa=?, guia=?, material=?, observaciones=?
            WHERE id=?
            """,
            (tipo_mov, placa, chofer, dni, empresa, guia, material, observaciones, ticket_id),
        )
        conn.commit()
        tid = int(ticket_id)
    else:
        cur.execute(
            "SELECT MAX(n_corr_dia) FROM tickets_vehiculares WHERE owner=? AND fecha=?",
            (owner, fecha),
        )
        row = cur.fetchone()
        last_corr = row[0] if row and row[0] is not None else 0
        corr = last_corr + 1
        cur.execute(
            """
            INSERT INTO tickets_vehiculares
            (owner,captura_owner,captura_kind,captura_day,captura_name,
             fecha,hora,n_corr_dia,tipo_mov,placa,chofer,dni,empresa,guia,material,observaciones,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                owner, owner, kind, day, name,
                fecha, hora, corr, tipo_mov, placa, chofer, dni, empresa, guia, material, observaciones, now_s
            ),
        )
        conn.commit()
        tid = cur.lastrowid
    conn.close()

    # Redirige a vista de impresión con auto=1 (abre diálogo de impresión)
    return RedirectResponse(url=f"/ticket/print/{tid}?auto=1", status_code=302)

# -------------------- Ticket Vehicular – Vista impresión --------------------
TICKET_PRINT_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ticket #{n_corr} – ORBET</title>
<style>
body{{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:12px}}
h1{{font-size:18px;margin:0 0 8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:4px 6px;vertical-align:top}}
hr{{margin:8px 0}}
</style>
</head>
<body>
<h1>REPORTE DE CONTROL VEHICULAR – ORBET</h1>
<hr>
<table>
<tr><td><strong>Cliente / Owner:</strong> {owner}</td><td><strong>Fecha:</strong> {fecha}</td></tr>
<tr><td><strong>Hora:</strong> {hora}</td><td><strong>Ticket N°:</strong> {n_corr}</td></tr>
<tr><td colspan="2"><strong>Captura:</strong> {captura_day} / {captura_name}</td></tr>
</table>
<hr>
<table>
<tr><td><strong>Movimiento:</strong></td><td>{tipo_mov}</td></tr>
<tr><td><strong>Placa:</strong></td><td>{placa}</td></tr>
<tr><td><strong>Chofer:</strong></td><td>{chofer}</td></tr>
<tr><td><strong>DNI:</strong></td><td>{dni}</td></tr>
<tr><td><strong>Empresa:</strong></td><td>{empresa}</td></tr>
<tr><td><strong>Guía de remisión:</strong></td><td>{guia}</td></tr>
<tr><td><strong>Material:</strong></td><td>{material}</td></tr>
<tr><td><strong>Observaciones:</strong></td><td>{observaciones}</td></tr>
</table>
<hr>
<p>Firma del vigilante: ________________________________</p>
{auto_script}
</body></html>
"""

@app.get("/ticket/print/{ticket_id}", response_class=HTMLResponse)
def ticket_print(req: Request, ticket_id: int, auto: int = 0):
    require_login(req)
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets_vehiculares WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    owner = row["owner"]
    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    auto_script = "<script>window.print();</script>" if auto else ""
    html = TICKET_PRINT_HTML.format(
        n_corr=row["n_corr_dia"],
        owner=row["owner"],
        fecha=row["fecha"],
        hora=row["hora"],
        captura_day=row["captura_day"],
        captura_name=row["captura_name"],
        tipo_mov=row["tipo_mov"] or "",
        placa=row["placa"] or "",
        chofer=row["chofer"] or "",
        dni=row["dni"] or "",
        empresa=row["empresa"] or "",
        guia=row["guia"] or "",
        material=row["material"] or "",
        observaciones=row["observaciones"] or "",
        auto_script=auto_script,
    )
    return HTMLResponse(html)

# -------------------- Reporte diario HTML --------------------
REPORT_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Reporte diario – ORBET</title>
<style>
body{{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}}
h1{{font-size:20px;margin:0 0 8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border:1px solid #ccc;padding:4px 6px}}
th{{background:#f0f0f0}}
.small{{font-size:12px;opacity:.8}}
</style>
</head>
<body>
<h1>REPORTE DIARIO DE CONTROL VEHICULAR – ORBET</h1>
<p><strong>Cliente / Owner:</strong> {owner}<br>
<strong>Fecha:</strong> {fecha}</p>
<table>
<thead>
<tr>
<th>N°</th><th>Hora</th><th>Placa</th><th>Chofer</th><th>Guía</th><th>Movimiento</th><th>Material</th><th>Observaciones</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<p><strong>Totales del día:</strong><br>
Total operaciones: {total_ops}<br>
Entradas: {total_ent}<br>
Salidas: {total_sal}</p>
</body></html>
"""

@app.get("/reporte/dia", response_class=HTMLResponse)
def reporte_dia(req: Request, owner: str, fecha: str):
    require_login(req)
    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM tickets_vehiculares
        WHERE owner=? AND fecha=?
        ORDER BY n_corr_dia ASC
        """,
        (owner, fecha),
    )
    rows_db = cur.fetchall()
    conn.close()
    total_ops = len(rows_db)
    total_ent = sum(1 for r in rows_db if (r["tipo_mov"] or "").upper() == "ENTRADA")
    total_sal = sum(1 for r in rows_db if (r["tipo_mov"] or "").upper() == "SALIDA")

    rows_html = ""
    for r in rows_db:
        rows_html += "<tr>"
        rows_html += f"<td>{r['n_corr_dia']}</td>"
        rows_html += f"<td>{r['hora']}</td>"
        rows_html += f"<td>{r['placa'] or ''}</td>"
        rows_html += f"<td>{r['chofer'] or ''}</td>"
        rows_html += f"<td>{r['guia'] or ''}</td>"
        rows_html += f"<td>{r['tipo_mov'] or ''}</td>"
        rows_html += f"<td>{r['material'] or ''}</td>"
        rows_html += f"<td>{r['observaciones'] or ''}</td>"
        rows_html += "</tr>\n"

    html = REPORT_HTML.format(
        owner=owner,
        fecha=fecha,
        rows=rows_html or '<tr><td colspan="8">Sin operaciones registradas.</td></tr>',
        total_ops=total_ops,
        total_ent=total_ent,
        total_sal=total_sal,
    )
    return HTMLResponse(html)

# -------------------- Run local --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

