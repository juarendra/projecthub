"""
ProjectHub - ClickUp-style personal project manager for Juarendra.
FastAPI + stdlib sqlite3. Hierarchy: Space > List > Task > Subtask.
Auth (pbkdf2 + signed cookie) + OpenClaw AI review.
"""
import os, sqlite3, json, subprocess, datetime, shutil, re, hashlib, hmac, time, base64, uuid, mimetypes, calendar, logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Body, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "projecthub.db")
UPLOADS = os.path.join(BASE, "data", "uploads")
os.makedirs(os.path.dirname(DB), exist_ok=True)
os.makedirs(UPLOADS, exist_ok=True)

# ---- logging terpusat (rotating, ke data/app.log) ----
_logh = RotatingFileHandler(os.path.join(BASE, "data", "app.log"), maxBytes=2_000_000, backupCount=3)
_logh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log = logging.getLogger("projecthub")
log.setLevel(logging.INFO)
log.addHandler(_logh)
# SVG intentionally excluded: served inline it allows stored XSS (embedded <script>).
# SVG uploads are treated as plain files (download-only, not is_image).
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# Raster mime types safe to serve with Content-Disposition: inline.
SAFE_INLINE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB
OPENCLAW_BIN = shutil.which("openclaw") or "/usr/bin/openclaw"

# ===== Project Explorer (read-only code browser) =====
# Root folder yang dibrowse (read-only). Override via env PH_CODE_ROOT.
CODE_ROOT = os.path.realpath(os.environ.get("PH_CODE_ROOT", "/home/rendra/shared/Pribadi/Github"))
# folder yang tidak pernah ditampilkan di listing
EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}
# ext teks/kode yang aman ditampilkan inline sebagai teks
TEXT_EXT = {
    "ino", "cpp", "c", "h", "hpp", "cc", "cxx", "py", "pyi", "js", "ts", "jsx", "tsx", "mjs", "cjs",
    "json", "jsonc", "css", "scss", "sass", "less", "html", "htm", "xml", "svg", "vue", "svelte",
    "txt", "log", "yml", "yaml", "sh", "bash", "zsh", "ps1", "bat", "php", "rb", "go", "rs", "java",
    "kt", "kts", "swift", "dart", "lua", "r", "m", "sql", "toml", "ini", "cfg", "conf", "properties",
    "env", "gitignore", "dockerignore", "dockerfile", "makefile", "cmake", "gradle", "tex",
    "kicad_sch", "kicad_pcb", "kicad_mod", "kicad_pro", "net", "csv", "tsv", "md", "markdown", "rst",
}
# raster image yang aman di-serve inline (sama seperti attachment)
CODE_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# Model 3D yang bisa ditampilkan online-3d-viewer (STEP butuh occt wasm)
MODEL3D_EXT = {"stl", "obj", "step", "stp", "3mf", "gltf", "glb", "ply", "fbx", "off", "igs", "iges", "brep"}
# File KiCad yang di-render KiCanvas (board + skematik, KiCad 6+)
KICAD_EXT = {"kicad_pcb", "kicad_sch"}
# Gerber + drill (render board top/bottom via pcb-stackup)
GERBER_EXT = {"gbr", "gtl", "gbl", "gts", "gbs", "gto", "gbo", "gko", "gm1", "gm2", "gm3",
              "gtp", "gbp", "gpt", "gpb", "gd1", "gp1", "drl", "xln", "exc", "nc", "ncd"}
MAX_TEXT_VIEW = 2 * 1024 * 1024  # 2 MB cap untuk view teks
GIT_TIMEOUT = 4  # detik, subprocess git singkat
NOW = lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

DEFAULT_STATUSES = [
    {"id": "todo", "name": "To Do", "color": "#94a3b8"},
    {"id": "inprogress", "name": "In Progress", "color": "#6366f1"},
    {"id": "review", "name": "Review", "color": "#f59e0b"},
    {"id": "done", "name": "Complete", "color": "#10b981"},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS spaces(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, color TEXT DEFAULT '#6366f1', icon TEXT DEFAULT '🚀',
  position INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS lists(
  id INTEGER PRIMARY KEY AUTOINCREMENT, space_id INTEGER NOT NULL,
  name TEXT NOT NULL, color TEXT DEFAULT '#6366f1',
  statuses TEXT DEFAULT '', position INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, list_id INTEGER NOT NULL,
  parent_id INTEGER, title TEXT NOT NULL, description TEXT DEFAULT '',
  status TEXT DEFAULT 'todo', priority INTEGER DEFAULT 0,
  start_date TEXT, due_date TEXT, tags TEXT DEFAULT '',
  position INTEGER DEFAULT 0, estimate INTEGER DEFAULT 0,
  recurrence TEXT DEFAULT 'none',
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  body TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, list_id INTEGER NOT NULL,
  content TEXT NOT NULL, ok INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  stored TEXT NOT NULL, original TEXT NOT NULL, mime TEXT, size INTEGER,
  is_image INTEGER DEFAULT 0, kind TEXT DEFAULT 'file', created_at TEXT);
CREATE TABLE IF NOT EXISTS file_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
  path TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS project_meta(
  name TEXT PRIMARY KEY, status TEXT DEFAULT 'planning',
  list_id INTEGER, started_at TEXT, updated_at TEXT);
"""

PROJECT_STATUSES = ("planning", "active", "paused", "done")

@contextmanager
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con; con.commit()
    finally:
        con.close()

def migrate():
    # idempotent: add any new tasks column that an older DB doesn't yet have
    NEW_TASK_COLS = [
        ("estimate", "ALTER TABLE tasks ADD COLUMN estimate INTEGER DEFAULT 0"),
        ("recurrence", "ALTER TABLE tasks ADD COLUMN recurrence TEXT DEFAULT 'none'"),
        ("completed_at", "ALTER TABLE tasks ADD COLUMN completed_at TEXT"),
    ]
    with db() as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()]
        for name, ddl in NEW_TASK_COLS:
            if name not in cols:
                con.execute(ddl)

def seed():
    with db() as con:
        con.executescript(SCHEMA)
        n = con.execute("SELECT COUNT(*) c FROM spaces").fetchone()["c"]
        if n == 0:
            con.execute("INSERT INTO spaces(name,color,icon,position,created_at) VALUES(?,?,?,?,?)",
                        ("Personal", "#6366f1", "🚀", 0, NOW()))
            sid = con.execute("SELECT id FROM spaces LIMIT 1").fetchone()["id"]
            con.execute("INSERT INTO lists(space_id,name,color,statuses,position,created_at) VALUES(?,?,?,?,?,?)",
                        (sid, "My Tasks", "#6366f1", json.dumps(DEFAULT_STATUSES), 0, NOW()))
def _daily_backup():
    """Backup DB harian ke data/backups/, simpan 7 terakhir. Idempotent per hari."""
    try:
        bdir = os.path.join(BASE, "data", "backups")
        os.makedirs(bdir, exist_ok=True)
        today = datetime.date.today().isoformat()
        dest = os.path.join(bdir, f"projecthub-{today}.db")
        if not os.path.exists(dest) and os.path.exists(DB):
            # konsisten: pakai sqlite backup API
            src = sqlite3.connect(DB)
            dst = sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            src.close(); dst.close()
        # rotasi: simpan 7 terbaru
        files = sorted([f for f in os.listdir(bdir) if f.startswith("projecthub-") and f.endswith(".db")])
        for old in files[:-7]:
            try:
                os.remove(os.path.join(bdir, old))
            except OSError:
                pass
    except Exception:
        pass

seed()
migrate()
_daily_backup()

app = FastAPI(title="ProjectHub")

# ================= Auth =================
AUTH_FILE = os.path.join(BASE, "data", "auth.json")
COOKIE = "ph_auth"; SESSION_DAYS = 30
OPEN_PATHS = ("/login", "/logout", "/health", "/favicon.ico")

def load_auth():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE) as f: return json.load(f)
        except Exception: return None
    return None
AUTH = load_auth()

def _pbkdf2(pw, salt): return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
def verify_password(pw): return True if not AUTH else hmac.compare_digest(_pbkdf2(pw, AUTH["salt"]), AUTH["hash"])
def make_token(user):
    exp = int(time.time()) + SESSION_DAYS*86400; msg = f"{user}.{exp}"
    sig = hmac.new(bytes.fromhex(AUTH["secret"]), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}.{sig}".encode()).decode()
def valid_token(tok):
    if not AUTH or not tok: return False
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        user, exp, sig = raw.rsplit(".", 2)
        if int(exp) < time.time(): return False
        good = hmac.new(bytes.fromhex(AUTH["secret"]), f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and user == AUTH["user"]
    except Exception: return False

def internal_key():
    if not AUTH: return ""
    return hmac.new(bytes.fromhex(AUTH["secret"]), b"internal-digest", hashlib.sha256).hexdigest()

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if AUTH:
        path = request.url.path
        # internal digest endpoints: allow via shared key (for cron/openclaw on localhost)
        if path == "/api/digest" or path.startswith("/api/digest/"):
            if hmac.compare_digest(request.query_params.get("key", ""), internal_key()):
                return await call_next(request)
        is_asset = path == "/assets" or path.startswith("/assets/")
        if not (path in OPEN_PATHS or is_asset) and not valid_token(request.cookies.get(COOKIE)):
            if path.startswith("/api"): return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
    return await call_next(request)

@app.middleware("http")
async def log_mw(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("EXC %s %s", request.method, request.url.path)
        raise
    if resp.status_code >= 500:
        log.error("%s %s -> %s", request.method, request.url.path, resp.status_code)
    return resp

@app.get("/login")
def login_page(): return FileResponse(os.path.join(BASE, "static", "login.html"))

@app.post("/login")
def do_login(body: dict = Body(...)):
    if not AUTH: return {"ok": True}
    if (body.get("username") or "").strip() == AUTH["user"] and verify_password(body.get("password") or ""):
        resp = JSONResponse({"ok": True})
        # Secure only when served over HTTPS (PH_HTTPS=1); LAN HTTP keeps it off so the cookie sticks.
        secure = os.environ.get("PH_HTTPS") == "1"
        resp.set_cookie(COOKIE, make_token(AUTH["user"]), max_age=SESSION_DAYS*86400,
                        httponly=True, samesite="lax", secure=secure, path="/")
        return resp
    raise HTTPException(401, "Username atau password salah")

@app.post("/logout")
def do_logout():
    resp = JSONResponse({"ok": True}); resp.delete_cookie(COOKIE, path="/"); return resp

# ================= Helpers =================
def row(r): return dict(r) if r else None
def rows(rs): return [dict(r) for r in rs]

def _clamp_int(v, lo, hi, default=0):
    try: n = int(v)
    except (TypeError, ValueError): return default
    return max(lo, min(hi, n))

def list_statuses(l):
    try:
        s = json.loads(l["statuses"]) if l["statuses"] else None
        return s or DEFAULT_STATUSES
    except Exception:
        return DEFAULT_STATUSES

def task_dict(con, t):
    d = dict(t)
    subs = rows(con.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY position,id", (t["id"],)).fetchall())
    d["subtasks"] = subs
    d["sub_total"] = len(subs)
    d["sub_done"] = sum(1 for s in subs if s["status"] == "done")
    d["comment_count"] = con.execute("SELECT COUNT(*) c FROM comments WHERE task_id=?", (t["id"],)).fetchone()["c"]
    d["attachment_count"] = con.execute("SELECT COUNT(*) c FROM attachments WHERE task_id=?", (t["id"],)).fetchone()["c"]
    return d

def attachment_dict(r):
    d = dict(r)
    d["url"] = f"/api/attachments/{d['id']}"
    d["download_url"] = f"/api/attachments/{d['id']}/download"
    return d

def filelink_dict(r):
    d = dict(r)
    d["name"] = os.path.basename(d["path"])  # nama file untuk ditampilkan
    return d

def list_progress(con, lid):
    c = con.execute("SELECT COUNT(*) n, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d FROM tasks WHERE list_id=? AND parent_id IS NULL", (lid,)).fetchone()
    n = c["n"] or 0; d = c["d"] or 0
    return {"total": n, "done": d, "progress": round(100*d/n) if n else 0}

# ================= Spaces =================
@app.get("/api/spaces")
def get_spaces():
    with db() as con:
        sp = rows(con.execute("SELECT * FROM spaces ORDER BY position,id").fetchall())
        for s in sp:
            ls = rows(con.execute("SELECT * FROM lists WHERE space_id=? ORDER BY position,id", (s["id"],)).fetchall())
            for l in ls:
                p = list_progress(con, l["id"]); l.update(p)
                l["open_count"] = con.execute("SELECT COUNT(*) c FROM tasks WHERE list_id=? AND parent_id IS NULL AND status<>'done'", (l["id"],)).fetchone()["c"]
            s["lists"] = ls
        return sp

@app.post("/api/spaces")
def add_space(b: dict = Body(...)):
    with db() as con:
        mx = con.execute("SELECT COALESCE(MAX(position),0)+1 m FROM spaces").fetchone()["m"]
        cur = con.execute("INSERT INTO spaces(name,color,icon,position,created_at) VALUES(?,?,?,?,?)",
                          (b.get("name","Space").strip() or "Space", b.get("color","#6366f1"), b.get("icon","📁"), mx, NOW()))
        sid = cur.lastrowid
    return get_one_space(sid)

def get_one_space(sid):
    with db() as con:
        return row(con.execute("SELECT * FROM spaces WHERE id=?", (sid,)).fetchone())

@app.patch("/api/spaces/{sid}")
def upd_space(sid: int, b: dict = Body(...)):
    f = ["name","color","icon","position"]; sets=[f"{k}=?" for k in f if k in b]; a=[b[k] for k in f if k in b]
    if sets:
        a.append(sid)
        with db() as con: con.execute(f"UPDATE spaces SET {','.join(sets)} WHERE id=?", a)
    return get_one_space(sid)

@app.delete("/api/spaces/{sid}")
def del_space(sid: int):
    with db() as con:
        lids = [r["id"] for r in con.execute("SELECT id FROM lists WHERE space_id=?", (sid,)).fetchall()]
        for lid in lids: _purge_list(con, lid)
        con.execute("DELETE FROM spaces WHERE id=?", (sid,))
    return {"ok": True}

# ================= Lists =================
def _del_attachment_files(con, tids):
    """Delete physical files for all attachments belonging to the given task ids."""
    if not tids: return
    ph = ",".join("?" * len(tids))
    for a in con.execute(f"SELECT stored FROM attachments WHERE task_id IN ({ph})", tids).fetchall():
        try: os.remove(os.path.join(UPLOADS, a["stored"]))
        except OSError: pass

def _purge_list(con, lid):
    tids = [r["id"] for r in con.execute("SELECT id FROM tasks WHERE list_id=?", (lid,)).fetchall()]
    _del_attachment_files(con, tids)
    for tid in tids:
        con.execute("DELETE FROM comments WHERE task_id=?", (tid,))
        con.execute("DELETE FROM attachments WHERE task_id=?", (tid,))
    con.execute("DELETE FROM tasks WHERE list_id=?", (lid,))
    con.execute("DELETE FROM reviews WHERE list_id=?", (lid,))
    con.execute("DELETE FROM lists WHERE id=?", (lid,))

@app.post("/api/spaces/{sid}/lists")
def add_list(sid: int, b: dict = Body(...)):
    with db() as con:
        mx = con.execute("SELECT COALESCE(MAX(position),0)+1 m FROM lists WHERE space_id=?", (sid,)).fetchone()["m"]
        cur = con.execute("INSERT INTO lists(space_id,name,color,statuses,position,created_at) VALUES(?,?,?,?,?,?)",
                          (sid, b.get("name","List").strip() or "List", b.get("color","#6366f1"),
                           json.dumps(DEFAULT_STATUSES), mx, NOW()))
        lid = cur.lastrowid
    return get_list(lid)

@app.get("/api/lists/{lid}")
def get_list(lid: int):
    with db() as con:
        l = con.execute("SELECT * FROM lists WHERE id=?", (lid,)).fetchone()
        if not l: raise HTTPException(404, "not found")
        l = dict(l); l["statuses"] = list_statuses(l)
        l.update(list_progress(con, lid))
        ts = rows(con.execute("SELECT * FROM tasks WHERE list_id=? AND parent_id IS NULL ORDER BY position,id", (lid,)).fetchall())
        l["tasks"] = [task_dict(con, t) for t in ts]
        l["reviews"] = rows(con.execute("SELECT * FROM reviews WHERE list_id=? ORDER BY id DESC", (lid,)).fetchall())
        sp = con.execute("SELECT * FROM spaces WHERE id=?", (l["space_id"],)).fetchone()
        l["space"] = dict(sp) if sp else None
        return l

@app.patch("/api/lists/{lid}")
def upd_list(lid: int, b: dict = Body(...)):
    f = ["name","color","position","space_id"]; sets=[f"{k}=?" for k in f if k in b]; a=[b[k] for k in f if k in b]
    if "statuses" in b: sets.append("statuses=?"); a.append(json.dumps(b["statuses"]))
    if sets:
        a.append(lid)
        with db() as con: con.execute(f"UPDATE lists SET {','.join(sets)} WHERE id=?", a)
    return get_list(lid)

@app.delete("/api/lists/{lid}")
def del_list(lid: int):
    with db() as con: _purge_list(con, lid)
    return {"ok": True}

# ================= Tasks =================
@app.post("/api/lists/{lid}/tasks")
def add_task(lid: int, b: dict = Body(...)):
    with db() as con:
        l = con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()
        if not l: raise HTTPException(404, "list not found")
        valid = {s["id"] for s in list_statuses(l)}
        st = b.get("status")
        # fall back to first status if missing or unknown (avoids orphan tasks in no column)
        if not st or st not in valid:
            st = list_statuses(l)[0]["id"]
        mx = con.execute("SELECT COALESCE(MAX(position),0)+1 m FROM tasks WHERE list_id=? AND IFNULL(parent_id,0)=IFNULL(?,0)", (lid, b.get("parent_id"))).fetchone()["m"]
        cur = con.execute("""INSERT INTO tasks(list_id,parent_id,title,description,status,priority,start_date,due_date,tags,position,estimate,recurrence,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, b.get("parent_id"), (b.get("title","") or "").strip()[:500] or "Untitled", b.get("description",""), st,
             _clamp_int(b.get("priority",0), 0, 4), b.get("start_date"), b.get("due_date"), b.get("tags",""),
             mx, max(0, _clamp_int(b.get("estimate",0), 0, 100000)), b.get("recurrence","none"), NOW(), NOW()))
        tid = cur.lastrowid
        return task_dict(con, con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    with db() as con:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t: raise HTTPException(404)
        d = task_dict(con, t)
        d["comments"] = rows(con.execute("SELECT * FROM comments WHERE task_id=? ORDER BY id", (tid,)).fetchall())
        d["attachments"] = [attachment_dict(r) for r in con.execute("SELECT * FROM attachments WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        d["file_links"] = [filelink_dict(r) for r in con.execute("SELECT * FROM file_links WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        l = con.execute("SELECT * FROM lists WHERE id=?", (t["list_id"],)).fetchone()
        d["list_statuses"] = list_statuses(l) if l else DEFAULT_STATUSES
        d["list_name"] = l["name"] if l else ""
        return d

def _next_due(due, rec):
    if not due: return None
    try: d = datetime.date.fromisoformat(due)
    except Exception: return None
    if rec == "daily": d += datetime.timedelta(days=1)
    elif rec == "weekly": d += datetime.timedelta(days=7)
    elif rec == "monthly":
        m = d.month + 1; y = d.year + (1 if m > 12 else 0); m = 1 if m > 12 else m
        # clamp to the target month's last day so 31 Jan -> 28/29 Feb, but the
        # original day is preserved for months that have it (e.g. -> 31 Mar).
        last = calendar.monthrange(y, m)[1]
        d = d.replace(year=y, month=m, day=min(d.day, last))
    else: return None
    return d.isoformat()

@app.patch("/api/tasks/{tid}")
def upd_task(tid: int, b: dict = Body(...)):
    f = ["title","description","status","priority","start_date","due_date","tags","position","estimate","list_id","parent_id","recurrence"]
    # sanitize bounded fields before write
    if "priority" in b: b["priority"] = _clamp_int(b["priority"], 0, 4)
    if "estimate" in b: b["estimate"] = max(0, _clamp_int(b["estimate"], 0, 100000))
    if "recurrence" in b and b["recurrence"] not in ("none","daily","weekly","monthly"): b["recurrence"] = "none"
    if "title" in b: b["title"] = (str(b["title"]) or "").strip()[:500] or "Untitled"
    sets=[f"{k}=?" for k in f if k in b]; a=[b[k] for k in f if k in b]
    if not sets: return {"ok": True}
    with db() as con:
        before = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        # validate incoming status against the owning list's statuses (avoid orphan column)
        if "status" in b and before:
            tgt_lid = b.get("list_id", before["list_id"])
            l = con.execute("SELECT statuses FROM lists WHERE id=?", (tgt_lid,)).fetchone()
            if l:
                statuses = list_statuses(l)
                valid = {s["id"] for s in statuses}
                if b["status"] not in valid:
                    fixed = statuses[0]["id"]
                    b["status"] = fixed
                    # keep the bound parameter list in sync with the corrected value
                    a = [b[k] for k in f if k in b]
        sets2 = sets + ["updated_at=?"]; a2 = a + [NOW(), tid]
        con.execute(f"UPDATE tasks SET {','.join(sets2)} WHERE id=?", a2)
        # track completion timestamp for analytics (streak/heatmap/burndown)
        if "status" in b and before:
            if b["status"] == "done" and before["status"] != "done":
                con.execute("UPDATE tasks SET completed_at=? WHERE id=?", (NOW(), tid))
            elif b["status"] != "done" and before["status"] == "done":
                con.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (tid,))
        # recurring: completing a recurring parent task spawns the next occurrence
        if before and b.get("status") == "done" and before["status"] != "done" \
           and (before["recurrence"] or "none") != "none" and before["parent_id"] is None:
            nd = _next_due(before["due_date"], before["recurrence"])
            if nd:
                mx = con.execute("SELECT COALESCE(MAX(position),0)+1 m FROM tasks WHERE list_id=? AND parent_id IS NULL", (before["list_id"],)).fetchone()["m"]
                first_st = _first_status(con, before["list_id"])
                cur = con.execute("""INSERT INTO tasks(list_id,title,description,status,priority,start_date,due_date,tags,position,estimate,recurrence,created_at,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (before["list_id"], before["title"], before["description"],
                     first_st, before["priority"], None, nd,
                     before["tags"], mx, before["estimate"], before["recurrence"], NOW(), NOW()))
                new_tid = cur.lastrowid
                # carry over subtasks (reset to first status) so the recurring checklist repeats
                subs = con.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY position,id", (tid,)).fetchall()
                for s in subs:
                    con.execute("""INSERT INTO tasks(list_id,parent_id,title,description,status,priority,position,estimate,recurrence,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (before["list_id"], new_tid, s["title"], s["description"],
                         first_st, s["priority"], s["position"], s["estimate"], "none", NOW(), NOW()))
        return task_dict(con, con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

def _first_status(con, lid):
    l = con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()
    return list_statuses(l)[0]["id"] if l else "todo"

@app.post("/api/tasks/reorder")
def reorder_tasks(b: dict = Body(...)):
    """b = {items:[{id,position,status?,list_id?}]}"""
    # cache list statuses to validate moved tasks without re-querying per item
    status_cache = {}
    list_exists_cache = {}
    def valid_status_ids(con, lid):
        if lid not in status_cache:
            l = con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()
            sts = list_statuses(l) if l else DEFAULT_STATUSES
            status_cache[lid] = (sts[0]["id"], {s["id"] for s in sts})
        return status_cache[lid]
    def list_exists(con, lid):
        if lid not in list_exists_cache:
            list_exists_cache[lid] = bool(con.execute("SELECT 1 FROM lists WHERE id=?", (lid,)).fetchone())
        return list_exists_cache[lid]
    with db() as con:
        for it in b.get("items", []):
            sets = ["position=?"]; a = [it.get("position",0)]
            # validate any destination list_id up-front: if it doesn't exist, drop the field
            # so the task is never moved into a non-existent list (orphan).
            dest_lid = it.get("list_id") if "list_id" in it and it.get("list_id") is not None \
                       and list_exists(con, it.get("list_id")) else None
            if "status" in it:
                # target list = validated dest list_id, else the task's current list
                lid = dest_lid
                if lid is None:
                    cur = con.execute("SELECT list_id FROM tasks WHERE id=?", (it["id"],)).fetchone()
                    lid = cur["list_id"] if cur else None
                st = it["status"]
                if lid is not None:
                    first, valid = valid_status_ids(con, lid)
                    if st not in valid: st = first
                sets.append("status=?"); a.append(st)
            if dest_lid is not None: sets.append("list_id=?"); a.append(dest_lid)
            a.append(it["id"])
            con.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", a)
    return {"ok": True}

def _collect_descendants(con, tid):
    """Return [tid] + every nested descendant id (any depth), so no orphan rows/files remain."""
    all_ids = [tid]
    frontier = [tid]
    while frontier:
        ph = ",".join("?" * len(frontier))
        children = [r["id"] for r in con.execute(
            f"SELECT id FROM tasks WHERE parent_id IN ({ph})", frontier).fetchall()]
        if not children: break
        all_ids.extend(children)
        frontier = children
    return all_ids

@app.delete("/api/tasks/{tid}")
def del_task(tid: int):
    with db() as con:
        ids = _collect_descendants(con, tid)
        _del_attachment_files(con, ids)
        for s in ids:
            con.execute("DELETE FROM comments WHERE task_id=?", (s,))
            con.execute("DELETE FROM attachments WHERE task_id=?", (s,))
        ph = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM tasks WHERE id IN ({ph})", ids)
    return {"ok": True}

# ================= Comments =================
@app.post("/api/tasks/{tid}/comments")
def add_comment(tid: int, b: dict = Body(...)):
    body = (b.get("body","") or "").strip()
    if not body: raise HTTPException(400, "komentar kosong")
    with db() as con:
        if not con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "task not found")
        cur = con.execute("INSERT INTO comments(task_id,body,created_at) VALUES(?,?,?)", (tid, body, NOW()))
        return row(con.execute("SELECT * FROM comments WHERE id=?", (cur.lastrowid,)).fetchone())

@app.delete("/api/comments/{cid}")
def del_comment(cid: int):
    with db() as con: con.execute("DELETE FROM comments WHERE id=?", (cid,))
    return {"ok": True}

# ================= My Work / Dashboard =================
@app.get("/api/mywork")
def mywork(list_id: int = 0):
    today = datetime.date.today().isoformat()
    soon = (datetime.date.today()+datetime.timedelta(days=7)).isoformat()
    lf = " AND list_id=?" if list_id else ""
    la = (list_id,) if list_id else ()
    with db() as con:
        def enrich(rs):
            out = []
            for t in rs:
                d = dict(t)
                l = con.execute("SELECT name,color FROM lists WHERE id=?", (t["list_id"],)).fetchone()
                d["list_name"] = l["name"] if l else ""; d["list_color"] = l["color"] if l else "#6366f1"
                out.append(d)
            return out
        base = "SELECT * FROM tasks WHERE status<>'done' AND parent_id IS NULL AND due_date IS NOT NULL AND due_date<>''" + lf
        # overdue & hari ini tetap tangkap semua (termasuk yg in-progress) karena mendesak
        overdue = enrich(con.execute(base+" AND due_date<? ORDER BY due_date", (*la, today)).fetchall())
        todayt = enrich(con.execute(base+" AND due_date=? ORDER BY priority DESC", (*la, today)).fetchall())
        # SEDANG DIKERJAKAN: status in-progress yg belum overdue/hari ini -> diangkat ke atas
        inprogress = enrich(con.execute(
            "SELECT * FROM tasks WHERE status='inprogress' AND parent_id IS NULL"
            " AND (due_date IS NULL OR due_date='' OR due_date>?)" + lf +
            " ORDER BY (due_date IS NULL OR due_date=''), due_date, priority DESC", (today, *la)).fetchall())
        # bucket tanggal lain: keluarkan yg in-progress (sudah di section sendiri)
        upcoming = enrich(con.execute(base+" AND status<>'inprogress' AND due_date>? AND due_date<=? ORDER BY due_date", (*la, today, soon)).fetchall())
        later = enrich(con.execute(base+" AND status<>'inprogress' AND due_date>? ORDER BY due_date", (*la, soon)).fetchall())
        nodate = enrich(con.execute("SELECT * FROM tasks WHERE status<>'done' AND status<>'inprogress' AND parent_id IS NULL AND (due_date IS NULL OR due_date='')" + lf + " ORDER BY priority DESC, updated_at DESC LIMIT 25", la).fetchall())
        stats = {
            "spaces": con.execute("SELECT COUNT(*) c FROM spaces").fetchone()["c"],
            "lists": con.execute("SELECT COUNT(*) c FROM lists").fetchone()["c"],
            "open": con.execute("SELECT COUNT(*) c FROM tasks WHERE status<>'done' AND parent_id IS NULL").fetchone()["c"],
            "done": con.execute("SELECT COUNT(*) c FROM tasks WHERE status='done' AND parent_id IS NULL").fetchone()["c"],
            "overdue": len(overdue), "today": len(todayt),
        }
        return {"overdue": overdue, "today": todayt, "inprogress": inprogress, "upcoming": upcoming, "later": later, "nodate": nodate, "stats": stats}

# ===== Project status (jembatan Explorer project <-> task/My Work) =====
def _projects_space(con):
    r = con.execute("SELECT id FROM spaces WHERE name='Projects'").fetchone()
    if r:
        return r["id"]
    cur = con.execute("INSERT INTO spaces(name,color,icon,position,created_at) VALUES('Projects','#10b981','📦',999,?)", (NOW(),))
    return cur.lastrowid

def _ensure_project_list(con, name):
    """Pastikan project punya List (1:1). Buat di space 'Projects' kalau belum ada."""
    m = con.execute("SELECT list_id FROM project_meta WHERE name=?", (name,)).fetchone()
    if m and m["list_id"] and con.execute("SELECT 1 FROM lists WHERE id=?", (m["list_id"],)).fetchone():
        return m["list_id"]
    sid = _projects_space(con)
    cur = con.execute("INSERT INTO lists(space_id,name,color,statuses,position,created_at) VALUES(?,?,?,?,?,?)",
                      (sid, name, '#10b981', '', 0, NOW()))
    lid = cur.lastrowid
    con.execute("UPDATE project_meta SET list_id=? WHERE name=?", (lid, name))
    return lid

@app.get("/api/projects/status")
def projects_status():
    """Map {project_name: {status, list_id}} untuk badge di Explorer."""
    with db() as con:
        return {r["name"]: {"status": r["status"], "list_id": r["list_id"]}
                for r in con.execute("SELECT name,status,list_id FROM project_meta").fetchall()}

@app.post("/api/projects/status")
def set_project_status(b: dict = Body(...)):
    name = (b.get("name") or "").strip()
    status = (b.get("status") or "").strip().lower()
    if not name:
        raise HTTPException(400, "name wajib")
    if status not in PROJECT_STATUSES:
        raise HTTPException(400, "status tidak valid")
    with db() as con:
        ex = con.execute("SELECT name FROM project_meta WHERE name=?", (name,)).fetchone()
        if ex:
            con.execute("UPDATE project_meta SET status=?,updated_at=? WHERE name=?", (status, NOW(), name))
        else:
            con.execute("INSERT INTO project_meta(name,status,started_at,updated_at) VALUES(?,?,?,?)",
                        (name, status, NOW(), NOW()))
        lid = None
        if status == "active":
            lid = _ensure_project_list(con, name)
        else:
            r = con.execute("SELECT list_id FROM project_meta WHERE name=?", (name,)).fetchone()
            lid = r["list_id"] if r else None
        return {"name": name, "status": status, "list_id": lid}

@app.get("/api/mywork/projects")
def mywork_projects():
    """Project Active untuk My Work level-1 (kartu + statistik task dari List-nya)."""
    today = datetime.date.today().isoformat()
    out = []
    with db() as con:
        for m in con.execute("SELECT * FROM project_meta WHERE status='active' ORDER BY name").fetchall():
            lid = m["list_id"]
            total = done = overdue = todc = 0
            if lid:
                for t in con.execute("SELECT status,due_date FROM tasks WHERE list_id=? AND parent_id IS NULL", (lid,)).fetchall():
                    total += 1
                    if t["status"] == "done":
                        done += 1
                    else:
                        dd = t["due_date"]
                        if dd and dd < today:
                            overdue += 1
                        elif dd == today:
                            todc += 1
            if total == 0:   # project active tanpa task -> jangan kotori My Work
                continue
            out.append({"name": m["name"], "status": m["status"], "list_id": lid,
                        "total": total, "done": done, "overdue": overdue, "today": todc})
        return out

@app.get("/api/projects/board")
def project_board(name: str = ""):
    """Board (kanban) untuk sebuah repo. Lazy: TIDAK membuat list (pakai /activate)."""
    name = (name or "").strip()
    with db() as con:
        m = con.execute("SELECT status,list_id FROM project_meta WHERE name=?", (name,)).fetchone()
        if not m or not m["list_id"]:
            return {"active": False, "name": name}
        lid = m["list_id"]
        l = con.execute("SELECT * FROM lists WHERE id=?", (lid,)).fetchone()
        if not l:
            return {"active": False, "name": name}
        ld = dict(l)
        ld["statuses"] = list_statuses(ld)
        ts = rows(con.execute("SELECT * FROM tasks WHERE list_id=? AND parent_id IS NULL ORDER BY position,id", (lid,)).fetchall())
        return {"active": (m["status"] == "active"), "name": name, "list_id": lid,
                "statuses": ld["statuses"], "tasks": ts}

@app.post("/api/projects/board/activate")
def project_board_activate(b: dict = Body(...)):
    """Aktifkan board untuk repo: set active + buat list 1:1 kalau belum ada."""
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name wajib")
    with db() as con:
        ex = con.execute("SELECT name FROM project_meta WHERE name=?", (name,)).fetchone()
        if ex:
            con.execute("UPDATE project_meta SET status='active',updated_at=? WHERE name=?", (NOW(), name))
        else:
            con.execute("INSERT INTO project_meta(name,status,started_at,updated_at) VALUES(?,?,?,?)",
                        (name, "active", NOW(), NOW()))
        lid = _ensure_project_list(con, name)
    return project_board(name)

@app.get("/api/search")
def search(q: str = ""):
    q = (q or "").strip()
    if len(q) < 2: return []
    with db() as con:
        rs = con.execute("""SELECT t.id,t.title,t.status,t.priority,t.due_date,t.tags,t.parent_id,
                                   l.id lid, l.name lname, l.color lcolor, s.name sname, s.icon sicon
                            FROM tasks t JOIN lists l ON l.id=t.list_id JOIN spaces s ON s.id=l.space_id
                            WHERE t.title LIKE ? OR t.tags LIKE ? OR t.description LIKE ?
                            ORDER BY t.status='done', t.updated_at DESC LIMIT 50""",
                         (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        return rows(rs)

# ---- Backup database ----
@app.get("/api/backup/list")
def backup_list():
    bdir = os.path.join(BASE, "data", "backups")
    out = []
    try:
        for f in sorted(os.listdir(bdir), reverse=True):
            if f.endswith(".db"):
                fp = os.path.join(bdir, f)
                try:
                    st = os.stat(fp)
                    out.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime)})
                except OSError:
                    pass
    except OSError:
        pass
    return {"backups": out}

@app.post("/api/backup/now")
def backup_now():
    try:
        bdir = os.path.join(BASE, "data", "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(bdir, f"manual-{stamp}.db")
        src = sqlite3.connect(DB); dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        return {"ok": True, "name": os.path.basename(dest), "size": os.path.getsize(dest)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

@app.get("/api/backup/export")
def backup_export():
    if not os.path.exists(DB):
        raise HTTPException(404, "DB tidak ditemukan")
    fn = "projecthub-" + datetime.date.today().isoformat() + ".db"
    return FileResponse(DB, media_type="application/octet-stream", filename=fn)

# ---- digest endpoints (internal, key-protected) for cron/OpenClaw push ----
@app.get("/api/digest")
def digest():
    """Plaintext deadline reminder for WhatsApp/Telegram."""
    today = datetime.date.today()
    ts = today.isoformat()
    soon = (today + datetime.timedelta(days=3)).isoformat()
    PR = {4:"🔴",3:"🟠",2:"🔵",1:"⚪",0:"▫️"}
    with db() as con:
        def q(where, args):
            return rows(con.execute(
                "SELECT t.title, t.due_date, t.priority, l.name lname FROM tasks t JOIN lists l ON l.id=t.list_id "
                "WHERE t.status<>'done' AND t.parent_id IS NULL AND t.due_date IS NOT NULL AND t.due_date<>'' "+where+" ORDER BY t.due_date, t.priority DESC", args).fetchall())
        overdue = q("AND t.due_date<?", (ts,))
        todayt = q("AND t.due_date=?", (ts,))
        upcoming = q("AND t.due_date>? AND t.due_date<=?", (ts, soon))
    hari = today.strftime("%A, %d %B %Y")
    L = [f"📋 *ProjectHub — Pengingat*", hari, ""]
    if overdue:
        L.append(f"⚠️ *Telat ({len(overdue)})*")
        L += [f"{PR.get(t['priority'],'')} {t['title']} — {t['lname']} (due {t['due_date']})" for t in overdue]
        L.append("")
    if todayt:
        L.append(f"📅 *Hari ini ({len(todayt)})*")
        L += [f"{PR.get(t['priority'],'')} {t['title']} — {t['lname']}" for t in todayt]
        L.append("")
    if upcoming:
        L.append(f"🗓 *3 hari ke depan ({len(upcoming)})*")
        L += [f"{PR.get(t['priority'],'')} {t['title']} — {t['lname']} ({t['due_date']})" for t in upcoming]
        L.append("")
    if not (overdue or todayt or upcoming):
        L.append("✅ Tidak ada deadline mendesak. Santai. 🌴")
    else:
        L.append("Semangat! 💪")
    return JSONResponse({"text": "\n".join(L).strip()})

@app.get("/api/digest/projects")
def digest_projects():
    """Structured text of active projects for OpenClaw to auto-review."""
    PR = {4:"Urgent",3:"High",2:"Normal",1:"Low",0:"-"}
    with db() as con:
        out = ["=== DATA PROJECT AKTIF ProjectHub ==="]
        lists = rows(con.execute("SELECT * FROM lists ORDER BY id").fetchall())
        for l in lists:
            ts = rows(con.execute("SELECT * FROM tasks WHERE list_id=? AND parent_id IS NULL ORDER BY position,id", (l["id"],)).fetchall())
            if not ts: continue
            done = sum(1 for t in ts if t["status"] == "done")
            prog = round(100*done/len(ts)) if ts else 0
            out.append(f"\n## {l['name']} — progress {prog}% ({done}/{len(ts)})")
            for t in ts:
                mark = "x" if t["status"] == "done" else " "
                out.append(f"- [{mark}] {t['title']} (status={t['status']}, prio={PR.get(t['priority'],'-')}, due={t['due_date'] or '-'})")
        return JSONResponse({"text": "\n".join(out)})

@app.get("/api/digest/weekly")
def digest_weekly():
    """Ringkasan mingguan (commit semua repo + task) untuk WhatsApp. Key-protected."""
    today = datetime.date.today()
    since = today - datetime.timedelta(days=7)
    since_iso = since.isoformat()

    def repo_week(d):
        name, full = d
        if not os.path.isdir(os.path.join(full, ".git")):
            return (name, 0)
        p = _gitp(full, ["log", "--since=" + since_iso, "--format=%h", "--no-merges"], timeout=8)
        if p.returncode != 0:
            return (name, 0)
        return (name, len([l for l in (p.stdout or "").splitlines() if l.strip()]))

    with ThreadPoolExecutor(max_workers=16) as ex:
        repo_res = list(ex.map(repo_week, _toplevel_repos()))
    total_commits = sum(c for _, c in repo_res)
    top = sorted([(n, c) for n, c in repo_res if c > 0], key=lambda x: -x[1])[:6]

    with db() as con:
        done = con.execute("SELECT COUNT(*) c FROM tasks WHERE status='done' AND completed_at>=? AND parent_id IS NULL", (since_iso,)).fetchone()["c"]
        created = con.execute("SELECT COUNT(*) c FROM tasks WHERE created_at>=? AND parent_id IS NULL", (since_iso,)).fetchone()["c"]
        active = con.execute("SELECT COUNT(*) c FROM project_meta WHERE status='active'").fetchone()["c"]
        nxt = today + datetime.timedelta(days=7)
        upcoming = rows(con.execute(
            "SELECT t.title,t.due_date,l.name lname FROM tasks t JOIN lists l ON l.id=t.list_id "
            "WHERE t.status<>'done' AND t.parent_id IS NULL AND t.due_date>=? AND t.due_date<=? "
            "ORDER BY t.due_date LIMIT 6", (today.isoformat(), nxt.isoformat())).fetchall())

    L = ["📊 *ProjectHub — Ringkasan Mingguan*",
         f"{since.strftime('%d %b')} – {today.strftime('%d %b %Y')}", ""]
    L.append(f"💻 *{total_commits} commit* di {len([1 for _,c in repo_res if c>0])} repo")
    if top:
        L += [f"  • {n}: {c}" for n, c in top]
    L.append("")
    L.append(f"✅ *{done} task selesai* · 🆕 {created} task baru · 📁 {active} project aktif")
    if upcoming:
        L.append("")
        L.append("🗓 *Deadline 7 hari ke depan:*")
        L += [f"  • {t['title']} — {t['lname']} ({t['due_date']})" for t in upcoming]
    L.append("")
    L.append("Mantap, lanjutkan! 🚀" if (total_commits or done) else "Minggu santai. Yuk gas minggu ini! 💪")
    return JSONResponse({"text": "\n".join(L)})

# ---- update task via WA (key-protected, dipakai OpenClaw) ----
@app.get("/api/digest/tasks")
def digest_tasks(q: str = ""):
    """Cari task by judul (buat OpenClaw temukan id sebelum update)."""
    q = (q or "").strip()
    with db() as con:
        sql = ("SELECT t.id,t.title,t.status,t.completed_at,l.name lname "
               "FROM tasks t JOIN lists l ON l.id=t.list_id")
        a = ()
        if q:
            sql += " WHERE t.title LIKE ?"
            a = (f"%{q}%",)
        sql += " ORDER BY t.updated_at DESC LIMIT 30"
        return {"tasks": rows(con.execute(sql, a).fetchall())}

def _done_status_id(statuses):
    for s in statuses:
        nm = (s.get("name") or "").lower()
        if s["id"] == "done" or "done" in nm or "selesai" in nm or "complete" in nm:
            return s["id"]
    return statuses[-1]["id"] if statuses else "done"

@app.post("/api/digest/task/{tid}")
def digest_task_update(tid: int, b: dict = Body(default={})):
    """Update task dari WA: {done:true} / {status:'inprogress'} / {note:'...'} (aman, lewat app)."""
    with db() as con:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "task tidak ditemukan")
        l = con.execute("SELECT * FROM lists WHERE id=?", (t["list_id"],)).fetchone()
        statuses = list_statuses(dict(l))
        done_id = _done_status_id(statuses)
        sets, args = [], []
        new_status = None
        if b.get("done"):
            new_status = done_id
        elif b.get("status"):
            s = str(b["status"]).strip().lower()
            if s in ("selesai", "done", "kelar", "beres", "complete"):
                new_status = done_id
            else:
                new_status = next((x["id"] for x in statuses if x["id"].lower() == s or (x.get("name") or "").lower() == s), None)
                alias = {"jalan": "inprogress", "proses": "inprogress", "progress": "inprogress",
                         "in progress": "inprogress", "kerjakan": "inprogress", "review": "review", "todo": "todo"}
                if not new_status and s in alias:
                    new_status = next((x["id"] for x in statuses if x["id"] == alias[s]), None)
                if not new_status:
                    raise HTTPException(400, "status tidak dikenal. Pilihan: " + ", ".join(x["id"] for x in statuses))
        if new_status:
            sets.append("status=?"); args.append(new_status)
            if new_status == done_id:
                sets.append("completed_at=?"); args.append(NOW())
            else:
                sets.append("completed_at=NULL")
        note = (b.get("note") or "").strip()
        if note:
            desc = (t["description"] or "")
            stamp = datetime.date.today().isoformat()
            desc = (desc + f"\n\n[WA {stamp}] {note}").strip()
            sets.append("description=?"); args.append(desc[:8000])
        if not sets:
            return {"ok": False, "error": "tidak ada perubahan (kirim done/status/note)"}
        sets.append("updated_at=?"); args.append(NOW())
        args.append(tid)
        con.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", args)
        nt = con.execute("SELECT id,title,status,completed_at FROM tasks WHERE id=?", (tid,)).fetchone()
        return {"ok": True, "task": dict(nt)}

@app.get("/api/calendar")
def calendar(list_id: int = None):
    with db() as con:
        sql = "SELECT t.*, l.name lname, l.color lcolor FROM tasks t JOIN lists l ON l.id=t.list_id WHERE t.due_date IS NOT NULL AND t.due_date<>''"
        a = []
        if list_id: sql += " AND t.list_id=?"; a.append(list_id)
        return rows(con.execute(sql, a).fetchall())

# ================= OpenClaw review =================
OPENCLAW_AGENT = os.environ.get("OPENCLAW_AGENT", "main")

def build_prompt(l):
    PRIO = {0:"-",1:"Low",2:"Normal",3:"High",4:"Urgent"}
    lines = [
        "Kamu project advisor. Review List/project pribadi berikut, beri rekomendasi.",
        "Jawab Bahasa Indonesia, Markdown, ringkas & actionable. Heading WAJIB:",
        "## Penilaian Singkat","## Yang Masih Kurang (gap)","## Risiko",
        "## Rekomendasi Langkah Berikutnya","## Saran Task Baru (tiap baris diawali '- ')","",
        "=== DATA ===", f"List: {l['name']} | Progress: {l['progress']}% ({l['done']}/{l['total']} task selesai)"]
    for t in l["tasks"]:
        lines.append(f"- [{'x' if t['status']=='done' else ' '}] {t['title']} (status={t['status']}, prio={PRIO.get(t['priority'],'-')}, due={t['due_date'] or '-'})")
        for s in t.get("subtasks", []):
            lines.append(f"    - [{'x' if s['status']=='done' else ' '}] {s['title']}")
    if not l["tasks"]: lines.append("(belum ada task)")
    return "\n".join(lines)

def run_openclaw(prompt, session_key="projecthub", timeout=320):
    try:
        proc = subprocess.run(
            [OPENCLAW_BIN, "agent", "--agent", OPENCLAW_AGENT, "--session-key", session_key,
             "--json", "--thinking", "low", "--timeout", str(timeout-10), "--message", prompt],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HOME": os.path.expanduser("~"), "NO_COLOR": "1"})
        out = (proc.stdout or "").strip(); err = (proc.stderr or "").strip()
        text = extract_reply(out)
        if text: return text, True
        if proc.returncode != 0:
            return f"OpenClaw gagal (exit {proc.returncode}).\n\n```\n{(err or out)[:1500]}\n```", False
        return (out or err or "OpenClaw tidak mengembalikan jawaban."), bool(out)
    except subprocess.TimeoutExpired:
        return "OpenClaw timeout. Coba lagi.", False
    except FileNotFoundError:
        return f"Binary openclaw tidak ditemukan di {OPENCLAW_BIN}.", False
    except Exception as e:
        return f"Error OpenClaw: {e}", False

def extract_reply(out):
    if not out: return None
    cands = []
    try: cands.append(json.loads(out))
    except Exception:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try: cands.append(json.loads(line))
                except Exception: pass
    for obj in reversed(cands):
        t = _dig_text(obj)
        if t: return t
    return None

def _dig_text(obj, depth=0):
    if depth > 6 or obj is None: return None
    if isinstance(obj, str): return obj.strip() or None
    if isinstance(obj, dict):
        if isinstance(obj.get("payloads"), list):
            parts = [p.get("text") for p in obj["payloads"] if isinstance(p, dict) and isinstance(p.get("text"), str)]
            parts = [p.strip() for p in parts if p and p.strip()]
            if parts: return "\n\n".join(parts)
        for k in ("result","data","payload","output"):
            if k in obj:
                t = _dig_text(obj[k], depth+1)
                if t: return t
        for k in ("reply","text","message","content","response"):
            if isinstance(obj.get(k), str) and obj[k].strip(): return obj[k].strip()
    if isinstance(obj, list):
        parts = [_dig_text(x, depth+1) for x in obj]; parts=[p for p in parts if p]
        if parts: return "\n\n".join(parts)
    return None

@app.post("/api/lists/{lid}/review")
def review_list(lid: int):
    l = get_list(lid)
    text, ok = run_openclaw(build_prompt(l), session_key=f"projecthub-list-{lid}")
    with db() as con:
        cur = con.execute("INSERT INTO reviews(list_id,content,ok,created_at) VALUES(?,?,?,?)", (lid, text, 1 if ok else 0, NOW()))
        return row(con.execute("SELECT * FROM reviews WHERE id=?", (cur.lastrowid,)).fetchone())

@app.delete("/api/reviews/{rid}")
def del_review(rid: int):
    with db() as con: con.execute("DELETE FROM reviews WHERE id=?", (rid,))
    return {"ok": True}

def _clean_bullet_title(t):
    """Strip a leading checkbox like '[ ]', '[x]' or '[]' from a bullet line,
    without eating a real title that happens to start with 'x'."""
    t = t.strip()
    t = re.sub(r'^\[[ xX]?\]\s*', '', t)
    return t.strip(" -•*")

@app.post("/api/reviews/{rid}/apply")
def apply_review(rid: int):
    with db() as con:
        r = con.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        if not r: raise HTTPException(404)
        lid = r["list_id"]; content = r["content"]
        # only take bullets under the "Saran Task Baru" heading; no heading => create nothing
        m = re.search(r"saran task[^\n]*\n(.*)$", content, re.I | re.S)
        if not m: return {"created": 0}
        section = m.group(1)
        l = con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()
        st = list_statuses(l)[0]["id"]
        mx = con.execute("SELECT COALESCE(MAX(position),0) m FROM tasks WHERE list_id=? AND parent_id IS NULL", (lid,)).fetchone()["m"]
        created = 0
        for line in section.splitlines():
            t = line.strip()
            if t.startswith(("- ","* ","• ")):
                title = _clean_bullet_title(t[2:])
                if title:
                    mx += 1
                    con.execute("INSERT INTO tasks(list_id,title,status,position,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (lid, title[:300], st, mx, NOW(), NOW()))
                    created += 1
        return {"created": created}

@app.post("/api/tasks/{tid}/breakdown")
def task_breakdown(tid: int):
    """OpenClaw memecah task jadi subtask konkret, langsung dibuat."""
    with db() as con:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t: raise HTTPException(404)
        t = dict(t)
    prompt = (
        "Pecah task berikut menjadi 3-7 subtask konkret & actionable (Bahasa Indonesia).\n"
        "Balas HANYA daftar, tiap baris diawali '- '. Tanpa penjelasan lain.\n\n"
        f"Task: {t['title']}\nDeskripsi: {t.get('description') or '-'}")
    text, ok = run_openclaw(prompt, session_key=f"projecthub-task-{tid}", timeout=180)
    created = 0
    if ok:
        with db() as con:
            mx = con.execute("SELECT COALESCE(MAX(position),0) m FROM tasks WHERE parent_id=?", (tid,)).fetchone()["m"]
            for line in text.splitlines():
                s = line.strip()
                if s.startswith(("- ","* ","• ")):
                    title = _clean_bullet_title(s[2:])
                    if title:
                        mx += 1
                        con.execute("INSERT INTO tasks(list_id,parent_id,title,status,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                    (t["list_id"], tid, title[:300], "todo", mx, NOW(), NOW()))
                        created += 1
    return {"ok": ok, "created": created, "text": text}

@app.post("/api/lists/{lid}/ask")
def ask_list(lid: int, b: dict = Body(...)):
    """Tanya bebas ke OpenClaw soal list ini (tidak disimpan)."""
    q = (b.get("question") or "").strip()
    if not q: raise HTTPException(400, "pertanyaan kosong")
    l = get_list(lid)
    ctx = build_prompt(l)
    prompt = f"{ctx}\n\n=== PERTANYAAN USER ===\n{q}\n\nJawab Bahasa Indonesia, Markdown, ringkas."
    text, ok = run_openclaw(prompt, session_key=f"projecthub-list-{lid}")
    return {"ok": ok, "answer": text}

# ================= Attachments =================
def _safe_name(name):
    """Keep a recognizable but filesystem-safe original filename."""
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return name[:120]

@app.post("/api/tasks/{tid}/attachments")
async def add_attachment(tid: int, request: Request, file: UploadFile = File(...)):
    # Reject oversized uploads up-front via Content-Length, before reading/writing any bytes.
    # (Per-chunk enforcement below still guards requests that omit Content-Length.)
    try:
        clen = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        clen = 0
    if clen > MAX_UPLOAD:
        raise HTTPException(413, f"File terlalu besar (maks {MAX_UPLOAD // (1024*1024)} MB)")
    with db() as con:
        if not con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "task not found")
    original = _safe_name(file.filename)
    ext = os.path.splitext(original)[1].lower()
    stored = f"{uuid.uuid4().hex}_{original}"
    dest = os.path.join(UPLOADS, stored)
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    out.close(); os.remove(dest)
                    raise HTTPException(413, f"File terlalu besar (maks {MAX_UPLOAD // (1024*1024)} MB)")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception:
        try: os.remove(dest)
        except OSError: pass
        raise HTTPException(400, "Gagal menyimpan file")
    is_image = 1 if ext in IMG_EXT else 0
    mime = file.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream"
    with db() as con:
        cur = con.execute(
            "INSERT INTO attachments(task_id,stored,original,mime,size,is_image,kind,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (tid, stored, original, mime, size, is_image, "image" if is_image else "file", NOW()))
        r = con.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone()
        return attachment_dict(r)

def _get_attachment(aid):
    with db() as con:
        r = con.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if not r: raise HTTPException(404, "lampiran tidak ditemukan")
    path = os.path.join(UPLOADS, r["stored"])
    if not os.path.exists(path): raise HTTPException(404, "file hilang")
    return dict(r), path

@app.get("/api/attachments/{aid}")
def view_attachment(aid: int):
    r, path = _get_attachment(aid)
    mime = (r["mime"] or "application/octet-stream").lower()
    # Only safe raster images may render inline; everything else is forced to download.
    # nosniff prevents MIME-confusion; CSP sandbox neutralizes any active content.
    disposition = "inline" if mime in SAFE_INLINE_MIME else "attachment"
    return FileResponse(path, media_type=r["mime"] or "application/octet-stream",
                        headers={
                            "Content-Disposition": f'{disposition}; filename="{r["original"]}"',
                            "X-Content-Type-Options": "nosniff",
                            "Content-Security-Policy": "default-src 'none'; sandbox",
                        })

@app.get("/api/attachments/{aid}/download")
def download_attachment(aid: int):
    r, path = _get_attachment(aid)
    return FileResponse(path, media_type=r["mime"] or "application/octet-stream",
                        filename=r["original"])

@app.delete("/api/attachments/{aid}")
def del_attachment(aid: int):
    with db() as con:
        r = con.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        if not r: raise HTTPException(404)
        try: os.remove(os.path.join(UPLOADS, r["stored"]))
        except OSError: pass
        con.execute("DELETE FROM attachments WHERE id=?", (aid,))
    return {"ok": True}

@app.get("/api/lists/{lid}/gallery")
def list_gallery(lid: int):
    """All image attachments across every task in a list (for the 'Hasil' gallery)."""
    with db() as con:
        rs = con.execute(
            "SELECT a.*, t.title task_title FROM attachments a "
            "JOIN tasks t ON t.id=a.task_id "
            "WHERE t.list_id=? AND a.is_image=1 ORDER BY a.id DESC", (lid,)).fetchall()
        return [attachment_dict(r) for r in rs]

# ================= Quick-add (local natural-language parse) =================
_ID_DAYS = {"senin": 0, "selasa": 1, "rabu": 2, "kamis": 3, "jumat": 4, "jum'at": 4, "sabtu": 5, "minggu": 6, "ahad": 6}
_PRIO_WORDS = {"urgent": 4, "high": 3, "normal": 2, "low": 1}

def _parse_quickadd(text):
    """Parse '#tag !prio besok 12/05' into structured task fields. Pure-local, no AI."""
    tags, priority, due = [], 0, None
    tokens = (text or "").split()
    kept = []
    today = datetime.date.today()
    for tok in tokens:
        low = tok.lower().strip(".,;:")
        if tok.startswith("#") and len(tok) > 1:
            # drop commas so tags don't break the comma-split used by the UI
            tag = tok[1:].strip("#").replace(",", "")
            if tag: tags.append(tag)
            continue
        if tok.startswith("!") and low[1:] in _PRIO_WORDS:
            priority = _PRIO_WORDS[low[1:]]; continue
        if due is None:
            d = None
            if low == "hari" or low == "ini":  # handle "hari ini" loosely (either token)
                pass
            if low in ("besok", "esok"): d = today + datetime.timedelta(days=1)
            elif low in ("lusa",): d = today + datetime.timedelta(days=2)
            elif low in _ID_DAYS:
                delta = (_ID_DAYS[low] - today.weekday()) % 7
                d = today + datetime.timedelta(days=delta or 7)
            else:
                m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", low)
                if m:
                    dd, mm = int(m.group(1)), int(m.group(2))
                    yy = int(m.group(3)) if m.group(3) else today.year
                    if yy < 100: yy += 2000
                    try:
                        d = datetime.date(yy, mm, dd)
                        if not m.group(3) and d < today: d = d.replace(year=yy + 1)
                    except ValueError: d = None
            if d is not None:
                due = d.isoformat(); continue
        kept.append(tok)
    # "hari ini" as a phrase
    title = " ".join(kept)
    m2 = re.search(r"\bhari ini\b", title, re.I)
    if m2 and due is None:
        due = today.isoformat()
        title = (title[:m2.start()] + title[m2.end():])
    title = re.sub(r"\s+", " ", title).strip() or "Untitled"
    return {"title": title, "tags": ",".join(tags), "priority": priority, "due_date": due}

@app.post("/api/lists/{lid}/quickadd")
def quickadd(lid: int, b: dict = Body(...)):
    parsed = _parse_quickadd(b.get("text", ""))
    with db() as con:
        l = con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()
        if not l: raise HTTPException(404, "list not found")
        st = list_statuses(l)[0]["id"]
        mx = con.execute("SELECT COALESCE(MAX(position),0)+1 m FROM tasks WHERE list_id=? AND parent_id IS NULL", (lid,)).fetchone()["m"]
        cur = con.execute(
            "INSERT INTO tasks(list_id,title,status,priority,due_date,tags,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (lid, parsed["title"][:500], st, parsed["priority"], parsed["due_date"], parsed["tags"], mx, NOW(), NOW()))
        return task_dict(con, con.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())

# ================= Analytics / stats =================
@app.get("/api/stats")
def stats(list_id: int = None):
    today = datetime.date.today()
    with db() as con:
        scope = ""
        args = []
        if list_id:
            scope = " AND list_id=?"; args = [list_id]
        # completions per day (last 120 days) using completed_at
        comp = {}
        for r in con.execute(
            "SELECT substr(completed_at,1,10) d, COUNT(*) c FROM tasks "
            "WHERE completed_at IS NOT NULL AND parent_id IS NULL" + scope + " GROUP BY d", args).fetchall():
            if r["d"]: comp[r["d"]] = r["c"]
        days = []
        for i in range(119, -1, -1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            days.append({"date": d, "count": comp.get(d, 0)})
        # streak: consecutive days up to today with >=1 completion
        streak = 0
        d = today
        while comp.get(d.isoformat(), 0) > 0:
            streak += 1; d -= datetime.timedelta(days=1)
        # totals
        tot = con.execute(
            "SELECT SUM(CASE WHEN status<>'done' THEN 1 ELSE 0 END) o, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) dn, COUNT(*) all_c FROM tasks "
            "WHERE parent_id IS NULL" + scope, args).fetchone()
        open_n = tot["o"] or 0; done_n = tot["dn"] or 0
        # priority distribution (open tasks)
        prio = {str(p): 0 for p in range(5)}
        for r in con.execute(
            "SELECT priority p, COUNT(*) c FROM tasks WHERE parent_id IS NULL AND status<>'done'" + scope +
            " GROUP BY priority", args).fetchall():
            prio[str(r["p"] or 0)] = r["c"]
        # status distribution
        statusd = rows(con.execute(
            "SELECT status, COUNT(*) c FROM tasks WHERE parent_id IS NULL" + scope + " GROUP BY status", args).fetchall())
        # burndown: cumulative created vs completed per day over the 120-day window
        created_by = {}
        for r in con.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) c FROM tasks WHERE parent_id IS NULL" + scope + " GROUP BY d", args).fetchall():
            if r["d"]: created_by[r["d"]] = r["c"]
        start = (today - datetime.timedelta(days=119)).isoformat()
        base_created = con.execute(
            "SELECT COUNT(*) c FROM tasks WHERE parent_id IS NULL AND substr(created_at,1,10)<?" + scope,
            [start] + args).fetchone()["c"]
        base_done = con.execute(
            "SELECT COUNT(*) c FROM tasks WHERE parent_id IS NULL AND completed_at IS NOT NULL "
            "AND substr(completed_at,1,10)<?" + scope, [start] + args).fetchone()["c"]
        burndown = []
        cc, dc = base_created, base_done
        for day in days:
            cc += created_by.get(day["date"], 0)
            dc += comp.get(day["date"], 0)
            burndown.append({"date": day["date"], "created": cc, "done": dc, "open": cc - dc})
        return {
            "completed_per_day": days, "streak": streak,
            "open": open_n, "done": done_n, "total": open_n + done_n,
            "progress": round(100 * done_n / (open_n + done_n)) if (open_n + done_n) else 0,
            "priority_dist": prio, "status_dist": statusd, "burndown": burndown,
        }

# ================= AI daily standup =================
@app.post("/api/standup")
def standup():
    today = datetime.date.today()
    yest = (today - datetime.timedelta(days=1)).isoformat()
    ts = today.isoformat()
    PRIO = {0: "-", 1: "Low", 2: "Normal", 3: "High", 4: "Urgent"}
    with db() as con:
        def enrich(rs):
            out = []
            for t in rs:
                l = con.execute("SELECT name FROM lists WHERE id=?", (t["list_id"],)).fetchone()
                out.append(f"- {t['title']} ({l['name'] if l else '-'}, prio={PRIO.get(t['priority'],'-')}, due={t['due_date'] or '-'})")
            return out
        done_y = enrich(con.execute(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND completed_at IS NOT NULL "
            "AND substr(completed_at,1,10)=? ORDER BY priority DESC", (yest,)).fetchall())
        focus = enrich(con.execute(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND status<>'done' AND due_date IS NOT NULL AND due_date<>'' "
            "AND due_date<=? ORDER BY due_date, priority DESC LIMIT 15", (ts,)).fetchall())
        blockers = enrich(con.execute(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND status<>'done' AND due_date IS NOT NULL AND due_date<>'' "
            "AND due_date<? ORDER BY due_date LIMIT 10", (ts,)).fetchall())
    ctx = [
        "Buat ringkasan STANDUP HARIAN pribadi dalam Bahasa Indonesia, format Markdown.",
        "Gunakan TEPAT 3 heading ini: '## Kemarin', '## Hari Ini', '## Blocker'.",
        "Ringkas, actionable, maksimal beberapa poin per bagian. Beri 1 kalimat motivasi singkat di akhir.",
        "", f"Tanggal: {today.strftime('%A, %d %B %Y')}",
        "", "=== Selesai kemarin ===", *(done_y or ["(tidak ada)"]),
        "", "=== Fokus hari ini (due hari ini / overdue / prioritas) ===", *(focus or ["(tidak ada)"]),
        "", "=== Kemungkinan blocker (overdue) ===", *(blockers or ["(tidak ada)"]),
    ]
    text, ok = run_openclaw("\n".join(ctx), session_key="projecthub-standup", timeout=180)
    return {"ok": ok, "text": text}

# ================= Project Explorer (read-only file browser) =================
# KEAMANAN: total read-only. Tidak ada endpoint yang menulis/menghapus di CODE_ROOT.
# Semua akses file wajib lewat _safe_path() untuk path confinement ketat.

def _safe_path(rel):
    """Resolve rel di dalam CODE_ROOT dengan confinement ketat (anti path-traversal & symlink keluar).
    realpath() mengikuti symlink, jadi target yang menunjuk keluar root tetap ditolak."""
    rel = (rel or "").strip().lstrip("/\\")
    full = os.path.realpath(os.path.join(CODE_ROOT, rel))
    if full != CODE_ROOT and not full.startswith(CODE_ROOT + os.sep):
        raise HTTPException(400, "Path tidak valid")
    return full

def _git(args, cwd):
    """Jalankan git singkat; kalau gagal/timeout kembalikan None (tidak pernah raise)."""
    try:
        p = subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
        if p.returncode == 0:
            return (p.stdout or "").strip() or None
    except Exception:
        pass
    return None

def _ext_of(name):
    """ext lower tanpa titik; tangani nama tanpa ext (Makefile, Dockerfile, .gitignore)."""
    base = os.path.basename(name)
    if base.startswith(".") and base.count(".") == 1:
        return base[1:].lower()  # .gitignore -> gitignore
    e = os.path.splitext(base)[1].lower().lstrip(".")
    return e or base.lower()

@app.get("/api/files/projects")
def files_projects():
    """Daftar folder top-level (project) di CODE_ROOT + info git ringan."""
    out = []
    vismap = _repo_visibility_map()
    with db() as con:
        smap = {r["name"]: r["status"] for r in con.execute("SELECT name,status FROM project_meta").fetchall()}
    try:
        entries = sorted(os.scandir(CODE_ROOT), key=lambda e: e.name.lower())
    except OSError:
        return []
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if e.name in EXCLUDE_DIRS or e.name.startswith("."):
            continue
        d = e.path
        # Satu git call/project: %D (ref names) -> branch, %s -> subject, %cr -> relative date.
        branch = None
        last = None
        info = _git(["log", "-1", "--format=%D%n%s%n%cr"], d)
        if info is not None:
            parts = info.split("\n", 2)
            refs = parts[0] if len(parts) > 0 else ""
            subject = parts[1] if len(parts) > 1 else ""
            reldate = parts[2] if len(parts) > 2 else ""
            # branch dari "HEAD -> <branch>" pada ref names (bila ada)
            for ref in (r.strip() for r in refs.split(",")):
                if ref.startswith("HEAD -> "):
                    branch = ref[len("HEAD -> "):].strip() or None
                    break
            if subject:
                last = f"{subject} ({reldate})" if reldate else subject
        has_readme = any(os.path.isfile(os.path.join(d, n)) for n in ("README.md", "readme.md", "Readme.md", "README.MD"))
        # thumbnail: cari <project>/DOC/thumbnail.<ext> (case-insensitive folder & ekstensi)
        thumb = None
        mtime = 0
        for sub in ("DOC", "doc", "Doc"):
            subdir = os.path.join(d, sub)
            if not os.path.isdir(subdir):
                continue
            # nama bisa "thumbnail" atau "tumbnail" (ejaan user), ekstensi jpeg/jpg/png/webp, segala case
            try:
                cand = sorted(os.scandir(subdir), key=lambda x: x.name.lower())
            except OSError:
                cand = []
            for f in cand:
                stem, _, x = f.name.lower().rpartition(".")
                if stem in ("thumbnail", "tumbnail") and x in ("jpeg", "jpg", "png", "webp"):
                    try:
                        if not f.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    thumb = f"{e.name}/{sub}/{f.name}"
                    try:
                        mtime = int(f.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        mtime = 0
                    break
            if thumb:
                break
        out.append({"name": e.name, "git_branch": branch, "git_last": last,
                    "has_readme": has_readme, "thumb": thumb, "thumb_v": mtime,
                    "visibility": vismap.get(e.name), "status": smap.get(e.name)})
    return out

@app.get("/api/files/tree")
def files_tree(path: str = ""):
    """Isi 1 folder (lazy, non-rekursif). Folder dulu lalu file."""
    full = _safe_path(path)
    if not os.path.isdir(full):
        raise HTTPException(404, "Folder tidak ditemukan")
    dirs, files = [], []
    try:
        entries = os.scandir(full)
    except OSError:
        raise HTTPException(404, "Tidak bisa membaca folder")
    for e in entries:
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if e.name in EXCLUDE_DIRS:
                continue
            dirs.append({"name": e.name, "type": "dir", "size": 0, "ext": ""})
        else:
            try:
                sz = e.stat(follow_symlinks=False).st_size
            except OSError:
                sz = 0
            files.append({"name": e.name, "type": "file", "size": sz, "ext": _ext_of(e.name)})
    rel = os.path.relpath(full, CODE_ROOT)
    rel = "" if rel == "." else rel.replace(os.sep, "/")
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {"path": rel, "entries": dirs + files}

# Kategori ekstensi untuk filter cepat (quick-filter chip di Explorer)
FIND_IMAGES = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
FIND_DOCS = {"pdf", "docx", "doc", "xlsx", "xls", "csv", "tsv", "md", "markdown", "txt", "ppt", "pptx", "rtf", "odt"}
FIND_CODE = {"py", "js", "ts", "jsx", "tsx", "c", "h", "cpp", "hpp", "cc", "ino", "java", "go", "rs",
             "rb", "php", "sh", "bash", "ps1", "html", "htm", "css", "scss", "sass", "json", "yaml",
             "yml", "toml", "xml", "sql", "kt", "swift", "m", "cs", "vue", "svelte", "lua", "r",
             "dart", "gradle", "cmake", "v", "sv", "vhd", "vhdl", "asm", "s", "pde", "cfg", "ini", "env"}

@app.get("/api/files/find")
def files_find(path: str = "", kind: str = ""):
    """Cari file rekursif dalam 1 project menurut kategori (images/docs/code). Untuk quick-filter."""
    base = _safe_path(path)
    if not os.path.isdir(base):
        raise HTTPException(404, "Folder tidak ditemukan")
    cats = {"images": FIND_IMAGES, "docs": FIND_DOCS, "code": FIND_CODE}
    want = cats.get(kind)
    all_files = (kind == "all")
    if want is None and not all_files:
        raise HTTPException(400, "kind tidak valid")
    cap = 500
    out = []
    truncated = False
    for root, dnames, fnames in os.walk(base):
        dnames[:] = [d for d in dnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in fnames:
            if not all_files and _ext_of(fn) not in want:
                continue
            full = os.path.join(root, fn)
            try:
                if not os.path.isfile(full):
                    continue
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            rel = os.path.relpath(full, CODE_ROOT).replace(os.sep, "/")
            sub = os.path.relpath(root, base).replace(os.sep, "/")
            out.append({"name": fn, "path": rel, "ext": _ext_of(fn), "size": sz,
                        "dir": "" if sub == "." else sub})
            if len(out) >= cap:
                truncated = True
                break
        if truncated:
            break
    out.sort(key=lambda x: x["path"].lower())
    return {"truncated": truncated, "results": out}

# ===== Buat project baru (git init + GitHub) — satu-satunya endpoint yang MENULIS ke CODE_ROOT =====
GH_OWNER = os.environ.get("PH_GH_OWNER", "juarendra")
GH_BIN = shutil.which("gh") or "/usr/bin/gh"
# nama folder/repo aman: mulai alfanumerik, lalu [A-Za-z0-9._-], maks 80, tanpa spasi/slash
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_VIS_CACHE = {"ts": 0.0, "map": {}}
VIS_TTL = 300  # detik

def _repo_visibility_map():
    """Map {repo_name: 'public'|'private'} via gh, cache TTL. Best-effort (token bisa invalid -> {})."""
    now = time.time()
    if _VIS_CACHE["map"] and now - _VIS_CACHE["ts"] < VIS_TTL:
        return _VIS_CACHE["map"]
    m = {}
    try:
        p = subprocess.run([GH_BIN, "repo", "list", GH_OWNER, "--json", "name,visibility",
                            "--limit", "400"], capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            for r in json.loads(p.stdout or "[]"):
                nm = r.get("name", "")
                if nm:
                    m[nm] = (r.get("visibility") or "").lower()
    except Exception:
        pass
    if m:  # hanya cache bila sukses; bila gagal, coba lagi request berikutnya
        _VIS_CACHE["ts"] = now
        _VIS_CACHE["map"] = m
    return _VIS_CACHE["map"]

GITIGNORE_DEFAULT = """# dependencies
node_modules/
__pycache__/
*.pyc
.venv/
venv/
# build output
dist/
build/
*.o
*.elf
*.bin
# os / editor
.DS_Store
Thumbs.db
.vscode/
.idea/
# secrets
.env
"""

@app.post("/api/files/projects/create")
def files_project_create(b: dict = Body(...)):
    """Buat project baru: folder + scaffold + git init/commit + gh repo create + push."""
    name = (b.get("name") or "").strip()
    desc = (b.get("description") or "").strip()
    vis = (b.get("visibility") or "private").lower()
    if vis not in ("public", "private"):
        vis = "private"
    want_readme = bool(b.get("readme", True))
    want_gitignore = bool(b.get("gitignore", True))
    if not NAME_RE.match(name):
        raise HTTPException(400, "Nama tidak valid (huruf/angka . _ - , maks 80, tanpa spasi)")
    if name in EXCLUDE_DIRS or name.startswith("."):
        raise HTTPException(400, "Nama tidak diizinkan")
    full = _safe_path(name)  # confine ketat ke CODE_ROOT
    if os.path.relpath(full, CODE_ROOT) != name:  # wajib top-level, bukan a/b
        raise HTTPException(400, "Nama tidak valid")
    if os.path.exists(full):
        raise HTTPException(409, "Folder dengan nama itu sudah ada")
    try:
        os.makedirs(full, exist_ok=False)
        if want_readme:
            with open(os.path.join(full, "README.md"), "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\n{desc}\n" if desc else f"# {name}\n")
        if want_gitignore:
            with open(os.path.join(full, ".gitignore"), "w", encoding="utf-8") as f:
                f.write(GITIGNORE_DEFAULT)
        subprocess.run(["git", "init", "-b", "main"], cwd=full, capture_output=True, text=True, timeout=20)
        subprocess.run(["git", "add", "-A"], cwd=full, capture_output=True, text=True, timeout=20)
        subprocess.run(["git", "commit", "-m", "init: scaffold project", "--allow-empty"],
                       cwd=full, capture_output=True, text=True, timeout=30)
        gh = subprocess.run([GH_BIN, "repo", "create", f"{GH_OWNER}/{name}", f"--{vis}",
                             "--source", full, "--remote", "origin", "--push"],
                            cwd=full, capture_output=True, text=True, timeout=120)
        if gh.returncode != 0:
            err = (gh.stderr or gh.stdout or "").strip()
            return {"ok": False, "created_local": True, "name": name, "visibility": vis,
                    "error": f"Folder & git lokal sudah dibuat, tapi push GitHub gagal: {err[:280]}",
                    "hint": "Jalankan 'gh auth login -h github.com' di Jetson (token mungkin expired), lalu coba push manual."}
        lines = [l.strip() for l in (gh.stdout or "").splitlines() if l.strip()]
        repo_url = next((l for l in lines if "github.com" in l), f"https://github.com/{GH_OWNER}/{name}")
        _VIS_CACHE["ts"] = 0.0  # invalidate supaya badge muncul
        return {"ok": True, "name": name, "visibility": vis, "url": repo_url}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "name": name, "error": str(e)[:280]}

@app.post("/api/files/projects/clone")
def files_project_clone(b: dict = Body(...)):
    """Clone repo GitHub yang sudah ada ke CODE_ROOT."""
    url = (b.get("url") or "").strip()
    m = re.match(r"^(https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(\.git)?/?$", url)
    if not m:
        raise HTTPException(400, "URL harus repo GitHub (https://github.com/user/repo atau git@github.com:user/repo)")
    name = m.group(3)
    if not NAME_RE.match(name) or name in EXCLUDE_DIRS or name.startswith("."):
        raise HTTPException(400, "Nama repo tidak valid")
    full = _safe_path(name)
    if os.path.relpath(full, CODE_ROOT) != name:
        raise HTTPException(400, "Nama tidak valid")
    if os.path.exists(full):
        raise HTTPException(409, "Folder dengan nama itu sudah ada")
    try:
        r = subprocess.run(["git", "clone", "--", url, full],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            # bersihkan folder gagal kalau sempat kebuat
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            return {"ok": False, "error": ((r.stderr or r.stdout) or "").strip()[:300]}
        _VIS_CACHE["ts"] = 0.0
        return {"ok": True, "name": name}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Clone timeout (repo terlalu besar?)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:280]}

# ===== Git per-project: status / push / pull / release (branch SELALU main) =====
def _proj_git_dir(path):
    """Resolve project dir (top-level) + pastikan git repo. Confine ke CODE_ROOT."""
    full = _safe_path(path)
    if os.path.relpath(full, CODE_ROOT) != path.strip("/"):
        raise HTTPException(400, "Path tidak valid")
    if not os.path.isdir(full):
        raise HTTPException(404, "Project tidak ditemukan")
    return full

def _gitp(full, args, timeout=15):
    return subprocess.run(["git", "-C", full] + args, capture_output=True, text=True, timeout=timeout)

def _git_status_dict(full, do_fetch=False):
    if not os.path.isdir(os.path.join(full, ".git")):
        return {"git": False}
    remote = _gitp(full, ["remote", "get-url", "origin"]).stdout.strip()
    has_remote = bool(remote)
    porcelain = [l for l in (_gitp(full, ["status", "--porcelain"]).stdout or "").splitlines() if l.strip()]
    conflict = any(l[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD") for l in porcelain)
    fetched = False
    if has_remote and do_fetch:
        fr = _gitp(full, ["fetch", "origin", "main"], timeout=40)
        fetched = fr.returncode == 0
    ahead = behind = 0
    rl = _gitp(full, ["rev-list", "--left-right", "--count", "origin/main...HEAD"])
    if rl.returncode == 0:
        parts = rl.stdout.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    if conflict:
        state = "conflict"
    elif porcelain:
        state = "dirty"
    elif ahead and behind:
        state = "diverged"
    elif ahead:
        state = "ahead"
    elif behind:
        state = "behind"
    else:
        state = "clean"
    return {"git": True, "has_remote": has_remote, "dirty": len(porcelain),
            "conflict": conflict, "ahead": ahead, "behind": behind, "state": state, "fetched": fetched}

@app.get("/api/files/git/status")
def git_status_ep(path: str = "", fetch: int = 0):
    return _git_status_dict(_proj_git_dir(path), do_fetch=bool(fetch))

@app.post("/api/files/git/push")
def git_push_ep(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    if not os.path.isdir(os.path.join(full, ".git")):
        raise HTTPException(400, "Bukan git repo")
    msg = (b.get("message") or "").strip() or ("update: " + NOW())
    _gitp(full, ["add", "-A"])
    committed = False
    if (_gitp(full, ["status", "--porcelain"]).stdout or "").strip():
        c = _gitp(full, ["commit", "-m", msg], timeout=30)
        committed = c.returncode == 0
    p = _gitp(full, ["push", "-u", "origin", "main"], timeout=120)
    out = ((p.stderr or "") + (p.stdout or "")).strip()
    return {"ok": p.returncode == 0, "committed": committed, "message": msg,
            "output": out[:400], "status": _git_status_dict(full)}

@app.post("/api/files/git/pull")
def git_pull_ep(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    if not os.path.isdir(os.path.join(full, ".git")):
        raise HTTPException(400, "Bukan git repo")
    p = _gitp(full, ["pull", "--no-rebase", "origin", "main"], timeout=120)
    out = ((p.stderr or "") + (p.stdout or "")).strip()
    conflict = ("CONFLICT" in out) or ("Automatic merge failed" in out)
    return {"ok": p.returncode == 0 and not conflict, "conflict": conflict,
            "output": out[:500], "status": _git_status_dict(full)}

def _suggest_version(latest):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", latest or "")
    if m:
        a, b2, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"v{a}.{b2}.{c+1}"
    return "v0.1.0"

@app.get("/api/files/git/releases")
def git_releases_ep(path: str = ""):
    full = _proj_git_dir(path)
    rels = []
    try:
        rl = subprocess.run([GH_BIN, "release", "list", "--limit", "12",
                             "--json", "tagName,name,publishedAt,isLatest"],
                            cwd=full, capture_output=True, text=True, timeout=20)
        if rl.returncode == 0:
            rels = json.loads(rl.stdout or "[]")
    except Exception:
        pass
    latest = next((r["tagName"] for r in rels if r.get("isLatest")),
                  rels[0]["tagName"] if rels else None)
    return {"latest": latest, "suggest": _suggest_version(latest), "releases": rels}

@app.post("/api/files/git/release")
def git_release_ep(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    name = os.path.basename(full)
    version = (b.get("version") or "").strip()
    if not re.match(r"^v?\d+\.\d+\.\d+([-.\w]*)?$", version):
        raise HTTPException(400, "Versi tidak valid (contoh: v1.0.0)")
    notes = (b.get("notes") or "").strip()
    mode = (b.get("mode") or "zip").lower()
    files = b.get("files") or []
    tmpzip = None
    try:
        assets = []
        if mode == "zip":
            tmpzip = os.path.join("/tmp", f"{name}-{version}.zip")
            ar = _gitp(full, ["archive", "--format=zip", "-o", tmpzip, "HEAD"], timeout=120)
            if ar.returncode != 0 or not os.path.isfile(tmpzip):
                return {"ok": False, "error": "Gagal buat zip: " + (ar.stderr or "")[:200]}
            assets = [tmpzip]
        else:
            for rel in files:
                fp = _safe_path(rel)  # confine ketat
                # wajib di dalam project ini
                if os.path.relpath(fp, full).startswith("..") or os.path.relpath(fp, full) == os.pardir:
                    continue
                if os.path.commonpath([fp, full]) == full and os.path.isfile(fp):
                    assets.append(fp)
            if not assets:
                raise HTTPException(400, "Tidak ada file valid dipilih")
        cmd = [GH_BIN, "release", "create", version, "--target", "main",
               "--title", version, "--notes", (notes or version)] + assets
        r = subprocess.run(cmd, cwd=full, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return {"ok": False, "error": ((r.stderr or r.stdout) or "").strip()[:400]}
        url = next((l.strip() for l in (r.stdout or "").splitlines() if "github.com" in l), "")
        return {"ok": True, "version": version, "url": url, "assets": len(assets), "mode": mode}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        if tmpzip and os.path.exists(tmpzip):
            try:
                os.remove(tmpzip)
            except OSError:
                pass

# ===== Cari isi file (grep) =====
@app.get("/api/files/grep")
def files_grep(path: str = "", q: str = "", max: int = 200):
    full = _proj_git_dir(path)
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": [], "truncated": False}
    cap = min(max, 400)
    out = []
    truncated = False
    is_git = os.path.isdir(os.path.join(full, ".git"))
    try:
        if is_git:
            # git grep: cepat, ikut file untracked, literal (-F), case-insensitive (-i)
            p = subprocess.run(["git", "-C", full, "grep", "-n", "-I", "-i", "-F",
                                "--untracked", "--no-color", "-e", q],
                               capture_output=True, text=True, timeout=20)
            lines = (p.stdout or "").splitlines()
            for ln in lines:
                # format: path:line:teks
                a = ln.split(":", 2)
                if len(a) < 3:
                    continue
                rel = a[0]
                full_rel = os.path.relpath(os.path.join(full, rel), CODE_ROOT).replace(os.sep, "/")
                out.append({"path": full_rel, "line": int(a[1]) if a[1].isdigit() else 0,
                            "text": a[2][:200], "name": os.path.basename(rel)})
                if len(out) >= cap:
                    truncated = True
                    break
        else:
            ql = q.lower()
            scanned = 0
            for root, dnames, fnames in os.walk(full):
                dnames[:] = [d for d in dnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
                for fn in fnames:
                    if scanned >= 4000:  # cap file dipindai (cegah self-DoS folder besar)
                        truncated = True
                        break
                    scanned += 1
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fp) > MAX_TEXT_VIEW:
                            continue
                        with open(fp, "rb") as f:
                            txt = f.read().decode("utf-8", "replace")
                    except OSError:
                        continue
                    for i, line in enumerate(txt.splitlines(), 1):
                        if ql in line.lower():
                            rel = os.path.relpath(fp, CODE_ROOT).replace(os.sep, "/")
                            out.append({"path": rel, "line": i, "text": line.strip()[:200], "name": fn})
                            if len(out) >= cap:
                                truncated = True
                                break
                    if truncated:
                        break
                if truncated:
                    break
    except Exception:
        pass
    return {"results": out, "truncated": truncated}

def _toplevel_repos():
    out = []
    try:
        for e in os.scandir(CODE_ROOT):
            try:
                if e.is_dir(follow_symlinks=False) and e.name not in EXCLUDE_DIRS and not e.name.startswith("."):
                    out.append((e.name, e.path))
            except OSError:
                continue
    except OSError:
        pass
    return out

@app.get("/api/files/grep-all")
def files_grep_all(q: str = "", max: int = 250):
    """Cari teks di SEMUA project sekaligus (git grep paralel)."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": [], "truncated": False}
    cap = min(max, 400)

    def repo_grep(d):
        name, full = d
        if not os.path.isdir(os.path.join(full, ".git")):
            return []
        try:
            p = subprocess.run(["git", "-C", full, "grep", "-n", "-I", "-i", "-F",
                                "--untracked", "--no-color", "-e", q],
                               capture_output=True, text=True, timeout=15)
        except Exception:
            return []
        res = []
        for ln in (p.stdout or "").splitlines()[:40]:  # cap per repo (cegah 1 repo banjir)
            a = ln.split(":", 2)
            if len(a) < 3:
                continue
            res.append({"project": name, "path": name + "/" + a[0],
                        "line": int(a[1]) if a[1].isdigit() else 0,
                        "text": a[2][:160], "name": os.path.basename(a[0])})
        return res

    results = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for lst in ex.map(repo_grep, _toplevel_repos()):
            results.extend(lst)
    results.sort(key=lambda x: x["project"].lower())
    truncated = len(results) > cap
    return {"results": results[:cap], "truncated": truncated}

@app.get("/api/files/gitignore")
def gitignore_get(path: str = ""):
    full = _proj_git_dir(path)
    fp = os.path.join(full, ".gitignore")
    content = ""
    if os.path.isfile(fp):
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            pass
    # daftar path yang saat ini di-track git (untuk tahu mana yang sudah ke-upload)
    tracked = []
    try:
        p = _gitp(full, ["ls-files"], timeout=15)
        tracked = [l for l in (p.stdout or "").splitlines() if l.strip()][:5000]
    except Exception:
        pass
    return {"content": content, "tracked": tracked}

@app.post("/api/files/gitignore")
def gitignore_save(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    content = b.get("content", "")
    if len(content) > 200000:
        raise HTTPException(400, "Terlalu besar")
    try:
        with open(os.path.join(full, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}
    # untrack file/folder konkret yg sekarang ke-track supaya hilang dari repo saat push
    untracked = 0
    if os.path.isdir(os.path.join(full, ".git")):
        for line in content.splitlines():
            p = line.strip()
            if not p or p.startswith("#") or p.startswith("!"):
                continue
            if any(c in p for c in "*?[]"):  # pola glob -> .gitignore saja, jangan rm
                continue
            rel = p.rstrip("/").lstrip("/")
            if not rel or rel.startswith(".."):
                continue
            target = os.path.realpath(os.path.join(full, rel))
            if os.path.commonpath([target, full]) != full or target == full:
                continue
            r = _gitp(full, ["rm", "-r", "--cached", "--ignore-unmatch", "-q", "--", rel], timeout=20)
            if r.returncode == 0:
                untracked += 1
    return {"ok": True, "untracked": untracked}

@app.get("/api/files/kfile/{fname}")
def kfile(fname: str, path: str = ""):
    """Serve file KiCad dgn NAMA FILE di URL (biar KiCanvas resolve hierarki sheet). Confined."""
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    if os.path.basename(full) != fname:
        raise HTTPException(400, "Nama tidak cocok")
    if _ext_of(fname) not in (KICAD_EXT | {"kicad_pro"}):
        raise HTTPException(400, "Bukan file KiCad")
    return FileResponse(full, media_type="text/plain",
                        headers={"X-Content-Type-Options": "nosniff"})

@app.get("/api/files/kicad-set")
def kicad_set(path: str = ""):
    """Kumpulkan semua .kicad_sch (+ .kicad_pro) di folder yang sama untuk skematik multi-sheet/hierarki."""
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    folder = os.path.dirname(full)
    out = []
    total = 0
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for fn in names:
        e = _ext_of(fn)
        if e not in ("kicad_sch", "kicad_pro"):
            continue
        fp = os.path.join(folder, fn)
        try:
            if not os.path.isfile(fp):
                continue
            sz = os.path.getsize(fp)
            if sz > 6 * 1024 * 1024:
                continue
            with open(fp, "rb") as f:
                content = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        out.append({"filename": fn, "ext": e, "content": content})
        total += sz
        if len(out) >= 50 or total > 25 * 1024 * 1024:
            break
    return {"files": out, "opened": os.path.basename(full), "count": len(out)}

@app.get("/api/files/gerber-set")
def gerber_set(path: str = ""):
    """Kumpulkan semua file gerber/drill di folder yang sama (untuk render board via pcb-stackup)."""
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    folder = os.path.dirname(full)
    layers = []
    total = 0
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for fn in names:
        if _ext_of(fn) not in GERBER_EXT:
            continue
        fp = os.path.join(folder, fn)
        try:
            if not os.path.isfile(fp):
                continue
            sz = os.path.getsize(fp)
            if sz > 3 * 1024 * 1024:  # skip layer raksasa
                continue
            with open(fp, "rb") as f:
                content = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        layers.append({"filename": fn, "gerber": content})
        total += sz
        if len(layers) >= 40 or total > 14 * 1024 * 1024:
            break
    return {"layers": layers, "folder": os.path.relpath(folder, CODE_ROOT).replace(os.sep, "/"),
            "count": len(layers)}

# ===== Auto-task dari TODO / FIXME =====
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[ :\-]*(.*)", re.I)

@app.get("/api/files/todos")
def files_todos(path: str = ""):
    """Scan komentar TODO/FIXME/HACK/XXX di sebuah project."""
    full = _proj_git_dir(path)
    out = []
    if os.path.isdir(os.path.join(full, ".git")):
        try:
            p = subprocess.run(["git", "-C", full, "grep", "-n", "-I", "--untracked", "--no-color",
                                "-E", "(TODO|FIXME|HACK|XXX)"],
                               capture_output=True, text=True, timeout=20)
            for ln in (p.stdout or "").splitlines():
                a = ln.split(":", 2)
                if len(a) < 3:
                    continue
                m = TODO_RE.search(a[2])
                if not m:
                    continue
                out.append({"file": a[0], "line": int(a[1]) if a[1].isdigit() else 0,
                            "tag": m.group(1).upper(), "text": (m.group(2) or "").strip()[:200]})
                if len(out) >= 400:
                    break
        except Exception:
            pass
    return {"todos": out}

@app.post("/api/files/todos/import")
def files_todos_import(b: dict = Body(...)):
    """Buat task dari TODO terpilih ke board project. Dedup via marker file:line di description."""
    full = _proj_git_dir(b.get("path", ""))
    name = os.path.basename(full)
    items = b.get("items") or []
    created = 0
    with db() as con:
        ex = con.execute("SELECT name FROM project_meta WHERE name=?", (name,)).fetchone()
        if not ex:
            con.execute("INSERT INTO project_meta(name,status,started_at,updated_at) VALUES(?,?,?,?)",
                        (name, "active", NOW(), NOW()))
        lid = _ensure_project_list(con, name)
        seen = set()
        for t in con.execute("SELECT description FROM tasks WHERE list_id=?", (lid,)).fetchall():
            d = t["description"] or ""
            mm = re.findall(r"\[src:([^\]]+)\]", d)
            seen.update(mm)
        first = _first_status(con, lid)
        for it in items:
            src = f"{it.get('file')}:{it.get('line')}"
            if src in seen:
                continue
            tag = (it.get("tag") or "TODO").upper()
            title = ((it.get("text") or tag).strip() or tag)[:160]
            desc = f"{tag} di `{it.get('file')}:{it.get('line')}` [src:{src}]"
            con.execute("INSERT INTO tasks(list_id,title,description,status,tags,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (lid, title, desc, first, "todo", NOW(), NOW()))
            created += 1
            seen.add(src)
    return {"ok": True, "created": created, "list_id": lid}

# ===== Git diff / history =====
@app.get("/api/files/git/diff")
def git_diff_ep(path: str = "", file: str = ""):
    full = _proj_git_dir(path)
    if not os.path.isdir(os.path.join(full, ".git")):
        raise HTTPException(400, "Bukan git repo")
    args = ["diff", "HEAD", "--no-color"]
    if file:
        args += ["--", file]
    p = _gitp(full, args, timeout=20)
    txt = p.stdout or ""
    truncated = len(txt) > 120000
    return {"diff": txt[:120000], "truncated": truncated,
            "stat": (_gitp(full, ["diff", "HEAD", "--stat", "--no-color"], timeout=15).stdout or "")[:4000]}

@app.get("/api/files/git/log")
def git_log_ep(path: str = "", limit: int = 40):
    full = _proj_git_dir(path)
    if not os.path.isdir(os.path.join(full, ".git")):
        return {"commits": []}
    fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%cr%x1f%cI"
    p = _gitp(full, ["log", f"-{min(limit,200)}", f"--format={fmt}"], timeout=20)
    out = []
    for ln in (p.stdout or "").splitlines():
        a = ln.split("\x1f")
        if len(a) >= 6:
            out.append({"hash": a[0], "short": a[1], "subject": a[2], "author": a[3], "rel": a[4], "date": a[5]})
    return {"commits": out}

@app.get("/api/files/git/show")
def git_show_ep(path: str = "", hash: str = ""):
    full = _proj_git_dir(path)
    if not re.match(r"^[0-9a-fA-F]{4,40}$", hash or ""):
        raise HTTPException(400, "hash tidak valid")
    p = _gitp(full, ["show", hash, "--no-color", "--stat", "-p"], timeout=20)
    txt = p.stdout or ""
    return {"diff": txt[:120000], "truncated": len(txt) > 120000}

@app.delete("/api/files/git/release")
def git_release_delete(path: str = "", tag: str = ""):
    full = _proj_git_dir(path)
    if not re.match(r"^[\w.][\w.\-]{0,59}$", tag or ""):  # awalan bukan '-' (cegah option-injection)
        raise HTTPException(400, "tag tidak valid")
    r = subprocess.run([GH_BIN, "release", "delete", "--yes", "--cleanup-tag", "--", tag],
                       cwd=full, capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        return {"ok": False, "error": ((r.stderr or r.stdout) or "").strip()[:300]}
    return {"ok": True, "tag": tag}

# ===== GitHub Issues <-> task =====
@app.get("/api/files/issues")
def gh_issues(path: str = "", state: str = "open"):
    full = _proj_git_dir(path)
    st = state if state in ("open", "closed", "all") else "open"
    try:
        p = subprocess.run([GH_BIN, "issue", "list", "--state", st, "--limit", "50",
                            "--json", "number,title,state,url,body,labels"],
                           cwd=full, capture_output=True, text=True, timeout=25)
        if p.returncode != 0:
            return {"ok": False, "error": ((p.stderr or p.stdout) or "").strip()[:200], "issues": []}
        issues = json.loads(p.stdout or "[]")
        for i in issues:
            i["labels"] = [l.get("name") for l in (i.get("labels") or [])]
        return {"ok": True, "issues": issues}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "issues": []}

@app.post("/api/files/issues/import")
def gh_issues_import(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    name = os.path.basename(full)
    try:
        p = subprocess.run([GH_BIN, "issue", "list", "--state", "open", "--limit", "100",
                            "--json", "number,title,body"], cwd=full,
                           capture_output=True, text=True, timeout=25)
        if p.returncode != 0:
            return {"ok": False, "error": ((p.stderr or p.stdout) or "").strip()[:200]}
        issues = json.loads(p.stdout or "[]")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    created = 0
    with db() as con:
        # pastikan project aktif + punya list
        ex = con.execute("SELECT name FROM project_meta WHERE name=?", (name,)).fetchone()
        if not ex:
            con.execute("INSERT INTO project_meta(name,status,started_at,updated_at) VALUES(?,?,?,?)",
                        (name, "active", NOW(), NOW()))
        lid = _ensure_project_list(con, name)
        existing = set()
        for t in con.execute("SELECT tags FROM tasks WHERE list_id=?", (lid,)).fetchall():
            for tg in (t["tags"] or "").split(","):
                if tg.strip().startswith("gh#"):
                    existing.add(tg.strip())
        statuses = list_statuses({"statuses": con.execute("SELECT statuses FROM lists WHERE id=?", (lid,)).fetchone()["statuses"]})
        first = statuses[0]["id"] if statuses else "todo"
        for iss in issues:
            tag = f"gh#{iss['number']}"
            if tag in existing:
                continue
            con.execute("INSERT INTO tasks(list_id,title,description,status,tags,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (lid, iss["title"][:200], (iss.get("body") or "")[:4000], first, tag, NOW(), NOW()))
            created += 1
    return {"ok": True, "created": created, "list_id": lid}

@app.post("/api/files/issues/create")
def gh_issue_create(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    title = (b.get("title") or "").strip()
    body = (b.get("body") or "").strip()
    if not title:
        raise HTTPException(400, "judul wajib")
    try:
        p = subprocess.run([GH_BIN, "issue", "create", "--title", title, "--body", body or title],
                           cwd=full, capture_output=True, text=True, timeout=40)
        if p.returncode != 0:
            return {"ok": False, "error": ((p.stderr or p.stdout) or "").strip()[:200]}
        url = next((l.strip() for l in (p.stdout or "").splitlines() if "github.com" in l), "")
        return {"ok": True, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

# ===== Dashboard (ringkasan semua project) + bulk push =====
def _git_local_state(full):
    """Status git lokal cepat (TANPA fetch). 1 subprocess + 1 file read."""
    if not os.path.isdir(os.path.join(full, ".git")):
        return None
    # has_remote: baca .git/config (tanpa subprocess)
    has_remote = False
    try:
        with open(os.path.join(full, ".git", "config"), "r", errors="ignore") as f:
            has_remote = 'remote "origin"' in f.read()
    except OSError:
        pass
    # status --branch: header beri ahead/behind, sisanya = file dirty
    p = _gitp(full, ["status", "--porcelain=v1", "--branch"], timeout=10)
    if p.returncode != 0:
        return {"has_remote": has_remote, "dirty": 0, "ahead": 0, "state": "clean"}
    lines = (p.stdout or "").splitlines()
    head = lines[0] if lines and lines[0].startswith("##") else ""
    dirty = sum(1 for l in lines if l and not l.startswith("##"))
    ahead = 0
    m = re.search(r"ahead (\d+)", head)
    if m:
        ahead = int(m.group(1))
    state = "dirty" if dirty else ("ahead" if ahead else "clean")
    return {"has_remote": has_remote, "dirty": dirty, "ahead": ahead, "state": state}

_DASH_CACHE = {"ts": 0.0, "data": None}
DASH_TTL = 20  # detik

@app.get("/api/dashboard")
def dashboard(fresh: int = 0):
    now = time.time()
    if not fresh and _DASH_CACHE["data"] and now - _DASH_CACHE["ts"] < DASH_TTL:
        return _DASH_CACHE["data"]
    today = datetime.date.today().isoformat()
    with db() as con:
        meta = {r["name"]: {"status": r["status"], "list_id": r["list_id"]}
                for r in con.execute("SELECT name,status,list_id FROM project_meta").fetchall()}
        task_stat = {}
        for name, m in meta.items():
            lid = m["list_id"]
            if not lid:
                continue
            tot = done = od = 0
            for t in con.execute("SELECT status,due_date FROM tasks WHERE list_id=? AND parent_id IS NULL", (lid,)).fetchall():
                tot += 1
                if t["status"] == "done":
                    done += 1
                elif t["due_date"] and t["due_date"] < today:
                    od += 1
            task_stat[name] = {"total": tot, "done": done, "overdue": od}
    dirs = []
    try:
        for e in sorted(os.scandir(CODE_ROOT), key=lambda e: e.name.lower()):
            try:
                if e.is_dir(follow_symlinks=False) and e.name not in EXCLUDE_DIRS and not e.name.startswith("."):
                    dirs.append((e.name, e.path))
            except OSError:
                continue
    except OSError:
        pass
    # git state paralel (IO-bound subprocess) -> jauh lebih cepat dari sequential
    with ThreadPoolExecutor(max_workers=16) as ex:
        gits = list(ex.map(lambda d: _git_local_state(d[1]), dirs))
    out = [{"name": nm, "status": meta.get(nm, {}).get("status"),
            "git": g, "tasks": task_stat.get(nm)}
           for (nm, _), g in zip(dirs, gits)]
    data = {"projects": out}
    _DASH_CACHE["ts"] = now
    _DASH_CACHE["data"] = data
    return data

_ACT_CACHE = {"ts": 0.0, "data": None}
ACT_TTL = 45

@app.get("/api/activity")
def activity(limit: int = 50, fresh: int = 0):
    now = time.time()
    if not fresh and _ACT_CACHE["data"] and now - _ACT_CACHE["ts"] < ACT_TTL:
        return _ACT_CACHE["data"]
    items = []
    dirs = []
    try:
        for e in os.scandir(CODE_ROOT):
            try:
                if e.is_dir(follow_symlinks=False) and e.name not in EXCLUDE_DIRS and not e.name.startswith("."):
                    dirs.append((e.name, e.path))
            except OSError:
                continue
    except OSError:
        pass

    def repo_commits(d):
        name, full = d
        if not os.path.isdir(os.path.join(full, ".git")):
            return []
        p = _gitp(full, ["log", "-3", "--format=%h%x1f%s%x1f%cI%x1f%cr"], timeout=8)
        out = []
        for ln in (p.stdout or "").splitlines():
            a = ln.split("\x1f")
            if len(a) >= 4:
                out.append({"type": "commit", "project": name, "text": a[1],
                            "hash": a[0], "iso": a[2].replace("T", " ")[:19], "rel": a[3]})
        return out

    with ThreadPoolExecutor(max_workers=16) as ex:
        for lst in ex.map(repo_commits, dirs):
            items.extend(lst)

    with db() as con:
        rows_ = con.execute(
            "SELECT t.title,t.status,t.completed_at,t.created_at,t.updated_at,l.name lname "
            "FROM tasks t JOIN lists l ON l.id=t.list_id WHERE t.parent_id IS NULL "
            "ORDER BY COALESCE(t.completed_at,t.updated_at,t.created_at) DESC LIMIT 30").fetchall()
        for t in rows_:
            if t["status"] == "done" and t["completed_at"]:
                items.append({"type": "task_done", "project": t["lname"], "text": t["title"], "iso": t["completed_at"][:19]})
            elif t["created_at"]:
                items.append({"type": "task_new", "project": t["lname"], "text": t["title"], "iso": t["created_at"][:19]})

    items.sort(key=lambda x: x.get("iso") or "", reverse=True)
    data = {"items": items[:limit]}
    _ACT_CACHE["ts"] = now
    _ACT_CACHE["data"] = data
    return data

@app.post("/api/files/git/push-all")
def git_push_all(b: dict = Body(...)):
    """Push semua repo yang dirty/ahead. Sequential, per-repo result."""
    msg = (b.get("message") or "").strip() or ("update: " + NOW())
    results = []
    try:
        entries = sorted(os.scandir(CODE_ROOT), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False) or e.name in EXCLUDE_DIRS or e.name.startswith("."):
                continue
        except OSError:
            continue
        g = _git_local_state(e.path)
        if not g or not g["has_remote"] or (g["dirty"] == 0 and g["ahead"] == 0):
            continue
        full = e.path
        _gitp(full, ["add", "-A"])
        if (_gitp(full, ["status", "--porcelain"]).stdout or "").strip():
            _gitp(full, ["commit", "-m", msg], timeout=30)
        p = _gitp(full, ["push", "-u", "origin", "main"], timeout=120)
        results.append({"name": e.name, "ok": p.returncode == 0,
                        "error": "" if p.returncode == 0 else ((p.stderr or p.stdout) or "").strip()[:150]})
    _DASH_CACHE["ts"] = 0.0  # invalidate cache
    return {"pushed": results, "count": len(results)}

# ===== AI helper (OpenClaw): commit message / release notes / README =====
@app.post("/api/files/ai/commit-msg")
def ai_commit_msg(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    _gitp(full, ["add", "-A"])
    diff = (_gitp(full, ["diff", "HEAD", "--no-color"], timeout=20).stdout or "")
    if not diff.strip():
        return {"ok": False, "error": "Tidak ada perubahan untuk di-commit."}
    prompt = ("Buat 1 pesan commit git singkat (maks 72 char judul, gaya conventional commit, "
              "Bahasa Indonesia boleh). HANYA keluarkan pesannya, tanpa penjelasan. Diff:\n\n" + diff[:7000])
    text, ok = run_openclaw(prompt, session_key="ph-commit-" + os.path.basename(full), timeout=120)
    msg = (text or "").strip().splitlines()[0].strip().strip("`\"' ") if ok else ""
    return {"ok": bool(msg), "message": msg or text[:120], "raw": text[:500]}

@app.post("/api/files/ai/review-diff")
def ai_review_diff(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    _gitp(full, ["add", "-A"])
    diff = (_gitp(full, ["diff", "HEAD", "--no-color"], timeout=20).stdout or "")
    if not diff.strip():
        return {"ok": False, "error": "Tidak ada perubahan untuk direview."}
    prompt = ("Review perubahan kode (git diff) berikut sebelum di-push. Sebutkan secara ringkas (markdown): "
              "potensi bug, hal yang terlewat, kode debug yang harus dihapus, atau risiko. "
              "Kalau aman, bilang aman. Bahasa Indonesia.\n\nDiff:\n" + diff[:8000])
    text, ok = run_openclaw(prompt, session_key="ph-revdiff-" + os.path.basename(full), timeout=150)
    return {"ok": ok, "review": text}

@app.post("/api/files/ai/release-notes")
def ai_release_notes(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    # commit sejak tag terakhir (kalau ada)
    last = _gitp(full, ["describe", "--tags", "--abbrev=0"], timeout=10).stdout.strip()
    rng = f"{last}..HEAD" if last else "-30"
    log = (_gitp(full, ["log", rng, "--no-color", "--format=- %s"], timeout=15).stdout or "")
    if not log.strip():
        log = (_gitp(full, ["log", "-20", "--no-color", "--format=- %s"], timeout=15).stdout or "")
    prompt = ("Buat catatan rilis (release notes) markdown ringkas dari daftar commit berikut. "
              "Kelompokkan (Fitur/Perbaikan/Lainnya) bila relevan, Bahasa Indonesia. "
              "Keluarkan markdown saja.\n\nCommits:\n" + log[:6000])
    text, ok = run_openclaw(prompt, session_key="ph-relnotes-" + os.path.basename(full), timeout=150)
    return {"ok": ok, "notes": text, "since": last or None}

@app.post("/api/files/ai/readme")
def ai_readme(b: dict = Body(...)):
    full = _proj_git_dir(b.get("path", ""))
    name = os.path.basename(full)
    # kumpulkan struktur + cuplikan file penting (bounded)
    tree = []
    for root, dn, fn in os.walk(full):
        dn[:] = [d for d in dn if d not in EXCLUDE_DIRS and not d.startswith(".")]
        depth = os.path.relpath(root, full).count(os.sep)
        if depth > 2:
            dn[:] = []
            continue
        for f in sorted(fn)[:40]:
            tree.append(os.path.relpath(os.path.join(root, f), full).replace(os.sep, "/"))
        if len(tree) > 200:
            break
    snippets = []
    for cand in ("platformio.ini", "package.json", "main.py", "main.cpp", "src/main.cpp", "src/main.ino"):
        fp = os.path.join(full, cand)
        if os.path.isfile(fp):
            try:
                with open(fp, "rb") as f:
                    snippets.append(f"### {cand}\n" + f.read(1500).decode("utf-8", "replace"))
            except OSError:
                pass
    prompt = (f"Buat README.md profesional (markdown, Bahasa Indonesia) untuk project '{name}'. "
              "Sertakan: judul, deskripsi singkat, fitur, struktur folder, cara pakai bila bisa ditebak. "
              "Keluarkan HANYA markdown README.\n\nDaftar file:\n" + "\n".join(tree[:200]) +
              ("\n\nCuplikan:\n" + "\n\n".join(snippets) if snippets else ""))
    text, ok = run_openclaw(prompt, session_key="ph-readme-" + name, timeout=180)
    wrote = False
    if ok and b.get("write"):
        try:
            with open(os.path.join(full, "README.md"), "w", encoding="utf-8") as f:
                f.write(text)
            wrote = True
        except OSError:
            pass
    return {"ok": ok, "readme": text, "wrote": wrote}

def _decode_text(raw):
    """Decode bytes ke str dengan deteksi encoding (UTF-8/16, BOM, file Windows UTF-16)."""
    # BOM eksplisit
    if raw[:2] == b"\xff\xfe":
        return raw.decode("utf-16-le", errors="replace")
    if raw[:2] == b"\xfe\xff":
        return raw.decode("utf-16-be", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    # Heuristik: banyak null byte di sampel awal -> kemungkinan UTF-16 tanpa BOM
    sample = raw[:4096]
    if sample and sample.count(0) > len(sample) // 4:
        nul_even = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
        nul_odd = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        enc = "utf-16-be" if nul_even > nul_odd else "utf-16-le"
        try:
            return raw.decode(enc)
        except Exception:
            pass
    # default UTF-8
    return raw.decode("utf-8", errors="replace")

def _read_text_capped(full):
    """Baca file teks dengan cap MAX_TEXT_VIEW; flag truncated bila dipotong."""
    truncated = False
    with open(full, "rb") as f:
        raw = f.read(MAX_TEXT_VIEW + 1)
    if len(raw) > MAX_TEXT_VIEW:
        raw = raw[:MAX_TEXT_VIEW]
        truncated = True
    return _decode_text(raw), truncated

@app.get("/api/files/view")
def files_view(path: str = ""):
    """Konten file untuk review. Kembalikan {kind, ...} sesuai tipe."""
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    name = os.path.basename(full)
    ext = _ext_of(name)
    try:
        size = os.path.getsize(full)
    except OSError:
        size = 0
    rel = os.path.relpath(full, CODE_ROOT).replace(os.sep, "/")
    raw_url = f"/api/files/raw?path={quote(rel)}"

    # gambar + pdf -> serve mentah (aman, lihat files_raw)
    if "." + ext in CODE_IMG_EXT:
        return {"kind": "raw", "mime": mimetypes.guess_type(name)[0] or "application/octet-stream", "url": raw_url, "name": name, "size": size}
    if ext == "pdf":
        return {"kind": "raw", "mime": "application/pdf", "url": raw_url, "name": name, "size": size}
    # model 3D (STEP/STL/OBJ/dll) -> viewer O3DV di frontend
    if ext in MODEL3D_EXT:
        return {"kind": "model3d", "url": raw_url, "name": name, "size": size, "ext": ext}
    # KiCad board/skematik -> KiCanvas di frontend
    if ext in KICAD_EXT:
        return {"kind": "kicad", "url": raw_url, "name": name, "size": size, "ext": ext}
    # Gerber/drill -> render board (top+bottom) via pcb-stackup; frontend ambil semua layer di folder
    if ext in GERBER_EXT:
        rel_path = os.path.relpath(full, CODE_ROOT).replace(os.sep, "/")
        return {"kind": "gerber", "url": raw_url, "name": name, "size": size, "ext": ext, "path": rel_path}

    # docx -> mammoth -> html (frontend sanitasi DOMPurify)
    if ext == "docx":
        try:
            import mammoth
            with open(full, "rb") as f:
                result = mammoth.convert_to_html(f)
            return {"kind": "html", "html": result.value, "name": name}
        except Exception as e:
            return {"kind": "binary", "size": size, "url": raw_url, "name": name, "error": f"Gagal baca DOCX: {e}"}

    # xlsx -> openpyxl -> sheets (cap baris/kolom)
    if ext == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(full, read_only=True, data_only=True)
            sheets = []
            for ws in wb.worksheets:
                rows_out = []
                for ri, r in enumerate(ws.iter_rows(values_only=True)):
                    if ri >= 200:
                        break
                    cells = ["" if c is None else str(c) for c in r[:40]]
                    rows_out.append(cells)
                sheets.append({"name": ws.title, "rows": rows_out})
            wb.close()
            return {"kind": "xlsx", "sheets": sheets, "name": name}
        except Exception as e:
            return {"kind": "binary", "size": size, "url": raw_url, "name": name, "error": f"Gagal baca XLSX: {e}"}

    # csv -> rows (cap ~500 baris)
    if ext in ("csv", "tsv"):
        try:
            import csv as _csv
            content, _ = _read_text_capped(full)
            delim = "\t" if ext == "tsv" else ","
            rows_out = []
            for ri, r in enumerate(_csv.reader(content.splitlines(), delimiter=delim)):
                if ri >= 500:
                    break
                rows_out.append([str(c) for c in r[:40]])
            return {"kind": "csv", "rows": rows_out, "name": name}
        except Exception:
            pass  # fallback ke text di bawah

    # markdown
    if ext in ("md", "markdown"):
        content, truncated = _read_text_capped(full)
        return {"kind": "markdown", "content": content, "truncated": truncated, "name": name}

    # teks/kode whitelist
    if ext in TEXT_EXT or name.lower() in ("makefile", "dockerfile"):
        # SVG: jangan render sebagai HTML, cukup tampil sebagai teks (anti XSS)
        content, truncated = _read_text_capped(full)
        return {"kind": "text", "lang": ext, "content": content, "truncated": truncated, "name": name}

    # selain itu: biner / tidak dikenal -> download
    return {"kind": "binary", "size": size, "url": raw_url, "name": name}

@app.get("/api/files/raw")
def files_raw(path: str = ""):
    """Serve file mentah AMAN. Inline hanya untuk raster image + PDF; selain itu attachment.
    HTML/SVG TIDAK PERNAH inline. nosniff + CSP sandbox neutralize konten aktif."""
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    name = os.path.basename(full)
    ext = _ext_of(name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline_ok = ("." + ext in CODE_IMG_EXT) or ext == "pdf"
    disposition = "inline" if inline_ok else "attachment"
    # paksa mime aman untuk yang tidak inline (hindari sniff jadi html/svg aktif)
    serve_mime = mime if inline_ok else "application/octet-stream"
    safe_name = _safe_name(name)
    return FileResponse(full, media_type=serve_mime,
                        headers={
                            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
                            "X-Content-Type-Options": "nosniff",
                            "Content-Security-Policy": "default-src 'none'; sandbox",
                        })

# ---- File links (hubungkan file project ke task) ----
@app.post("/api/tasks/{tid}/filelink")
def add_filelink(tid: int, b: dict = Body(...)):
    # validasi path di dalam CODE_ROOT & file benar-benar ada
    full = _safe_path(b.get("path", ""))
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    rel = os.path.relpath(full, CODE_ROOT).replace(os.sep, "/")
    with db() as con:
        if not con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "task not found")
        cur = con.execute("INSERT INTO file_links(task_id,path,created_at) VALUES(?,?,?)", (tid, rel, NOW()))
        return filelink_dict(con.execute("SELECT * FROM file_links WHERE id=?", (cur.lastrowid,)).fetchone())

@app.delete("/api/filelinks/{fid}")
def del_filelink(fid: int):
    with db() as con:
        con.execute("DELETE FROM file_links WHERE id=?", (fid,))
    return {"ok": True}

# ---- Review file pakai OpenClaw ----
@app.post("/api/files/review")
def files_review(b: dict = Body(...)):
    """Baca isi file (cap), minta OpenClaw review/ringkasan kode (Bahasa Indonesia, Markdown)."""
    full = _safe_path(b.get("path", ""))
    if not os.path.isfile(full):
        raise HTTPException(404, "File tidak ditemukan")
    name = os.path.basename(full)
    ext = _ext_of(name)
    if ext not in TEXT_EXT and name.lower() not in ("makefile", "dockerfile"):
        raise HTTPException(400, "Hanya file teks/kode yang bisa direview")
    content, truncated = _read_text_capped(full)
    # cap konteks lebih kecil untuk prompt agar cepat & hemat
    snippet = content[:60000]
    rel = os.path.relpath(full, CODE_ROOT).replace(os.sep, "/")
    prompt = (
        "Kamu reviewer kode senior. Review file berikut dalam Bahasa Indonesia, format Markdown, ringkas & actionable.\n"
        "Gunakan heading: '## Ringkasan', '## Temuan / Potensi Masalah', '## Saran Perbaikan'.\n"
        f"File: {rel} (bahasa: {ext}{', dipotong' if truncated else ''})\n\n"
        f"```{ext}\n{snippet}\n```"
    )
    file_key = hashlib.sha1(rel.encode()).hexdigest()[:8]
    text, ok = run_openclaw(prompt, session_key=f"projecthub-file-{file_key}", timeout=240)
    return {"ok": ok, "text": text}

# ================= Static =================
app.mount("/assets", StaticFiles(directory=os.path.join(BASE,"static")), name="assets")

@app.get("/")
def index(): return FileResponse(os.path.join(BASE,"static","index.html"))

@app.get("/health")
def health(): return {"ok": True, "openclaw": bool(shutil.which("openclaw")), "auth": bool(AUTH), "time": NOW()}
