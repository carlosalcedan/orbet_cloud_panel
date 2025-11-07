# app.py — ORBET Cloud Panel: Login + /files protegido + /thumbs + Filtros + Paginación + CSV
# Reqs: fastapi uvicorn python-multipart Pillow itsdangerous
# Env en Render:
#   SECRET_KEY     = <cadena larga aleatoria>
#   USERS          = "admin:admin123,cliente:1234"
#   ORBET_TOKEN    = "ORBET_2025_Seguridad_ARES"
#   DATA_DIR       = "data"
#   RETENTION_DAYS = "0"    # 0 = sin limpieza automática

import os, io, csv, time, shutil
from pathlib import Path
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "ORBET Cloud Panel"

# -------------------- Config --------------------
ORBET_TOKEN = os.getenv("ORBET_TOKEN", "ORBET_2025_Seguridad_ARES")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "0"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
CAP_DIR = DATA_DIR / "capturas"
TIC_DIR = DATA_DIR / "tickets"
for d in (CAP_DIR, TIC_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Login
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret_in_render")
USERS_ENV = os.getenv("USERS", "admin:admin123")  # "user:pass,user2:pass2"
USERS: Dict[str,str] = {}
for pair in USERS_ENV.split(","):
    if ":" in pair:
        u, p = pair.split(":", 1)
        USERS[u.strip()] = p.strip()

# -------------------- Utils --------------------
def today_dir(kind: str) -> Path:
    base = CAP_DIR if kind == "capturas" else TIC_DIR
    day = datetime.now().strftime("%Y-%m-%d")
    p = base / day
    p.mkdir(parents=True, exist_ok=True)
    return p

def secure_name(name: str) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(c if c in keep else "_" for c in name)

def is_image(filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))

def make_thumb(dest: Path):
    """Genera miniatura JPG junto al archivo original (solo si es imagen)."""
    if not is_image(dest.name):
        return
    try:
        from PIL import Image
        im = Image.open(dest)
        im.thumbnail((480, 480))
        thumb = dest.with_suffix(dest.suffix + ".thumb.jpg")
        im.save(thumb, quality=80)
    except Exception:
        # no bloqueamos por errores de thumbnail
        pass

def cleanup_old(days: int):
    if days <= 0: return
    cutoff = time.time() - days * 86400
    for base in (CAP_DIR, TIC_DIR):
        for day_dir in base.iterdir():
            if not day_dir.is_dir(): continue
            try:
                ts = time.mktime(time.strptime(day_dir.name, "%Y-%m-%d"))
            except Exception:
                continue
            if ts < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _category_from_name(name: str) -> str:
    n = name.lower()
    if "persona" in n: return "PERSONA"
    if any(x in n for x in ("auto","car","vehic","truck","bus")): return "AUTO"
    if any(x in n for x in ("animal","dog","cat","bird")): return "ANIMAL"
    return "OTROS"

def _iter_items(kind_dir: Path, start: Optional[date], end: Optional[date], qnorm: str):
    """Iterador de archivos filtrados por día y query, renderea más tarde."""
    for day_dir in sorted(kind_dir.iterdir(), reverse=True):
        if not day_dir.is_dir(): 
            continue
        try:
            d = parse_ymd(day_dir.name)
        except Exception:
            continue
        if start and d < start: 
            continue
        if end and d > end: 
            continue
        for f in sorted(day_dir.iterdir(), reverse=True):
            if not f.is_file(): 
                continue
            name = f.name
            if qnorm:
                cat = _category_from_name(name)
                if (qnorm not in name.lower()) and (qnorm not in cat.lower()):
                    continue
            yield (d, day_dir.name, f)

def list_items(kind: str, start: Optional[date], end: Optional[date], q: Optional[str]) -> List[Dict]:
    base = CAP_DIR if kind == "capturas" else TIC_DIR
    qnorm = (q or "").strip().lower()
    items: List[Dict] = []
    for d, day_str, f in _iter_items(base, start, end, qnorm):
        name = f.name
        items.append({
            "date": d.isoformat(),
            "name": name,
            "url": f"/files/{kind}/{day_str}/{name}",
            "thumb": f"/thumbs/{kind}/{day_str}/{name}",  # NUEVO: miniatura
            "kind": kind,
            "cat": _category_from_name(name),
        })
    return items

def paginate(items: List[Dict], page: int, size: int) -> Tuple[List[Dict], int]:
    total = len(items)
    if size <= 0: size = 50
    if size > 200: size = 200
    if page <= 0: page = 1
    start = (page - 1) * size
    end = start + size
    return items[start:end], total

# -------------------- App --------------------
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60*60*12)  # 12h

@app.on_event("startup")
def _on_start():
    try: cleanup_old(RETENTION_DAYS)
    except Exception: pass

# -------------------- Auth helpers --------------------
def current_user(req: Request) -> Optional[str]:
    return req.session.get("user")

def require_login(req: Request):
    if not current_user(req):
        raise HTTPException(status_code=401, detail="No autorizado")

# -------------------- Login/Logout --------------------
LOGIN_HTML = """
<!doctype html><html lang="es"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ingresar – ORBET</title>
<style>
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:22px;width:360px;box-shadow:0 6px 18px rgba(0,0,0,.25)}
h1{font-size:20px;margin:0 0 12px}
label{display:block;font-size:12px;opacity:.9;margin:8px 0 4px}
input,button{width:100%;background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:10px}
button{cursor:pointer;margin-top:10px}
.msg{color:#ff9b9b;font-size:12px;margin:8px 0}
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
  </form>
</body></html>
"""

@app.get("/login", response_class=HTMLResponse)
def login_form():
    return LOGIN_HTML.replace("{msg}","")

@app.post("/login")
def login(req: Request, username: str = Form(...), password: str = Form(...)):
    if username in USERS and USERS[username] == password:
        req.session["user"] = username
        return RedirectResponse(url="/panel", status_code=302)
    html = LOGIN_HTML.replace("{msg}", '<div class="msg">Usuario o contraseña inválidos</div>')
    return HTMLResponse(html, status_code=401)

@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse(url="/login", status_code=302)

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
    background: BackgroundTasks = None,
):
    if token != ORBET_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    if kind not in ("captura", "ticket"):
        raise HTTPException(status_code=400, detail="kind debe ser 'captura' o 'ticket'")

    target_dir = today_dir("capturas" if kind == "captura" else "tickets")
    original = secure_name(file.filename or f"file_{int(time.time())}")
    stamp = datetime.now().strftime("%H-%M-%S")
    dest = target_dir / f"{stamp}_{original}"

    data = await file.read()
    with open(dest, "wb") as fh:
        fh.write(data)

    # Miniatura en segundo plano (si es imagen)
    if background: background.add_task(make_thumb, dest)
    else: make_thumb(dest)

    url = f"/files/{'capturas' if kind=='captura' else 'tickets'}/{target_dir.name}/{dest.name}"
    return {"ok": True, "url": url, "name": dest.name}

# -------------------- /files protegido (requiere login) --------------------
def _safe_path(kind: str, day: str, name: str) -> Path:
    if kind not in ("capturas", "tickets"):
        raise HTTPException(status_code=404, detail="No encontrado")
    # evitar path traversal
    day = secure_name(day)
    name = secure_name(name)
    base = CAP_DIR if kind == "capturas" else TIC_DIR
    path = (base / day / name).resolve()
    if not str(path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Ruta inválida")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return path

@app.get("/files/{kind}/{day}/{name}")
def get_file(req: Request, kind: str, day: str, name: str):
    require_login(req)
    path = _safe_path(kind, day, name)
    media = "application/octet-stream"
    if is_image(path.name):
        media = "image/jpeg" if path.suffix.lower() in (".jpg",".jpeg") else "image/png"
    return FileResponse(path, media_type=media)

# -------------------- /thumbs protegido (sirve miniatura si existe, sino original) --------------------
@app.get("/thumbs/{kind}/{day}/{name}")
def get_thumb(req: Request, kind: str, day: str, name: str):
    require_login(req)
    path = _safe_path(kind, day, name)
    if is_image(path.name):
        thumb = path.with_suffix(path.suffix + ".thumb.jpg")
        if thumb.exists():
            return FileResponse(thumb, media_type="image/jpeg")
    # fallback
    media = "application/octet-stream"
    if is_image(path.name):
        media = "image/jpeg" if path.suffix.lower() in (".jpg",".jpeg") else "image/png"
    return FileResponse(path, media_type=media)

# -------------------- API list (requiere login) --------------------
@app.get("/api/list", summary="Lista archivos con filtros de fecha, búsqueda y paginación")
def api_list(
    req: Request,
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(60, ge=1, le=200),
):
    require_login(req)
    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None
    items_all = list_items(kind, s, e, q)
    items, total = paginate(items_all, page, size)
    counts = {"PERSONA": 0, "AUTO": 0, "ANIMAL": 0, "OTROS": 0}
    for it in items_all:
        counts[it["cat"]] = counts.get(it["cat"], 0) + 1
    return {
        "ok": True,
        "count": len(items),
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size if size else 1,
        "items": items,
        "counts": counts
    }

# -------------------- Export CSV (requiere login) --------------------
@app.get("/api/export.csv")
def export_csv(
    req: Request,
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
):
    require_login(req)
    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None
    items = list_items(kind, s, e, q)

    def gen():
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow(["fecha", "tipo", "categoria", "nombre_archivo", "url"])
        for it in items:
            writer.writerow([it["date"], it["kind"], it["cat"], it["name"], it["url"]])
        yield sio.getvalue()

    headers = {"Content-Disposition": f'attachment; filename="orbet_{kind}.csv"'}
    return StreamingResponse(gen(), headers=headers, media_type="text/csv; charset=utf-8")

# -------------------- UI: vista rápida (requiere login) --------------------
INDEX_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORBET – Galería rápida</title>
<style>
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 12px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
select{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}
.badge{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease}
.item.show{opacity:1}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px}
a{color:#9ecbff}
.right{margin-left:auto}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería en vivo <a class="right" href="/logout">Salir</a></h1>
  <div class="card">
    <div class="row">
      <label>Tipo
        <select id="kind">
          <option value="capturas">Capturas</option>
          <option value="tickets">Tickets</option>
        </select>
      </label>
      <span id="count" class="badge">0</span>
      <a href="/panel">Ir al Panel con filtros</a>
    </div>
  </div>
  <div class="card"><div id="grid" class="grid"></div></div>
</div>
<script>
const $=s=>document.querySelector(s);
const grid=$("#grid"), countEl=$("#count"), kind=$("#kind");
const cardId=url=>"i_"+btoa(url).replace(/=/g,"");
function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  if(!el){
    const isImg=/\\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumbHtml=isImg?`<img loading="lazy" src="${it.thumb}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    el=document.createElement("div"); el.className="item"; el.id=id;
    el.innerHTML=thumbHtml+`<div class="meta"><span>${it.date}</span><a class="badge" target="_blank" href="${it.url}">Abrir</a></div>`;
    grid.prepend(el); requestAnimationFrame(()=>el.classList.add("show"));
  }
  return el;
}
function diffRender(items){
  const want=new Set(items.map(it=>cardId(it.url)));
  Array.from(grid.children).forEach(ch=>{ if(!want.has(ch.id)) ch.remove(); });
  items.forEach(it=>ensureCard(it));
  countEl.textContent=items.length;
}
async function load(){
  const r=await fetch("/api/list?kind="+kind.value+"&page=1&size=60");
  if(r.status==401){ location.href="/login"; return; }
  const j=await r.json(); diffRender(j.items||[]);
}
kind.addEventListener("change", load);
load(); setInterval(load, 5000);
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
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1200px;margin:auto}
h1{font-size:26px;margin:0 0 14px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
.row label{display:flex;flex-direction:column;font-size:12px;opacity:.9}
input,select,button{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}
button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease}
.item.show{opacity:1}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px}
.badge{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}
.stats{display:flex;gap:8px;flex-wrap:wrap}
a{color:#9ecbff}
.right{margin-left:auto}
.pager{display:flex;gap:8px;align-items:center}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería con filtros <a class="right" href="/logout">Salir</a></h1>

  <div class="card">
    <form id="f" class="row">
      <label>Tipo
        <select id="kind">
          <option value="capturas">Capturas</option>
          <option value="tickets">Tickets</option>
        </select>
      </label>
      <label>Desde <input type="date" id="start"></label>
      <label>Hasta <input type="date" id="end"></label>
      <label>Buscar (nombre o categoría)
        <input type="text" id="q" placeholder="persona / auto / animal / texto">
      </label>
      <label>Tamaño
        <select id="size">
          <option>30</option><option selected>60</option><option>100</option><option>150</option><option>200</option>
        </select>
      </label>
      <button type="submit">Filtrar</button>
      <button id="btnHoy">Hoy</button>
      <button id="btn7">Últimos 7 días</button>
      <label>Autorefresco
        <select id="refreshSel">
          <option value="0">Apagado</option>
          <option value="30000">Cada 30 s</option>
        </select>
      </label>
      <a href="/">Vista rápida</a>
      <a id="dl" href="#" download>⬇️ CSV</a>
      <span id="count" class="badge">0</span>
    </form>
  </div>

  <div class="card">
    <div class="stats">
      <span class="badge" id="cP">PERSONA: 0</span>
      <span class="badge" id="cA">AUTO: 0</span>
      <span class="badge" id="cN">ANIMAL: 0</span>
      <span class="badge" id="cO">OTROS: 0</span>
      <span class="badge" id="total">TOTAL: 0</span>
    </div>
  </div>

  <div class="card">
    <div class="pager">
      <button id="prev">Anterior</button>
      <span id="pageInfo">Página 1/1</span>
      <button id="next">Siguiente</button>
    </div>
    <div id="grid" class="grid" style="margin-top:12px"></div>
  </div>
</div>

<script>
const $=sel=>document.querySelector(sel);
const grid=$("#grid"), countEl=$("#count");
const kind=$("#kind"), start=$("#start"), end=$("#end"), q=$("#q"), sizeSel=$("#size");
const cP=$("#cP"), cA=$("#cA"), cN=$("#cN"), cO=$("#cO"), totalEl=$("#total");
const refreshSel=$("#refreshSel"); let timer=null;
const dl=$("#dl"); const pageInfo=$("#pageInfo"), btnPrev=$("#prev"), btnNext=$("#next");
let page=1, pages=1;

const cardId=url=>"i_"+btoa(url).replace(/=/g,"");

function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  if(!el){
    const isImg=/\\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumbHtml=isImg?`<img loading="lazy" src="${it.thumb}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    el=document.createElement("div"); el.className="item"; el.id=id;
    el.innerHTML=thumbHtml+`<div class="meta"><span>${it.date}</span><a class="badge" target="_blank" href="${it.url}">Abrir</a></div>`;
    grid.appendChild(el); requestAnimationFrame(()=>el.classList.add("show"));
  }
  return el;
}
function diffRender(items){
  const want=new Set(items.map(it=>cardId(it.url)));
  Array.from(grid.children).forEach(ch=>{ if(!want.has(ch.id)) ch.remove(); });
  items.forEach(it=>ensureCard(it));
  countEl.textContent=items.length;
}
function applyCounts(counts,total,p,ps){
  cP.textContent="PERSONA: "+(counts["PERSONA"]||0);
  cA.textContent="AUTO: "+(counts["AUTO"]||0);
  cN.textContent="ANIMAL: "+(counts["ANIMAL"]||0);
  cO.textContent="OTROS: "+(counts["OTROS"]||0);
  totalEl.textContent="TOTAL: "+(total||0);
  pageInfo.textContent="Página "+p+"/"+ps;
  btnPrev.disabled = p<=1;
  btnNext.disabled = p>=ps;
}
function buildParams(){
  const p=new URLSearchParams();
  p.set("kind", kind.value);
  if(start.value) p.set("start", start.value);
  if(end.value) p.set("end", end.value);
  if(q.value.trim()) p.set("q", q.value.trim());
  p.set("page", String(page));
  p.set("size", String(sizeSel.value));
  return p;
}
async function load(){
  const r=await fetch("/api/list?"+buildParams().toString());
  if(r.status==401){ location.href="/login"; return; }
  const j=await r.json();
  diffRender(j.items||[]);
  applyCounts(j.counts||{}, j.total||0, j.page||1, j.pages||1);
  pages = j.pages || 1;
  // link CSV
  const p2=buildParams(); p2.delete("page"); p2.delete("size");
  dl.href="/api/export.csv?"+p2.toString();
}
function setRefresh(ms){
  if(timer){ clearInterval(timer); timer=null; }
  if(ms>0){ timer=setInterval(load, ms); }
}
$("#f").addEventListener("submit", e=>{ e.preventDefault(); page=1; load(); });
$("#btnHoy").addEventListener("click", e=>{
  e.preventDefault(); const t=new Date(); const y=t.toISOString().slice(0,10);
  start.value=y; end.value=y; page=1; load();
});
$("#btn7").addEventListener("click", e=>{
  e.preventDefault(); const t=new Date(); const s=new Date(t.getTime()-6*86400000);
  start.value=s.toISOString().slice(0,10); end.value=t.toISOString().slice(0,10); page=1; load();
});
btnPrev.addEventListener("click", e=>{ e.preventDefault(); if(page>1){ page--; load(); }});
btnNext.addEventListener("click", e=>{ e.preventDefault(); if(page<pages){ page++; load(); }});
refreshSel.addEventListener("change", ()=>setRefresh(parseInt(refreshSel.value||"0",10)));
load(); setRefresh(parseInt(refreshSel.value||"0",10));
</script>
</body></html>
"""
@app.get("/panel", response_class=HTMLResponse)
def panel(req: Request):
    require_login(req)
    return PANEL_HTML

# -------------------- Run local --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

