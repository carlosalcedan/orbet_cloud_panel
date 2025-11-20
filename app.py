# app.py — ORBET Cloud Panel
# Funciones: Login + Roles (admin/cliente) + Multi-tenant por usuario (carpetas) +
# /files protegido + Filtro por categoría + Paginación + Export CSV + Delete (solo admin)
#
# Requisitos: fastapi uvicorn python-multipart itsdangerous Pillow
#
# Variables de entorno recomendadas (Render):
#   SECRET_KEY       = <cadena larga aleatoria>
#   USERS            = "admin:admin123:admin,cliente1:1234:cliente"   # usuario:pass:rol
#   ORBET_TOKEN      = "ORBET_2025_Seguridad_ARES"                    # token global (opcional)
#   ORBET_TOKENS     = "admin:AAA111,cliente1:BBB222,cliente2:CCC333" # tokens por owner (opcional)
#   DATA_DIR         = "data"
#   RETENTION_DAYS   = "0"   # 0 = sin limpieza, o días a retener
#
# Subida desde ORBET local:
#   POST /upload (multipart/form-data)
#     token=<token del cliente>  (si ORBET_TOKENS está definido, se valida contra owner)
#     kind=captura|ticket
#     owner=<usuario destino>  (por ejemplo: mercado1, empresaX)
#     file=@archivo.jpg
#
# NOTA:
# - Si ORBET_TOKENS está definido, se usa para validar token+owner.
# - Si ORBET_TOKENS está vacío, se usa ORBET_TOKEN global como antes.

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
DB_FILE = DATA_DIR / "orbet_cloud.db"   # BD del panel (tickets, etc.)

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
    # Creamos solo el directorio raíz; subcarpetas se crean on-demand
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def secure_name(name: str) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(c if c in keep else "_" for c in name)

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _category_from_name(name: str) -> str:
    n = name.lower()
    if "persona" in n: return "PERSONA"
    if any(x in n for x in ("auto", "car", "vehic", "truck", "bus")): return "AUTO"
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
    # Borramos por debajo de DATA_DIR, recursivo por owners/kind/day
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
    # kind: "capturas" | "tickets"
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

# -------------------- DB: Tickets vehiculares --------------------
def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets_vehiculares (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        owner               TEXT NOT NULL,
        fecha               TEXT NOT NULL,             -- YYYY-MM-DD
        correlativo_dia     INTEGER NOT NULL,          -- 1,2,3... por día y owner

        captura_date        TEXT NOT NULL,             -- carpeta día de la captura
        captura_name        TEXT NOT NULL,             -- nombre de archivo

        fecha_hora          TEXT NOT NULL,             -- ISO: YYYY-MM-DDTHH:MM:SS
        tipo_movimiento     TEXT NOT NULL,             -- ENTRADA / SALIDA
        placa               TEXT,
        chofer_nombre       TEXT,
        chofer_dni          TEXT,
        empresa_transporte  TEXT,
        tipo_vehiculo       TEXT,
        tipo_material       TEXT,
        guia_remision       TEXT,
        observaciones       TEXT,

        creado_por          TEXT,
        creado_en           TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()

# -------------------- Token validation --------------------
def _validate_token(owner: str, token: str):
    """
    Valida el token recibido desde ORBET local.
    - Si TOKENS (ORBET_TOKENS) está definido, exige que el token coincida con el del owner.
    - Si TOKENS está vacío, usa ORBET_TOKEN global (compatibilidad con versiones anteriores).
    """
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
    try:
        cleanup_old(RETENTION_DAYS)
    except Exception:
        pass
    try:
        init_db()
    except Exception as e:
        print("Error init_db:", e)

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
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:22px;width:360px;box-shadow:0 6px 18px rgba(0,0,0,.25)}
h1{font-size:20px;margin:0 0 12px}
label{display:block;font-size:12px;opacity:.9;margin:8px 0 4px}
input,button{width:100%;background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:10px}
button{cursor:pointer;margin-top:10px}
.msg{color:#ff9b9b;font-size:12px;margin:8px 0}
.small{opacity:.8;font-size:12px;margin-top:8px}
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
    owner: str = Form("admin"),  # nombre de usuario destino => carpeta
    background: BackgroundTasks = None,
):
    # Validar token según owner
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
    # restrict traversal
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
    # cliente: solo su propio owner
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
@app.get("/api/list", summary="Lista archivos con filtros, categoría y paginación")
def api_list(
    req: Request,
    # scope de datos
    owner: Optional[str] = Query(None, description="Owner (solo admin; si no, se usa el propio usuario)"),
    kind: str = Query("capturas", pattern="^(capturas|tickets)$"),
    # filtros
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    cat: Optional[str] = Query(None, description="PERSONA|AUTO|ANIMAL|OTROS"),
    # paginación
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    require_login(req)
    role = current_role(req)
    user = current_user(req)

    if role != "admin":
        owner = user   # cliente restringido a su carpeta
    else:
        owner = owner or user  # por defecto, el admin puede consultar su carpeta; puede elegir otra

    s = parse_ymd(start) if start else None
    e = parse_ymd(end) if end else None

    items_all = list_items(owner, kind, s, e, q, cat)
    total = len(items_all)

    # paginación
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    items = items_all[start_idx:end_idx]

    # conteo por categoría (de los items filtrados completos, no solo la página)
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

# -------------------- Tickets: detalle de captura --------------------
@app.get("/api/capture_detail")
def capture_detail(
    req: Request,
    owner: str = Query(...),
    kind: str = Query(...),
    day: str = Query(..., description="YYYY-MM-DD"),
    name: str = Query(...),
):
    require_login(req)
    if kind != "capturas":
        raise HTTPException(status_code=400, detail="Solo válido para capturas")

    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")

    url = f"/files/{secure_name(owner)}/capturas/{secure_name(day)}/{secure_name(name)}"

    # Intentar inferir fecha_hora a partir del nombre (prefijo HH-MM-SS_)
    fecha_hora = None
    try:
        stamp = name.split("_", 1)[0]               # "14-37-22"
        hhmmss = stamp.replace("-", ":")            # "14:37:22"
        fecha_hora = f"{day}T{hhmmss}"
    except Exception:
        fecha_hora = f"{day}T00:00:00"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM tickets_vehiculares
        WHERE owner = ? AND captura_date = ? AND captura_name = ?
        LIMIT 1
    """, (owner, day, name))
    row = cur.fetchone()
    conn.close()

    ticket = {"has_ticket": False}
    if row:
        ticket = {
            "has_ticket": True,
            "id": row["id"],
            "correlativo_dia": row["correlativo_dia"],
            "fecha": row["fecha"],
            "fecha_hora": row["fecha_hora"],
            "tipo_movimiento": row["tipo_movimiento"],
            "placa": row["placa"],
            "chofer_nombre": row["chofer_nombre"],
            "chofer_dni": row["chofer_dni"],
            "empresa_transporte": row["empresa_transporte"],
            "tipo_vehiculo": row["tipo_vehiculo"],
            "tipo_material": row["tipo_material"],
            "guia_remision": row["guia_remision"],
            "observaciones": row["observaciones"],
            "creado_por": row["creado_por"],
        }
        fecha_hora = row["fecha_hora"]

    return {
        "ok": True,
        "owner": owner,
        "captura_date": day,
        "captura_name": name,
        "url": url,
        "fecha_hora": fecha_hora,
        "ticket": ticket,
    }

# -------------------- Tickets: crear / actualizar --------------------
@app.post("/api/ticket")
def upsert_ticket(
    req: Request,
    owner: str = Form(...),
    captura_date: str = Form(...),
    captura_name: str = Form(...),
    tipo_movimiento: str = Form(...),
    placa: str = Form(""),
    chofer_nombre: str = Form(""),
    chofer_dni: str = Form(""),
    empresa_transporte: str = Form(""),
    tipo_vehiculo: str = Form(""),
    tipo_material: str = Form(""),
    guia_remision: str = Form(""),
    observaciones: str = Form(""),
):
    require_login(req)
    user = current_user(req)

    if not _can_access_owner(req, owner):
        raise HTTPException(status_code=403, detail="Permisos insuficientes")

    tipo_movimiento = tipo_movimiento.upper().strip()
    if tipo_movimiento not in ("ENTRADA", "SALIDA"):
        raise HTTPException(status_code=400, detail="tipo_movimiento debe ser ENTRADA o SALIDA")

    fecha = captura_date
    try:
        stamp = captura_name.split("_", 1)[0]
        hhmmss = stamp.replace("-", ":")
        fecha_hora = f"{fecha}T{hhmmss}"
    except Exception:
        fecha_hora = f"{fecha}T00:00:00"

    conn = get_db()
    cur = conn.cursor()

    # ¿Ya existe ticket para esta captura?
    cur.execute("""
        SELECT id, correlativo_dia
        FROM tickets_vehiculares
        WHERE owner = ? AND captura_date = ? AND captura_name = ?
        LIMIT 1
    """, (owner, captura_date, captura_name))
    row = cur.fetchone()

    if row:
        ticket_id = row["id"]
        correlativo_dia = row["correlativo_dia"]
        cur.execute("""
            UPDATE tickets_vehiculares
            SET fecha_hora = ?, tipo_movimiento = ?, placa = ?, chofer_nombre = ?,
                chofer_dni = ?, empresa_transporte = ?, tipo_vehiculo = ?,
                tipo_material = ?, guia_remision = ?, observaciones = ?,
                creado_por = ?
            WHERE id = ?
        """, (
            fecha_hora, tipo_movimiento, placa, chofer_nombre,
            chofer_dni, empresa_transporte, tipo_vehiculo,
            tipo_material, guia_remision, observaciones,
            user, ticket_id
        ))
    else:
        cur.execute("""
            SELECT COALESCE(MAX(correlativo_dia), 0) AS max_corr
            FROM tickets_vehiculares
            WHERE owner = ? AND fecha = ?
        """, (owner, fecha))
        max_row = cur.fetchone()
        next_corr = (max_row["max_corr"] if max_row else 0) + 1

        cur.execute("""
            INSERT INTO tickets_vehiculares (
                owner, fecha, correlativo_dia,
                captura_date, captura_name,
                fecha_hora, tipo_movimiento, placa, chofer_nombre,
                chofer_dni, empresa_transporte, tipo_vehiculo,
                tipo_material, guia_remision, observaciones,
                creado_por
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            owner, fecha, next_corr,
            captura_date, captura_name,
            fecha_hora, tipo_movimiento, placa, chofer_nombre,
            chofer_dni, empresa_transporte, tipo_vehiculo,
            tipo_material, guia_remision, observaciones,
            user
        ))
        ticket_id = cur.lastrowid
        correlativo_dia = next_corr

    conn.commit()
    conn.close()

    return {"ok": True, "id": ticket_id, "correlativo_dia": correlativo_dia}

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

    def gen():
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow(["owner", "fecha", "tipo", "categoria", "nombre_archivo", "url"])
        for it in items:
            writer.writerow([it["owner"], it["date"], it["kind"], it["cat"], it["name"], it["url"]])
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
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 12px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
select,input,button{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:6px 10px}
.badge{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease}
.item.show{opacity:1}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.btn{background:#24305a;border:none;border-radius:8px;padding:4px 8px;color:#e7e7ee;text-decoration:none;cursor:pointer}
a{color:#9ecbff}
.right{margin-left:auto}
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
const owner=$("#owner"), prev=$("#prev"), next=$("#next"), pageInfo=$("#pageInfo");
let page=1, pages=1, limit=50;

const cardId=url=>"i_"+btoa(url).replace(/=/g,"");
function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  if(!el){
    const isImg=/\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumb=isImg?`<img loading="lazy" src="${it.url}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    const delBtn = canDelete? `<button class="btn" data-del="${it.url}" data-owner="${it.owner}" data-kind="${it.kind}" data-date="${it.date}" data-name="${it.name}">🗑</button>` : "";
    el=document.createElement("div"); el.className="item"; el.id=id;
    el.innerHTML=thumb+`<div class="meta"><span>${it.date} · ${it.cat}</span><div><a class="btn" target="_blank" href="${it.url}">Abrir</a> ${delBtn}</div></div>`;
    grid.appendChild(el); requestAnimationFrame(()=>el.classList.add("show"));
  }
  return el;
}
function bindDeletes(){
  if(!canDelete) return;
  grid.querySelectorAll("[data-del]").forEach(btn=>{
    btn.onclick=async ()=>{
      if(!confirm("¿Borrar este archivo?")) return;
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
  grid.innerHTML=""; // simple: reemplazo por página
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
body{background:#0f1220;color:#e7e7ee;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
.wrap{max-width:1200px;margin:auto}
h1{font-size:26px;margin:0 0 14px}
.card{background:#171a2b;border:1px solid #262a41;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
.row label{display:flex;flex-direction:column;font-size:12px;opacity:.9}
input,select,button{background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:8px 10px}
button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.item{background:#11152a;border:1px solid #2a2f4a;border-radius:12px;padding:10px;opacity:0;transition:opacity .25s ease;cursor:pointer}
.item.show{opacity:1}
.item img{width:100%;display:block;border-radius:10px}
.meta{font-size:12px;opacity:.85;margin-top:6px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.badge{font-size:11px;background:#24305a;padding:4px 8px;border-radius:999px}
.stats{display:flex;gap:8px;flex-wrap:wrap}
.btn{background:#24305a;border:none;border-radius:8px;padding:4px 8px;color:#e7e7ee;text-decoration:none;cursor:pointer}
a{color:#9ecbff}
.right{margin-left:auto}

/* Modal ticket */
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:50}
.modal{background:#171a2b;border-radius:16px;border:1px solid #262a41;max-width:900px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 12px 30px rgba(0,0,0,.4);padding:16px}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.modal-body{display:flex;gap:12px;flex-wrap:wrap}
.modal-body img{max-width:380px;width:100%;border-radius:10px;background:#000}
.modal-form{flex:1;min-width:260px}
.modal-form label{font-size:12px;opacity:.9;display:block;margin-top:6px}
.modal-form input,.modal-form select,.modal-form textarea{width:100%;background:#0f1220;color:#e7e7ee;border:1px solid #2b3050;border-radius:10px;padding:6px 8px;font-size:13px}
.modal-form textarea{min-height:70px;resize:vertical}
.hidden{display:none}
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
      <label>Desde <input type="date" id="start"></label>
      <label>Hasta <input type="date" id="end"></label>
      <label>Buscar (texto) <input type="text" id="q" placeholder="persona / auto / animal / texto"></label>
      <label>Página <input type="number" id="page" min="1" value="1" style="width:90px"></label>
      <label>Por página
        <select id="limit"><option>25</option><option selected>50</option><option>100</option><option>200</option></select>
      </label>
      <button type="submit">Aplicar</button>
      <a href="/">Vista rápida</a>
      <a id="dl" href="#" download>⬇️ CSV</a>
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

<!-- Modal Ticket Vehicular -->
<div id="ticketBackdrop" class="modal-backdrop hidden">
  <div class="modal">
    <div class="modal-header">
      <h2>Ticket vehicular</h2>
      <button id="ticketClose" class="btn" type="button">Cerrar ✖</button>
    </div>
    <div class="modal-body">
      <div>
        <img id="ticketImg" src="" alt="Captura">
        <div style="font-size:12px;opacity:.8;margin-top:4px">
          Fecha/hora: <span id="t_fecha_hora"></span><br>
          N° diario: <span id="t_correlativo">-</span>
        </div>
      </div>
      <form id="ticketForm" class="modal-form">
        <input type="hidden" id="t_owner">
        <input type="hidden" id="t_captura_date">
        <input type="hidden" id="t_captura_name">

        <label>Tipo de movimiento
          <select id="t_tipo_movimiento">
            <option value="ENTRADA">ENTRADA</option>
            <option value="SALIDA">SALIDA</option>
          </select>
        </label>

        <label>Placa
          <input id="t_placa" placeholder="V7M-842">
        </label>

        <label>Nombre del chofer
          <input id="t_chofer_nombre" placeholder="Nombre completo">
        </label>

        <label>DNI del chofer
          <input id="t_chofer_dni" placeholder="DNI">
        </label>

        <label>Empresa transportista
          <input id="t_empresa_transporte" placeholder="Empresa o razón social">
        </label>

        <label>Tipo de vehículo
          <input id="t_tipo_vehiculo" placeholder="Camión, cisterna, camioneta...">
        </label>

        <label>Tipo de material
          <input id="t_tipo_material" placeholder="Fertilizante, repuestos, etc.">
        </label>

        <label>N° Guía de remisión
          <input id="t_guia_remision" placeholder="001-000123">
        </label>

        <label>Observaciones
          <textarea id="t_observaciones" placeholder="EPP, sellos, novedades..."></textarea>
        </label>

        <button type="submit" class="btn" style="margin-top:10px">Guardar ticket</button>
      </form>
    </div>
  </div>
</div>

<script>
let canDelete=false, myRole="cliente", myUser="";
const $=sel=>document.querySelector(sel);
const grid=$("#grid"), countEl=$("#count"), pagesEl=$("#pages"), dl=$("#dl");
const kind=$("#kind"), start=$("#start"), end=$("#end"), q=$("#q"), cat=$("#cat");
const owner=$("#owner"), page=$("#page"), limit=$("#limit");
const cP=$("#cP"), cA=$("#cA"), cN=$("#cN"), cO=$("#cO");

// Modal refs
const backdrop=$("#ticketBackdrop");
const ticketImg=$("#ticketImg");
const t_fecha_hora=$("#t_fecha_hora");
const t_correlativo=$("#t_correlativo");
const ticketForm=$("#ticketForm");
const ticketClose=$("#ticketClose");
const t_owner=$("#t_owner");
const t_captura_date=$("#t_captura_date");
const t_captura_name=$("#t_captura_name");
const t_tipo_movimiento=$("#t_tipo_movimiento");
const t_placa=$("#t_placa");
const t_chofer_nombre=$("#t_chofer_nombre");
const t_chofer_dni=$("#t_chofer_dni");
const t_empresa_transporte=$("#t_empresa_transporte");
const t_tipo_vehiculo=$("#t_tipo_vehiculo");
const t_tipo_material=$("#t_tipo_material");
const t_guia_remision=$("#t_guia_remision");
const t_observaciones=$("#t_observaciones");

let currentOwner="", currentDate="", currentName="";

const cardId=url=>"i_"+btoa(url).replace(/=/g,"");

function ensureCard(it){
  const id=cardId(it.url); let el=document.getElementById(id);
  if(!el){
    const isImg=/\.(jpg|jpeg|png|gif|webp)$/i.test(it.name);
    const thumb=isImg?`<img loading="lazy" src="${it.url}" alt="${it.name}">`:`<div style="padding:20px;font-size:13px;opacity:.9">📄 ${it.name}</div>`;
    const delBtn = canDelete? `<button class="btn" data-del="${it.url}" data-owner="${it.owner}" data-kind="${it.kind}" data-date="${it.date}" data-name="${it.name}">🗑</button>` : "";
    el=document.createElement("div"); el.className="item"; el.id=id;
    el.dataset.owner = it.owner;
    el.dataset.kind  = it.kind;
    el.dataset.date  = it.date;
    el.dataset.name  = it.name;
    el.innerHTML=thumb+`<div class="meta"><span>${it.date} · ${it.cat}</span><div><a class="btn" target="_blank" href="${it.url}">Abrir</a> ${delBtn}</div></div>`;
    grid.appendChild(el); requestAnimationFrame(()=>el.classList.add("show"));
  }
  return el;
}
function bindDeletes(){
  if(!canDelete) return;
  grid.querySelectorAll("[data-del]").forEach(btn=>{
    btn.onclick=async (ev)=>{
      ev.stopPropagation();
      if(!confirm("¿Borrar este archivo?")) return;
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
}

// Abrir modal al hacer clic en una captura
grid.addEventListener("click", async (ev)=>{
  const card = ev.target.closest(".item");
  if(!card) return;
  const ownerC = card.dataset.owner;
  const kindC  = card.dataset.kind;
  const dateC  = card.dataset.date;
  const nameC  = card.dataset.name;
  if(kindC !== "capturas") return; // solo capturas para tickets

  const qs = new URLSearchParams({ owner: ownerC, kind: kindC, day: dateC, name: nameC });
  const r = await fetch("/api/capture_detail?"+qs.toString());
  if(!r.ok){
    alert("No se pudo cargar detalle de la captura");
    return;
  }
  const j = await r.json();

  currentOwner = ownerC;
  currentDate  = dateC;
  currentName  = nameC;

  ticketImg.src = j.url;
  t_fecha_hora.textContent = j.fecha_hora || (dateC);
  t_owner.value = currentOwner;
  t_captura_date.value = currentDate;
  t_captura_name.value = currentName;

  const t = j.ticket || {};
  if(t.has_ticket){
    t_correlativo.textContent = t.correlativo_dia || "-";
    t_tipo_movimiento.value   = t.tipo_movimiento || "ENTRADA";
    t_placa.value             = t.placa || "";
    t_chofer_nombre.value     = t.chofer_nombre || "";
    t_chofer_dni.value        = t.chofer_dni || "";
    t_empresa_transporte.value= t.empresa_transporte || "";
    t_tipo_vehiculo.value     = t.tipo_vehiculo || "";
    t_tipo_material.value     = t.tipo_material || "";
    t_guia_remision.value     = t.guia_remision || "";
    t_observaciones.value     = t.observaciones || "";
  } else {
    t_correlativo.textContent = "-";
    t_tipo_movimiento.value   = "ENTRADA";
    t_placa.value             = "";
    t_chofer_nombre.value     = "";
    t_chofer_dni.value        = "";
    t_empresa_transporte.value= "";
    t_tipo_vehiculo.value     = "";
    t_tipo_material.value     = "";
    t_guia_remision.value     = "";
    t_observaciones.value     = "";
  }

  backdrop.classList.remove("hidden");
});

// Cerrar modal
ticketClose.addEventListener("click", ()=>{ backdrop.classList.add("hidden"); });
backdrop.addEventListener("click", (ev)=>{
  if(ev.target === backdrop){ backdrop.classList.add("hidden"); }
});

// Guardar ticket
ticketForm.addEventListener("submit", async (ev)=>{
  ev.preventDefault();
  if(!currentOwner || !currentDate || !currentName){
    alert("Falta información de la captura");
    return;
  }
  const data = new URLSearchParams();
  data.set("owner", currentOwner);
  data.set("captura_date", currentDate);
  data.set("captura_name", currentName);
  data.set("tipo_movimiento", t_tipo_movimiento.value);
  data.set("placa", t_placa.value);
  data.set("chofer_nombre", t_chofer_nombre.value);
  data.set("chofer_dni", t_chofer_dni.value);
  data.set("empresa_transporte", t_empresa_transporte.value);
  data.set("tipo_vehiculo", t_tipo_vehiculo.value);
  data.set("tipo_material", t_tipo_material.value);
  data.set("guia_remision", t_guia_remision.value);
  data.set("observaciones", t_observaciones.value);

  const r = await fetch("/api/ticket", {
    method: "POST",
    body: data
  });
  if(!r.ok){
    const tx = await r.text();
    alert("Error al guardar ticket: " + tx);
    return;
  }
  const j = await r.json();
  t_correlativo.textContent = j.correlativo_dia || "-";
  alert("Ticket guardado (N° " + (j.correlativo_dia||"?") + ")");
  backdrop.classList.add("hidden");
});

async function boot(){
  const me=await fetch("/me"); if(me.status==401){ location.href="/login"; return; }
  const info=await me.json(); canDelete=(info.role==="admin");
  myRole=info.role; myUser=info.user;
  const whoEl = document.querySelector("#who");
  if(whoEl){ whoEl.textContent = "· " + myUser + " (" + myRole + ")"; }
  if(myRole!=="admin"){ owner.value=myUser; owner.disabled=true; }
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

# -------------------- Run local --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

