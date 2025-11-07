# app.py — ORBET CLOUD PANEL (auth + filtros + CSV + Paginación + ZIP + Guardado seguro)
import os
import io
import csv
import mimetypes
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File, Response, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import URL

# ========================= CONFIG =========================
APP_TITLE = "ORBET – Galería"
DEFAULT_TZ_OFFSET = -5  # solo para texto de ejemplo

# Seguridad / sesión
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

# Raíz de subidas (persistente si montas /data en Render)
UPLOAD_ROOT = Path(os.environ.get("UPLOAD_ROOT", "uploads")).resolve()
CAPTURES_DIR = UPLOAD_ROOT / "capturas"
TICKETS_DIR = UPLOAD_ROOT / "tickets"
for d in (CAPTURES_DIR, TICKETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Token para /upload del detector
ORBET_UPLOAD_TOKEN = os.environ.get("ORBET_CLOUD_TOKEN", "ORBET_2025_Seguridad_ARES")

# Paginación
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100

app = FastAPI(title=APP_TITLE)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ========================= Helpers =========================
def is_logged(request: Request) -> bool:
    return request.session.get("user") == ADMIN_USER

def login_required(request: Request):
    if not is_logged(request):
        raise HTTPException(status_code=401, detail="No autorizado")

def parse_date(d: Optional[str]) -> Optional[datetime]:
    if not d: 
        return None
    # admite "YYYY-MM-DD"
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None

def list_daily_folders(root: Path) -> List[Path]:
    if not root.exists():
        return []
    # directorios con nombre YYYY-MM-DD
    items = [p for p in root.iterdir() if p.is_dir()]
    items.sort(reverse=True)
    return items

def collect_files(
    root: Path,
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> List[Path]:
    """
    Recorre subcarpetas YYYY-MM-DD y junta los archivos dentro del rango.
    """
    out: List[Path] = []
    for day_dir in list_daily_folders(root):
        # intentar parsear fecha del folder
        try:
            fdate = datetime.strptime(day_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if date_from and fdate < date_from:
            continue
        if date_to and fdate > date_to:
            continue
        # agregar archivos (jpg/txt/lo que haya)
        for f in sorted(day_dir.iterdir()):
            if f.is_file():
                out.append(f)
    # orden descendente por mtime
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out

def slice_page(items: List[Path], page: int, size: int) -> Tuple[List[Path], int]:
    size = max(1, min(size, MAX_PAGE_SIZE))
    total = len(items)
    if total == 0:
        return [], 0
    pages = (total + size - 1) // size
    page = max(1, min(page, pages))
    start = (page - 1) * size
    end = start + size
    return items[start:end], pages

def url_for_with_query(request: Request, **kwargs) -> str:
    url = URL(str(request.url))
    q = dict(request.query_params)
    q.update({k: str(v) for k, v in kwargs.items() if v is not None})
    return str(url.replace(query=q))

def fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def guess_mimetype(path: Path) -> str:
    t, _ = mimetypes.guess_type(path.name)
    return t or "application/octet-stream"

# ========================= Auth =========================
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if is_logged(request):
        return RedirectResponse("/panel", status_code=302)
    return HTMLResponse(f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Login – ORBET</title>
<style>
body {{ background:#0b1320; color:#e5e7eb; font-family:system-ui, sans-serif; }}
.card {{ max-width:360px; margin:8vh auto; background:#121a2a; padding:24px; border-radius:14px; box-shadow:0 10px 30px rgba(0,0,0,.35); }}
h1 {{ font-size:22px; margin:0 0 16px; }}
label {{ display:block; font-size:12px; opacity:.8; margin-top:10px; }}
input {{ width:100%; padding:10px 12px; border-radius:10px; border:1px solid #273247; background:#0f172a; color:#e5e7eb; outline:none; }}
button {{ margin-top:16px; width:100%; padding:10px; border-radius:10px; border:0; background:#2563eb; color:white; font-weight:600; cursor:pointer; }}
small {{ display:block; margin-top:12px; opacity:.7; }}
</style></head>
<body>
  <div class="card">
    <h1>ORBET – Ingresar</h1>
    <form method="post" action="/login">
      <label>Usuario</label>
      <input name="username" autocomplete="username" required>
      <label>Contraseña</label>
      <input type="password" name="password" autocomplete="current-password" required>
      <button>Entrar</button>
    </form>
    <small>Consejo: cambia la clave vía variables de entorno (ADMIN_USER / ADMIN_PASS).</small>
  </div>
</body></html>
""")

@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["user"] = ADMIN_USER
        return RedirectResponse("/panel", status_code=302)
    return RedirectResponse("/login", status_code=302)

@app.get("/logout")
def do_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

# ========================= Vistas =========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_logged(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/panel", status_code=302)

@app.get("/panel", response_class=HTMLResponse)
def panel(
    request: Request,
    tipo: str = "capturas",        # "capturas" | "tickets"
    desde: Optional[str] = None,   # "YYYY-MM-DD"
    hasta: Optional[str] = None,   # "YYYY-MM-DD"
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
):
    login_required(request)

    # Rango por defecto = hoy
    today = datetime.utcnow().date()
    f_from = parse_date(desde) or datetime.combine(today, datetime.min.time())
    f_to = parse_date(hasta) or datetime.combine(today, datetime.min.time())

    root = CAPTURES_DIR if tipo == "capturas" else TICKETS_DIR
    files = collect_files(root, f_from, f_to)
    page_items, total_pages = slice_page(files, page, size)

    # Conteos rápidos (hoy)
    cnt_caps = len(collect_files(CAPTURES_DIR, f_from, f_to))
    cnt_ticks = len(collect_files(TICKETS_DIR, f_from, f_to))

    # Construcción del grid
    cards = []
    for p in page_items:
        is_img = p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        thumb = f"/files/{p.relative_to(UPLOAD_ROOT).as_posix()}"
        fecha = p.parent.name
        open_btn = f'<a href="{thumb}" target="_blank" class="btn">Abrir</a>'
        if is_img:
            cards.append(f"""
<div class="card">
  <img src="{thumb}" alt="">
  <div class="meta">
    <span>{fecha}</span>
    {open_btn}
  </div>
</div>""")
        else:
            cards.append(f"""
<div class="card">
  <div class="file">{p.name}</div>
  <div class="meta">
    <span>{fecha}</span>
    <a href="{thumb}" target="_blank" class="btn">Abrir</a>
  </div>
</div>""")

    # Navegación de páginas
    prev_link = url_for_with_query(request, page=max(1, page-1))
    next_link = url_for_with_query(request, page=min(total_pages or 1, page+1))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{APP_TITLE}</title>
<style>
:root {{ --bg:#0b1320; --panel:#0f172a; --muted:#8aa0c5; --text:#e5e7eb; --pri:#2563eb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:system-ui, sans-serif; }}
.topbar {{ display:flex; gap:16px; align-items:center; padding:14px 18px; background:#0b1220; border-bottom:1px solid #1e293b; position:sticky; top:0; z-index:10; }}
.brand {{ font-weight:800; letter-spacing:.2px; }}
.badge {{ background:#1e293b; padding:4px 8px; border-radius:999px; font-size:12px; }}
.container {{ max-width:1200px; margin:18px auto; padding:0 14px; }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:end; margin-bottom:14px; }}
select, input[type=date] {{ background:#0f172a; color:var(--text); border:1px solid #22314a; border-radius:10px; padding:8px 10px; }}
button, .btn {{ background:var(--pri); color:white; border:0; border-radius:10px; padding:8px 12px; font-weight:600; text-decoration:none; cursor:pointer; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }}
.card {{ background:var(--panel); border:1px solid #1e293b; border-radius:16px; overflow:hidden; }}
.card img {{ width:100%; height:200px; object-fit:cover; display:block; }}
.card .file {{ padding:20px; word-break:break-all; opacity:.9; }}
.card .meta {{ display:flex; justify-content:space-between; align-items:center; padding:8px 12px 12px; }}
.pager {{ display:flex; gap:8px; align-items:center; justify-content:center; margin:18px 0; }}
a.muted {{ color:#9fb0d0; text-decoration:none; }}
.right {{ margin-left:auto; display:flex; gap:8px; align-items:center; }}
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">ORBET – Panel</div>
    <span class="badge">Capturas: {cnt_caps}</span>
    <span class="badge">Tickets: {cnt_ticks}</span>
    <div class="right">
      <a class="muted" href="/logout">Salir</a>
    </div>
  </div>

  <div class="container">
    <form class="controls" method="get" action="/panel">
      <div>
        <div style="font-size:12px;opacity:.8;">Tipo</div>
        <select name="tipo">
          <option value="capturas" {"selected" if tipo=="capturas" else ""}>Capturas</option>
          <option value="tickets" {"selected" if tipo=="tickets" else ""}>Tickets</option>
        </select>
      </div>
      <div>
        <div style="font-size:12px;opacity:.8;">Desde</div>
        <input type="date" name="desde" value="{fmt_date(f_from)}">
      </div>
      <div>
        <div style="font-size:12px;opacity:.8;">Hasta</div>
        <input type="date" name="hasta" value="{fmt_date(f_to)}">
      </div>
      <div>
        <div style="font-size:12px;opacity:.8;">Tamaño</div>
        <select name="size">
          {"".join(f'<option value="{s}" {"selected" if s==size else ""}>{s}</option>' for s in (12,24,48,96))}
        </select>
      </div>
      <div>
        <button>Filtrar</button>
      </div>
      <div class="right">
        <a class="btn" href="{url_for_with_query(request, desde=fmt_date(datetime.utcnow().date()), hasta=fmt_date(datetime.utcnow().date()), page=1)}">Hoy</a>
        <a class="btn" href="{url_for_with_query(request, desde=fmt_date(datetime.utcnow().date()-timedelta(days=6)), hasta=fmt_date(datetime.utcnow().date()), page=1)}">Últimos 7 días</a>
        <a class="btn" href="/export_csv?{request.url.query}">⬇️ CSV</a>
        <a class="btn" href="/export_zip?{request.url.query}">⬇️ ZIP</a>
      </div>
    </form>

    <div class="grid">
      {"".join(cards) if cards else "<div style='opacity:.7'>Sin resultados.</div>"}
    </div>

    <div class="pager">
      <a class="btn" href="{prev_link}">◀︎ Anterior</a>
      <span>Página {page} de {total_pages or 1}</span>
      <a class="btn" href="{next_link}">Siguiente ▶︎</a>
    </div>
  </div>
</body></html>
"""
    return HTMLResponse(html)

# ========================= Descargas =========================
@app.get("/export_csv")
def export_csv(
    request: Request,
    tipo: str = "capturas",
    desde: Optional[str] = None,
    hasta: Optional[str] = None
):
    login_required(request)
    f_from = parse_date(desde)
    f_to = parse_date(hasta)
    root = CAPTURES_DIR if tipo == "capturas" else TICKETS_DIR
    files = collect_files(root, f_from, f_to)

    def gen():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["tipo", "fecha", "archivo", "ruta"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for p in files:
            writer.writerow([tipo, p.parent.name, p.name, f"/files/{p.relative_to(UPLOAD_ROOT).as_posix()}"])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    filename = f"orbet_{tipo}_{(desde or 'all')}_{(hasta or 'all')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(gen(), media_type="text/csv", headers=headers)

@app.get("/export_zip")
def export_zip(
    request: Request,
    tipo: str = "capturas",
    desde: Optional[str] = None,
    hasta: Optional[str] = None
):
    login_required(request)
    f_from = parse_date(desde)
    f_to = parse_date(hasta)
    root = CAPTURES_DIR if tipo == "capturas" else TICKETS_DIR
    files = collect_files(root, f_from, f_to)

    def stream():
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in files:
                arcname = f"{p.parent.name}/{p.name}"
                try:
                    z.write(p, arcname=arcname)
                except FileNotFoundError:
                    continue
        mem.seek(0)
        yield mem.read()

    filename = f"orbet_{tipo}_{(desde or 'all')}_{(hasta or 'all')}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(stream(), media_type="application/zip", headers=headers)

# ========================= Archivos protegidos =========================
@app.get("/files/{path:path}")
def serve_file(path: str, request: Request):
    login_required(request)
    full = (UPLOAD_ROOT / path).resolve()
    if not str(full).startswith(str(UPLOAD_ROOT)) or not full.exists() or not full.is_file():
        raise HTTPException(404, "No encontrado")
    def fileiter():
        with open(full, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
    return StreamingResponse(fileiter(), media_type=guess_mimetype(full))

# ========================= Endpoint de subida desde el detector =========================
@app.post("/upload")
async def upload_file(token: str = Form(...), kind: str = Form(...), file: UploadFile = File(...)):
    if token != ORBET_UPLOAD_TOKEN:
        raise HTTPException(401, "Token inválido")

    if kind not in {"captura", "ticket"}:
        raise HTTPException(400, "kind inválido")

    # Guardar siempre en subcarpeta por fecha: YYYY-MM-DD
    day_dir = (CAPTURES_DIR if kind == "captura" else TICKETS_DIR) / datetime.utcnow().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    dest = day_dir / file.filename

    # Evitar sobrescribir
    i = 1
    base = dest.stem
    while dest.exists():
        dest = day_dir / f"{base}_{i}{Path(file.filename).suffix}"
        i += 1

    with open(dest, "wb") as f:
        f.write(await file.read())

    rel_url = f"/files/{dest.relative_to(UPLOAD_ROOT).as_posix()}"
    return {"ok": True, "url": rel_url}

# ========================= Salud =========================
@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")
