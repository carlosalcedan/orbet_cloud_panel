from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime
import os

# ===== Config =====
APP_TOKEN = os.getenv("ORBET_TOKEN", "CAMBIA_ESTE_TOKEN")  # pon un token fuerte en Render
BASE_DIR  = Path(__file__).parent.resolve()
DATA_DIR  = BASE_DIR / "data"
CAP_DIR   = DATA_DIR / "capturas"
TIC_DIR   = DATA_DIR / "tickets"
for d in (CAP_DIR, TIC_DIR): d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ORBET Cloud Panel")

# Servir archivos estáticos (galería lee de aquí)
app.mount("/files", StaticFiles(directory=str(DATA_DIR), html=False), name="files")

def day_dir(sub: Path) -> Path:
    d = sub / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d

def assert_token(token: str):
    if token != APP_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

@app.get("/", response_class=HTMLResponse)
def home():
    # Galería auto-refresh
    return """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ORBET – Galería en vivo</title>
<style>
 body{font-family:system-ui,Segoe UI,Roboto,Arial;margin:0;background:#0b0f14;color:#e8eef7}
 header{padding:12px 16px;background:#111827;position:sticky;top:0}
 h1{margin:0;font-size:18px}
 main{padding:14px}
 .row{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
 .card{background:#111827;border:1px solid #1f2937;border-radius:12px;overflow:hidden}
 .card img{width:100%;display:block}
 .meta{padding:8px 10px;font-size:12px;color:#b6c2d1}
 .tabs a{color:#9ca3af;margin-right:14px;text-decoration:none}
 .tabs a.active{color:#fff;font-weight:600}
 .muted{color:#9ca3af}
</style>
<script>
 const qs = (k)=>new URL(location).searchParams.get(k);
 let tab = qs('tab') || 'capturas';
 function setTab(t){ tab=t; render(); }
 async function fetchList(){
   const r = await fetch('/api/list?type='+tab);
   if(!r.ok) return [];
   return r.json();
 }
 function fmt(s){ return s; }
 async function render(){
   document.querySelectorAll('.tabs a').forEach(a=>{
     a.classList.toggle('active', a.dataset.t===tab);
   });
   const data = await fetchList();
   const cont = document.getElementById('grid');
   cont.innerHTML='';
   if(!data.length){ cont.innerHTML = '<p class="muted">Sin registros.</p>'; return; }
   for(const it of data){
     const el = document.createElement('div');
     el.className = 'card';
     if(it.url.endsWith('.jpg') || it.url.endsWith('.png')){
       el.innerHTML = `<img src="${it.url}" alt=""><div class="meta">${fmt(it.name)}</div>`;
     } else {
       el.innerHTML = `<div class="meta"><a href="${it.url}" target="_blank">${fmt(it.name)}</a></div>`;
     }
     cont.appendChild(el);
   }
 }
 setInterval(render, 5000);
 window.addEventListener('DOMContentLoaded', render);
</script>
</head>
<body>
<header>
  <h1>ORBET – Galería en vivo</h1>
  <div class="tabs" style="margin-top:6px">
    <a href="javascript:setTab('capturas')" data-t="capturas" class="active">📸 Capturas</a>
    <a href="javascript:setTab('tickets')"  data-t="tickets">🎟️ Tickets</a>
  </div>
</header>
<main>
  <div id="grid" class="row"></div>
  <p class="muted" style="margin-top:10px">Se actualiza cada 5 s.</p>
</main>
</body></html>"""

@app.get("/api/list")
def api_list(type: str = "capturas", date: str | None = None):
    root = CAP_DIR if type == "capturas" else TIC_DIR
    if date:
      target = root / date
    else:
      # listamos el día actual + el anterior para que siempre veas algo
      target = root
    if not target.exists(): return []
    items = []
    if target.is_dir():
        # Recorre subcarpetas por fecha y lista archivos
        for sub in sorted(target.glob("*"), reverse=True):
            if not sub.is_dir(): continue
            for f in sorted(sub.glob("*")):
                rel = f.relative_to(DATA_DIR).as_posix()
                items.append({"name": f.name, "url": f"/files/{rel}"})
    else:
        for f in sorted(root.glob("*")):
            rel = f.relative_to(DATA_DIR).as_posix()
            items.append({"name": f.name, "url": f"/files/{rel}"})
    return JSONResponse(items[-500:])  # tope

@app.post("/upload")
async def upload(
    token: str = Form(...),
    kind: str  = Form(...),  # "captura" | "ticket"
    file: UploadFile = File(...)
):
    assert_token(token)
    if kind not in ("captura", "ticket"):
        raise HTTPException(400, "kind debe ser 'captura' o 'ticket'")
    folder = day_dir(CAP_DIR if kind == "captura" else TIC_DIR)
    suffix = Path(file.filename).suffix.lower() or (".jpg" if kind=="captura" else ".txt")
    now = datetime.now().strftime("%H-%M-%S_%f")
    safe = f"{('CAP' if kind=='captura' else 'TIC')}_{now}{suffix}"
    dest = folder / safe
    with dest.open("wb") as fh:
        fh.write(await file.read())
    rel = dest.relative_to(DATA_DIR).as_posix()
    return {"ok": True, "url": f"/files/{rel}", "name": safe}

@app.get("/healthz")
def healthz():
    return {"ok": True}
