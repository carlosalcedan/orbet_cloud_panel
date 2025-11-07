# app.py — ORBET Panel en la nube (FastAPI)
# - Subidas autenticadas por token
# - Carpeta por fecha: data/<capturas|tickets>/YYYY-MM-DD/*
# - Miniaturas automáticas .thumb.jpg
# - Galería simple (/)
# - Panel con filtros por fecha (/panel)
# - API de lista con filtros (/api/list)
# Reqs: fastapi uvicorn python-multipart

import os, io, time, shutil
from pathlib import Path
from datetime import datetime, date
from typing import Optional, List, Dict

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

# =================== Configuración ===================
APP_NAME = "ORBET Cloud Panel"

# Token principal (ajústalo en Render → Environment)
ORBET_TOKEN = os.getenv("ORBET_TOKEN", "ORBET_2025_Seguridad_ARES")

# Días de retención (opcional). Si no deseas borrar, no pongas la variable.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "0"))  # 0 = no borrar

# Directorio base de datos de archivos
DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
CAP_DIR = DATA_DIR / "capturas"
TIC_DIR = DATA_DIR / "tickets"
for d in (CAP_DIR, TIC_DIR):
    d.mkdir(parents=True, exist_ok=True)

# =================== Utilidades ===================
def today_dir(kind: str) -> Path:
    """Devuelve la carpeta del día para 'capturas' o 'tickets'."""
    if kind not in ("capturas", "tickets"):
        raise ValueError("kind inválido")
    base = CAP_DIR if kind == "capturas" else TIC_DIR
    day = datetime.now().strftime("%Y-%m-%d")
    p = base / day
    p.mkdir(parents=True, exist_ok=True)
    return p

def secure_name(name: str) -> str:
    # evita rutas raras; deja solo caracteres seguros
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(c if c in keep else "_" for c in name)

def is_image(filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))

def make_thumb(dest: Path):
    """Crea .thumb.jpg si es imagen (no falla si no hay Pillow)."""
    if not is_image(dest.name):
        return
    try:
        from PIL import Image
        im = Image.open(dest)
        im.thumbnail((480, 480))
        thumb = dest.with_suffix(dest.suffix + ".thumb.jpg")
        im.save(thumb, quality=80)
    except Exception:
        # Miniatura opcional: si no hay Pillow, simplemente no se genera
        pass

def cleanup_old(days: int):
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for base in (CAP_DIR, TIC_DIR):
        for day_dir in base.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                ts = time.mktime(time.strptime(day_dir.name, "%Y-%m-%d"))
            except Exception:
                continue
            if ts < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def list_items(kind: str, start: Optional[date], end: Optional[date]) -> List[Dict]:
    base = CAP_DIR if kind == "capturas" else TIC_DIR
    items: List[Dict] = []
    for day_dir in sorted(base.iterdir(), reverse=True):
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
            items.append({
                "date": d.isoformat(),
                "name": f.name,
                "url": f"/files/{kind}/{day_dir.name}/{f.name}",
                "kind": kind
            })
    return items

# =================== App ===================
app = FastAPI(title=APP_NAME)

# Servir archivos guardados
app.mount("/files", StaticFiles(directory=str(DATA_DIR), html=False), name="files")

@app.on_event("startup")
def _on_start():
    # limpieza opcional al arrancar
    try:
        cleanup_old(RETENTION_DAYS)
    except Exception:
        pass

# ------------------- Health -------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": APP_NAME}

# ------------------- Upload -------------------
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

    # Carpeta del día según tipo
    target_dir = today_dir("capturas" if kind == "captura" else "tickets")

    # Nombre y ruta destino
    original = secure_name(file.filename or f"file_{int(time.time())}")
    # si viene solo "image.jpg", lo mantenemos; si quieres prefijar hora:
    stamp = datetime.now().strftime("%H-%M-%S")
    name = f"{stamp}_{original}"
    dest = target_dir / name

    # Guardar a disco
    data = await file.read()
    with open(dest, "wb") as fh:
        fh.write(data)

    # Crear miniatura si es imagen (no bloqueo la petición)
    if background:
        background.add_task(make_thumb, dest)
    else:
        make_thumb(dest)

    url = f"/files/{'capturas' if kind=='captura' else 'tickets'}/{target_dir.name}/{dest.name}"
    return {"ok": True, "url": url, "name": dest.name}

# ------------------- API list con filtros -------------------
@app.get("/api/list", summary="Lista archivos con filtros de fecha")
def api_list(
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None
    items = list_items(kind, s, e)
    return {"ok": True, "count": len(items), "items": items}

# ------------------- Galería simple (auto-refresh) -------------------
INDEX_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORBET – Galería</title>
<style>
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 12px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
.row label{display:flex;flex-direction:column;font-size:12px;opacity:.9}
select,button{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px}
.badge{font-size:11px;background:#24305a;padding:2px 8px;border-radius:999px}
a{color:#9ecbff}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería rápida</h1>
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
  <div class="card">
    <div id="grid" class="grid"></div>
  </div>
</div>

<script>
const $ = s=>document.querySelector(s);
const grid = $("#grid"), countEl = $("#count"), kind = $("#kind");

function render(items){
  countEl.textContent = items.length;
  grid.innerHTML = items.slice(0,200).map(it=>{
    const isImg = /\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumb = isImg ? `<img loading="lazy" src="${it.url}" alt="${it.name}">`
                        : `<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    return `<div class="item">${thumb}
      <div class="meta"><span>${it.date}</span><a class="badge" target="_blank" href="${it.url}">Abrir</a></div>
    </div>`;
  }).join("");
}

async function load(){
  const r = await fetch("/api/list?kind="+kind.value);
  const j = await r.json();
  render(j.items || []);
}
kind.addEventListener("change", load);
async function loop(){ await load(); setTimeout(loop, 5000); }
loop();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

# ------------------- Panel con filtros por fecha -------------------
PANEL_HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ORBET – Panel con filtros</title>
<style>
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 12px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
.row label{display:flex;flex-direction:column;font-size:12px;opacity:.9}
input,select,button{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}
button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px}
.badge{font-size:11px;background:#24305a;padding:2px 8px;border-radius:999px}
a{color:#9ecbff}
</style>
</head>
<body>
<div class="wrap">
  <h1>ORBET – Galería con filtros por fecha</h1>
  <div class="card">
    <form id="f" class="row">
      <label>Tipo
        <select id="kind">
          <option value="capturas">Capturas</option>
          <option value="tickets">Tickets</option>
        </select>
      </label>
      <label>Desde
        <input type="date" id="start">
      </label>
      <label>Hasta
        <input type="date" id="end">
      </label>
      <button type="submit">Filtrar</button>
      <button id="btnHoy">Hoy</button>
      <button id="btn7">Últimos 7 días</button>
      <span id="count" class="badge">0</span>
      <a href="/">Volver a la vista rápida</a>
    </form>
  </div>

  <div class="card">
    <div id="grid" class="grid"></div>
  </div>
</div>

<script>
const $ = sel => document.querySelector(sel);
const grid = $("#grid"), countEl = $("#count");
const kind = $("#kind"), start = $("#start"), end = $("#end");

function ymd(d){ return d.toISOString().slice(0,10); }

function render(items){
  countEl.textContent = items.length;
  grid.innerHTML = items.map(it=>{
    const isImg = /\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumb = isImg ? `<img loading="lazy" src="${it.url}" alt="${it.name}">`
                        : `<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    return `<div class="item">${thumb}
      <div class="meta"><span>${it.date}</span><a class="badge" target="_blank" href="${it.url}">Abrir</a></div>
    </div>`;
  }).join("");
}

async function load(){
  const params = new URLSearchParams();
  params.set("kind", kind.value);
  if(start.value) params.set("start", start.value);
  if(end.value)   params.set("end", end.value);
  const r = await fetch("/api/list?"+params.toString());
  const j = await r.json();
  render(j.items || []);
}

$("#f").addEventListener("submit", e=>{e.preventDefault(); load();});

$("#btnHoy").addEventListener("click", e=>{
  e.preventDefault();
  const t=new Date();
  start.value=end.value=ymd(t);
  load();
});

$("#btn7").addEventListener("click", e=>{
  e.preventDefault();
  const t=new Date();
  const s=new Date(t.getTime()-6*86400000);
  start.value=ymd(s); end.value=ymd(t);
  load();
});

// Carga inicial
load();
// Auto-refresh cada 5s manteniendo el filtro actual
setInterval(load, 5000);
</script>
</body>
</html>
"""

@app.get("/panel", response_class=HTMLResponse)
def panel():
    return PANEL_HTML

# ------------------- Run local -------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

