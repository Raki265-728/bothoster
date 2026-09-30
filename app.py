#!/usr/bin/env python3
"""
BotHoster — Working Flask backend
Run: python app.py
"""

import os, sys, re, uuid, time, shutil, zipfile, tarfile, sqlite3
import subprocess, threading, ast, importlib, logging, secrets
from datetime import datetime, timedelta
from functools import wraps

# ── Auto-install ────────────────────────────────────────
def _ensure(pkgs):
    for pip_name, imp in pkgs:
        try: __import__(imp)
        except ImportError:
            print(f"📦 Installing {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--quiet"])

_ensure([("Flask","flask"),("Werkzeug","werkzeug"),("psutil","psutil")])

from flask import (Flask, request, jsonify, session, redirect, url_for,
                   send_from_directory, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psutil

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("bothoster")

# ── Paths ───────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BOTS_DIR = os.path.join(DATA_DIR, "bots")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
DB_PATH  = os.path.join(DATA_DIR, "bothoster.db")

for d in (DATA_DIR, BOTS_DIR, LOGS_DIR, TEMP_DIR):
    os.makedirs(d, exist_ok=True)

# ── Flask ───────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── DB ──────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
dblock = threading.Lock()

def init_db():
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bots(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        token TEXT NOT NULL,
        chat_id TEXT,
        code_path TEXT,
        log_path TEXT,
        type TEXT,
        status TEXT DEFAULT 'stopped',
        pid INTEGER,
        created_at TEXT)""")
    conn.commit()
init_db()

# ── Process registry ────────────────────────────────────
procs = {}
plock = threading.Lock()

# ── File type / extract / find ──────────────────────────
def get_file_type(fn):
    if not fn: return "unknown"
    n = fn.lower()
    if n.endswith(".py"): return "python"
    if n.endswith(".js"): return "javascript"
    if n.endswith(".zip"): return "zip"
    if any(n.endswith(e) for e in [".tar",".tar.gz",".tgz"]): return "archive"
    return "unknown"

def extract_archive(path, out):
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z: z.extractall(out)
        elif path.lower().endswith((".tar.gz",".tgz")):
            with tarfile.open(path,"r:gz") as t: t.extractall(out)
        elif path.lower().endswith(".tar"):
            with tarfile.open(path,"r") as t: t.extractall(out)
        else: return False, "Unsupported"
        return True, None
    except Exception as e: return False, str(e)

def find_main_file(d):
    priority = ["main.py","bot.py","app.py","server.py","index.py",
                "main.js","bot.js","app.js","index.js"]
    for f in priority:
        p = os.path.join(d,f)
        if os.path.isfile(p): return p
    for root,_,files in os.walk(d):
        for f in priority:
            if f in files: return os.path.join(root,f)
    for root,_,files in os.walk(d):
        for f in files:
            if f.endswith((".py",".js")): return os.path.join(root,f)
    return None

def install_requirements_file(path):
    if not os.path.exists(path): return
    with open(path) as f:
        pkgs = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    for pkg in pkgs:
        try:
            subprocess.run([sys.executable,"-m","pip","install",pkg,"--quiet"],
                           capture_output=True, timeout=180)
        except: pass

def extract_imports(fp):
    imps = set()
    try:
        with open(fp, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: imps.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level==0:
                imps.add(node.module.split(".")[0])
    except: pass
    return imps

def install_missing_imports(imps):
    map_pip = {"telebot":"pyTelegramBotAPI","PIL":"Pillow","cv2":"opencv-python",
               "Crypto":"pycryptodome","bs4":"beautifulsoup4","dotenv":"python-dotenv",
               "yaml":"PyYAML","aiogram":"aiogram","telethon":"Telethon"}
    for m in imps:
        try: importlib.import_module(m); continue
        except ImportError: pass
        pip_name = map_pip.get(m, m)
        try:
            subprocess.run([sys.executable,"-m","pip","install",pip_name,"--quiet"],
                           capture_output=True, timeout=180)
        except: pass

# ── Bot runner ──────────────────────────────────────────
def start_bot_process(bot_id, code_path, log_path, env_extra=None):
    with plock:
        if bot_id in procs and procs[bot_id].poll() is None:
            return False, "Already running"

    ext = os.path.splitext(code_path)[1].lower()
    workdir = os.path.dirname(code_path)

    # install deps
    if ext == ".py":
        req = os.path.join(workdir, "requirements.txt")
        install_requirements_file(req)
        imps = extract_imports(code_path)
        install_missing_imports(imps)

    if ext == ".py":    cmd = [sys.executable, "-u", code_path]
    elif ext == ".js":  cmd = ["node", code_path]
    else:               return False, f"Unsupported: {ext}"

    env = os.environ.copy()
    if env_extra: env.update({k:str(v) for k,v in env_extra.items()})

    log_f = open(log_path, "a")
    log_f.write(f"\n{'='*50}\n▶ START {datetime.utcnow().isoformat()}\n{'='*50}\n")
    log_f.flush()

    try:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                cwd=workdir, env=env, text=True)
    except Exception as e:
        log_f.close()
        return False, f"Failed: {e}"

    with plock: procs[bot_id] = proc

    def watch():
        try:
            code = proc.wait()
        finally:
            with plock: procs.pop(bot_id, None)
            try:
                with dblock:
                    c = conn.cursor()
                    c.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
                    conn.commit()
            except: pass
            log_f.write(f"\n■ EXIT code={code} {datetime.utcnow().isoformat()}\n")
            log_f.close()

    threading.Thread(target=watch, daemon=True).start()
    return True, "Started"

def stop_bot_process(bot_id):
    with plock:
        proc = procs.pop(bot_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()

def is_running(bot_id):
    with plock:
        p = procs.get(bot_id)
    return p is not None and p.poll() is None

def read_logs(log_path, lines=300):
    try:
        with open(log_path) as f:
            return "".join(f.readlines()[-lines:])
    except FileNotFoundError:
        return "(no logs yet)"

# ── Auth decorator ──────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return jsonify(error="Not authenticated", ok=False), 401
        return f(*a, **k)
    return w

# ── Frontend (serve index.html) ─────────────────────────
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

# ── Auth routes ─────────────────────────────────────────
@app.route("/api/signup", methods=["POST"])
def signup():
    email = (request.form.get("email") or "").strip().lower()
    pw    = request.form.get("password") or ""
    if not email or not pw: return jsonify(ok=False, error="Missing fields")
    if len(pw) < 6: return jsonify(ok=False, error="Password min 6 chars")
    try:
        with dblock:
            c = conn.cursor()
            c.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",
                      (email, generate_password_hash(pw), datetime.utcnow().isoformat()))
            conn.commit()
            uid = c.lastrowid
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error="Email already registered")
    session.permanent = True
    session["uid"] = uid
    session["email"] = email
    return jsonify(ok=True)

@app.route("/api/login", methods=["POST"])
def login():
    email = (request.form.get("email") or "").strip().lower()
    pw    = request.form.get("password") or ""
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], pw):
        return jsonify(ok=False, error="Invalid email or password")
    session.permanent = True
    session["uid"] = row["id"]
    session["email"] = row["email"]
    return jsonify(ok=True)

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)

@app.route("/api/me")
def me():
    if "uid" not in session: return jsonify(ok=False)
    return jsonify(ok=True, email=session["email"], uid=session["uid"])

# ── Stats ───────────────────────────────────────────────
@app.route("/api/stats")
@login_required
def stats():
    rows = conn.execute("SELECT id,status FROM bots WHERE user_id=?", (session["uid"],)).fetchall()
    total = len(rows)
    running = sum(1 for r in rows if is_running(r["id"]))
    try: cpu = psutil.cpu_percent(interval=0.1)
    except: cpu = 0
    return jsonify(ok=True, total=total, running=running, stopped=total-running, cpu=cpu)

# ── List bots ───────────────────────────────────────────
@app.route("/api/bots")
@login_required
def list_bots():
    rows = conn.execute("SELECT * FROM bots WHERE user_id=? ORDER BY created_at DESC",
                        (session["uid"],)).fetchall()
    bots = []
    for r in rows:
        bots.append({
            "id": r["id"], "name": r["name"], "type": r["type"],
            "created_at": r["created_at"], "running": is_running(r["id"])
        })
    return jsonify(ok=True, bots=bots)

# ── Deploy (paste) ──────────────────────────────────────
@app.route("/api/deploy", methods=["POST"])
@login_required
def deploy():
    name    = (request.form.get("name") or "My Bot").strip()
    token   = (request.form.get("token") or "").strip()
    chat_id = (request.form.get("chat_id") or "").strip()
    code    = request.form.get("code") or ""

    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        return jsonify(ok=False, error="Invalid bot token format")
    if not chat_id.lstrip("-").isdigit():
        return jsonify(ok=False, error="Invalid chat ID")
    if not code.strip():
        return jsonify(ok=False, error="Code is empty")

    bot_id = uuid.uuid4().hex[:10]
    user_dir = os.path.join(BOTS_DIR, str(session["uid"]), bot_id)
    os.makedirs(user_dir, exist_ok=True)

    code_path = os.path.join(user_dir, "main.py")
    with open(code_path, "w") as f: f.write(code)
    log_path = os.path.join(LOGS_DIR, f"{bot_id}.log")

    with dblock:
        c = conn.cursor()
        c.execute("""INSERT INTO bots(id,user_id,name,token,chat_id,
                     code_path,log_path,type,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (bot_id, session["uid"], name, token, chat_id,
                   code_path, log_path, "python", datetime.utcnow().isoformat()))
        conn.commit()

    ok, msg = start_bot_process(bot_id, code_path, log_path,
                                 env_extra={"BOT_TOKEN": token, "ADMIN_CHAT_ID": chat_id})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        return jsonify(ok=True, bot_id=bot_id)
    return jsonify(ok=False, error=msg)

# ── Upload ──────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        return jsonify(ok=False, error="No file")
    f = request.files["file"]
    token   = (request.form.get("token") or "").strip()
    chat_id = (request.form.get("chat_id") or "").strip()
    name    = (request.form.get("name") or f.filename).strip()

    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        return jsonify(ok=False, error="Invalid token")
    if not chat_id.lstrip("-").isdigit():
        return jsonify(ok=False, error="Invalid chat ID")

    bot_id = uuid.uuid4().hex[:10]
    user_dir = os.path.join(BOTS_DIR, str(session["uid"]), bot_id)
    os.makedirs(user_dir, exist_ok=True)

    fname = secure_filename(f.filename)
    tmp = os.path.join(TEMP_DIR, f"{bot_id}_{fname}")
    f.save(tmp)

    ftype = get_file_type(fname)
    if ftype in ("zip", "archive"):
        ok, err = extract_archive(tmp, user_dir)
        if not ok: return jsonify(ok=False, error=f"Extract failed: {err}")
        os.remove(tmp)
        main = find_main_file(user_dir)
        if not main: return jsonify(ok=False, error="No main .py/.js in archive")
        code_path = main
        ftype = get_file_type(main)
    elif ftype in ("python", "javascript"):
        code_path = os.path.join(user_dir, fname)
        os.rename(tmp, code_path)
    else:
        os.remove(tmp)
        return jsonify(ok=False, error="Only .py, .js, .zip, .tar.gz allowed")

    log_path = os.path.join(LOGS_DIR, f"{bot_id}.log")

    with dblock:
        c = conn.cursor()
        c.execute("""INSERT INTO bots(id,user_id,name,token,chat_id,
                     code_path,log_path,type,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (bot_id, session["uid"], name, token, chat_id,
                   code_path, log_path, ftype, datetime.utcnow().isoformat()))
        conn.commit()

    ok, msg = start_bot_process(bot_id, code_path, log_path,
                                 env_extra={"BOT_TOKEN": token, "ADMIN_CHAT_ID": chat_id})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        return jsonify(ok=True, bot_id=bot_id)
    return jsonify(ok=False, error=msg)

# ── Bot controls ────────────────────────────────────────
def own_bot(bot_id):
    return conn.execute("SELECT * FROM bots WHERE id=? AND user_id=?",
                        (bot_id, session["uid"])).fetchone()

@app.route("/api/bot/<bot_id>/start", methods=["POST"])
@login_required
def api_start(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    ok, msg = start_bot_process(bot_id, row["code_path"], row["log_path"],
                                 env_extra={"BOT_TOKEN": row["token"], "ADMIN_CHAT_ID": row["chat_id"]})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg)

@app.route("/api/bot/<bot_id>/stop", methods=["POST"])
@login_required
def api_stop(bot_id):
    if not own_bot(bot_id): return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    conn.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
    conn.commit()
    return jsonify(ok=True)

@app.route("/api/bot/<bot_id>/restart", methods=["POST"])
@login_required
def api_restart(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    time.sleep(1)
    ok, msg = start_bot_process(bot_id, row["code_path"], row["log_path"],
                                 env_extra={"BOT_TOKEN": row["token"], "ADMIN_CHAT_ID": row["chat_id"]})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg)

@app.route("/api/bot/<bot_id>/delete", methods=["POST"])
@login_required
def api_delete(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    try: shutil.rmtree(os.path.dirname(row["code_path"]), ignore_errors=True)
    except: pass
    conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    conn.commit()
    return jsonify(ok=True)

@app.route("/api/bot/<bot_id>/logs")
@login_required
def api_logs(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    return jsonify(ok=True, logs=read_logs(row["log_path"], 300))

# ── Run ─────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*55}\n🤖 BotHoster → http://localhost:{port}\n{'='*55}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
