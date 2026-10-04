#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AY Telegram Hosting
-------------------
Render Web Service + Telegram Topic storage.

Supported uploads:
- Python .py
- Node.js .js

Permanent project storage:
- Telegram supergroup topic (message_thread_id)

Render local disk is used only as temporary runtime/cache space.
No Render Persistent Disk is required.

IMPORTANT:
Set all secrets/configuration through Render Environment Variables.
"""

import os
import sys
import re
import ast
import time
import json
import signal
import shutil
import sqlite3
import logging
import threading
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------
# Dependencies
# ---------------------------------------------------------
try:
    import requests
    import psutil
    import telebot
    from flask import Flask, jsonify
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
except ImportError as exc:
    raise SystemExit(
        f"Missing dependency: {exc}. Run: pip install -r requirements.txt"
    )

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("8591198249:AAEaydQVNM_NaCpZYIiFPIxeBB8IWtrdpwc", "8591198249:AAEaydQVNM_NaCpZYIiFPIxeBB8IWtrdpwc").strip()

# Numeric Telegram supergroup/channel ID, e.g. -1001234567890
STORAGE_CHAT_ID_RAW = os.getenv("-1003866131609", "-1003866131609").strip()

# Topic ID from your link .../178
STORAGE_THREAD_ID = int(os.getenv("STORAGE_THREAD_ID", "178"))

# Optional: comma-separated Telegram user IDs allowed to use hosting.
# Empty = every user can use it.
ALLOWED_USER_IDS_RAW = os.getenv("1087968824", "").strip()

# Runtime limits
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
SUPERVISOR_INTERVAL = int(os.getenv("SUPERVISOR_INTERVAL", "10"))
TEMP_TTL_SECONDS = int(os.getenv("TEMP_TTL_SECONDS", "3600"))
PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
TEMP_DIR = RUNTIME_DIR / "temp"
DB_PATH = RUNTIME_DIR / "metadata.db"
LOG_DIR = RUNTIME_DIR / "logs"

for d in (RUNTIME_DIR, TEMP_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")

if not STORAGE_CHAT_ID_RAW:
    raise SystemExit("STORAGE_CHAT_ID environment variable is required.")

try:
    STORAGE_CHAT_ID = int(STORAGE_CHAT_ID_RAW)
except ValueError:
    raise SystemExit("STORAGE_CHAT_ID must be a numeric Telegram chat ID.")

ALLOWED_USER_IDS = set()
if ALLOWED_USER_IDS_RAW:
    for x in ALLOWED_USER_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("AY-HOSTING")

# ---------------------------------------------------------
# Database
# ---------------------------------------------------------
db_lock = threading.RLock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

def db_init():
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                orig_name TEXT NOT NULL,
                safe_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT DEFAULT '',
                uploaded_at TEXT NOT NULL,
                status TEXT DEFAULT 'Stopped',
                autostart INTEGER DEFAULT 1,
                pid INTEGER,
                restart_count INTEGER DEFAULT 0,
                last_start TEXT,
                last_stop TEXT,
                last_error TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        """)
        conn.commit()

db_init()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def register_user(user):
    with db_lock:
        conn.execute("""
            INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            now_iso(),
            now_iso()
        ))
        conn.commit()

def add_file_record(user, doc, storage_message):
    name = Path(doc.file_name or "script.py").name
    ext = Path(name).suffix.lower()
    file_type = "python" if ext == ".py" else "javascript"
    with db_lock:
        cur = conn.execute("""
            INSERT INTO files (
                user_id, username, orig_name, safe_name, file_type,
                telegram_message_id, telegram_file_id,
                telegram_file_unique_id, uploaded_at, status, autostart
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Stopped', 1)
        """, (
            user.id,
            user.username or "",
            name,
            f"{int(time.time())}_{name}",
            file_type,
            storage_message.message_id,
            storage_message.document.file_id,
            storage_message.document.file_unique_id or "",
            now_iso()
        ))
        conn.commit()
        return cur.lastrowid

def get_file(file_id):
    with db_lock:
        return conn.execute(
            "SELECT * FROM files WHERE id=?",
            (file_id,)
        ).fetchone()

def user_files(user_id):
    with db_lock:
        return conn.execute(
            "SELECT * FROM files WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        ).fetchall()

def update_file(file_id, **fields):
    if not fields:
        return
    allowed = {
        "status", "pid", "autostart", "restart_count",
        "last_start", "last_stop", "last_error"
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [file_id]
    with db_lock:
        conn.execute(f"UPDATE files SET {sql} WHERE id=?", values)
        conn.commit()

def delete_file_record(file_id):
    with db_lock:
        conn.execute("DELETE FROM runs WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

# ---------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def allowed(user_id):
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📤 Upload File"),
        KeyboardButton("📁 My Files")
    )
    kb.add(
        KeyboardButton("📊 Statistics"),
        KeyboardButton("⚡ Server Status")
    )
    return kb

def safe_html(s):
    import html
    return html.escape(str(s))

def file_type(name):
    ext = Path(name).suffix.lower()
    if ext == ".py":
        return "python"
    if ext == ".js":
        return "javascript"
    return "unknown"

# ---------------------------------------------------------
# Telegram storage
# ---------------------------------------------------------
def store_document_in_telegram(message):
    """
    Re-downloads the incoming Telegram document and posts it
    into the configured storage topic.

    The temporary local copy is deleted after the Telegram
    storage message succeeds.
    """
    doc = message.document
    if not doc:
        raise ValueError("Document missing.")

    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        raise ValueError(
            f"File is too large. Maximum allowed: {MAX_FILE_MB} MB."
        )

    if file_type(doc.file_name or "") == "unknown":
        raise ValueError("Only .py and .js files are supported.")

    tg_file = bot.get_file(doc.file_id)
    data = bot.download_file(tg_file.file_path)

    tmp_path = TEMP_DIR / f"upload_{message.from_user.id}_{int(time.time()*1000)}_{Path(doc.file_name).name}"
    tmp_path.write_bytes(data)

    try:
        with tmp_path.open("rb") as fh:
            storage_message = bot.send_document(
                STORAGE_CHAT_ID,
                fh,
                caption=(
                    "🗄 <b>AY HOSTING STORAGE</b>\n\n"
                    f"📄 <b>File:</b> {safe_html(doc.file_name)}\n"
                    f"👤 <b>User ID:</b> <code>{message.from_user.id}</code>\n"
                    f"🕒 <b>Stored:</b> {safe_html(now_iso())}\n"
                    f"🧵 <b>Topic:</b> <code>{STORAGE_THREAD_ID}</code>"
                ),
                message_thread_id=STORAGE_THREAD_ID,
                disable_notification=True
            )
        return storage_message
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

def download_storage_file(file_record, destination):
    """
    Fetches the Telegram storage message and downloads the
    stored document into a temporary runtime file.
    """
    # The file_id belongs to this bot and remains usable after the
    # storage message is posted. Download directly from Telegram;
    # do not forward the storage message into the user's private chat.
    stored_file_id = file_record["telegram_file_id"]
    tg_file = bot.get_file(stored_file_id)
    data = bot.download_file(tg_file.file_path)
    Path(destination).write_bytes(data)

# ---------------------------------------------------------
# Process manager
# ---------------------------------------------------------
processes = {}
proc_lock = threading.RLock()

def terminate_process_tree(proc):
    if not proc:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
    except Exception as exc:
        logger.warning("terminate error: %s", exc)

def python_imports(path):
    result = set()
    try:
        tree = ast.parse(
            Path(path).read_text(encoding="utf-8", errors="ignore")
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                result.add(node.module.split(".")[0])
    except Exception:
        pass
    return result

STANDARD_MODULES = {
    "os","sys","re","json","time","math","random","datetime","asyncio",
    "threading","subprocess","sqlite3","logging","signal","shutil","tempfile",
    "pathlib","collections","itertools","functools","typing","traceback",
    "socket","http","urllib","email","hashlib","base64","secrets","csv",
    "io","string","statistics","ast","html","platform","enum","dataclasses",
    "queue","concurrent","contextlib","inspect","struct","uuid","decimal",
    "fractions","glob","fnmatch","getpass","argparse","configparser"
}

PIP_MAP = {
    "telebot": "pyTelegramBotAPI",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "yt_dlp": "yt-dlp",
    "Crypto": "pycryptodome",
    "discord": "discord.py",
    "aiogram": "aiogram",
    "pyrogram": "pyrogram",
    "tgcrypto": "tgcrypto",
    "requests": "requests",
}

def install_python_imports(path, working_dir):
    imports = python_imports(path)
    for module in imports:
        if module in STANDARD_MODULES:
            continue
        try:
            __import__(module)
            continue
        except ImportError:
            pass
        package = PIP_MAP.get(module, module)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", package, "--disable-pip-version-check"],
                cwd=working_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False
            )
        except Exception as exc:
            logger.warning("dependency install failed %s: %s", package, exc)

def install_requirements_if_present(working_dir):
    req = Path(working_dir) / "requirements.txt"
    if req.exists():
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req),
                 "--disable-pip-version-check"],
                cwd=working_dir,
                timeout=300,
                check=False
            )
        except Exception as exc:
            logger.warning("requirements install failed: %s", exc)

def start_host(file_id, chat_id=None, recovery=False):
    def worker():
        rec = get_file(file_id)
        if not rec:
            return

        with proc_lock:
            current = processes.get(file_id)
            if current:
                try:
                    if current["process"].poll() is None:
                        return
                except Exception:
                    pass
                processes.pop(file_id, None)

        ext = ".py" if rec["file_type"] == "python" else ".js"
        work_dir = Path(tempfile.mkdtemp(prefix=f"ayhost_{file_id}_", dir=TEMP_DIR))
        target = work_dir / rec["safe_name"]

        try:
            # Pull permanent source from Telegram.
            download_storage_file(rec, target)

            if rec["file_type"] == "python":
                install_requirements_if_present(work_dir)
                install_python_imports(target, work_dir)
                cmd = [sys.executable, "-u", str(target)]
            else:
                # JS can use installed npm packages only if the uploaded script
                # itself is self-contained or dependencies are already available.
                # This project intentionally does not support ZIP/package uploads.
                cmd = ["node", str(target)]

            log_path = LOG_DIR / f"file_{file_id}_{int(time.time())}.log"
            log = log_path.open("a", encoding="utf-8")
            log.write(f"\n=== START {now_iso()} recovery={recovery} ===\n")
            log.flush()

            kwargs = {
                "cwd": str(work_dir),
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name != "nt":
                kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **kwargs)

            with db_lock:
                cur = conn.execute("""
                    INSERT INTO runs(file_id, started_at, pid, log_path)
                    VALUES (?, ?, ?, ?)
                """, (file_id, now_iso(), proc.pid, str(log_path)))
                run_id = cur.lastrowid
                conn.commit()

            update_file(
                file_id,
                status="Running",
                pid=proc.pid,
                last_start=now_iso(),
                last_error=None
            )
            if recovery:
                with db_lock:
                    conn.execute(
                        "UPDATE files SET restart_count=COALESCE(restart_count,0)+1 WHERE id=?",
                        (file_id,)
                    )
                    conn.commit()

            with proc_lock:
                processes[file_id] = {
                    "process": proc,
                    "log": log,
                    "log_path": str(log_path),
                    "work_dir": str(work_dir),
                    "run_id": run_id,
                    "chat_id": chat_id,
                    "started": time.time()
                }

            if chat_id:
                try:
                    bot.send_message(
                        chat_id,
                        f"✅ <b>{safe_html(rec['orig_name'])}</b>\n\n"
                        f"🟢 Hosting: <b>Running</b>\n"
                        f"🆔 PID: <code>{proc.pid}</code>\n"
                        f"🗄 Storage: <b>Telegram Topic</b>"
                    )
                except Exception:
                    pass

            def monitor():
                try:
                    code = proc.wait()
                except Exception:
                    code = -1

                try:
                    log.write(f"\n=== STOP {now_iso()} exit={code} ===\n")
                    log.flush()
                    log.close()
                except Exception:
                    pass

                with proc_lock:
                    current = processes.get(file_id)
                    if current and current["process"] is proc:
                        processes.pop(file_id, None)

                with db_lock:
                    conn.execute(
                        "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
                        (now_iso(), code, run_id)
                    )
                    conn.commit()

                update_file(
                    file_id,
                    status="Stopped",
                    pid=None,
                    last_stop=now_iso(),
                    last_error=f"Process exited with code {code}"
                )

                # Runtime files are not permanent.
                shutil.rmtree(work_dir, ignore_errors=True)

            threading.Thread(target=monitor, daemon=True).start()

        except Exception as exc:
            logger.exception("start_host failed")
            update_file(
                file_id,
                status="Stopped",
                pid=None,
                last_stop=now_iso(),
                last_error=str(exc)
            )
            shutil.rmtree(work_dir, ignore_errors=True)
            if chat_id:
                try:
                    bot.send_message(
                        chat_id,
                        f"❌ <b>Hosting failed</b>\n\n<code>{safe_html(exc)}</code>"
                    )
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()

def stop_host(file_id, manual=True):
    with proc_lock:
        item = processes.get(file_id)

    if item:
        terminate_process_tree(item["process"])
        try:
            item["log"].close()
        except Exception:
            pass
        shutil.rmtree(item["work_dir"], ignore_errors=True)
        with proc_lock:
            processes.pop(file_id, None)

    update_file(
        file_id,
        autostart=0 if manual else 1,
        status="Stopped",
        pid=None,
        last_stop=now_iso(),
        last_error="Manually stopped" if manual else None
    )

def restart_host(file_id, chat_id):
    stop_host(file_id, manual=False)
    update_file(file_id, autostart=1)
    time.sleep(2)
    start_host(file_id, chat_id=chat_id, recovery=False)

# ---------------------------------------------------------
# Supervisor
# ---------------------------------------------------------
supervisor_running = True

def supervisor():
    time.sleep(5)
    while supervisor_running:
        try:
            with db_lock:
                rows = conn.execute(
                    "SELECT * FROM files WHERE autostart=1"
                ).fetchall()

            for rec in rows:
                with proc_lock:
                    item = processes.get(rec["id"])

                alive = False
                if item:
                    try:
                        alive = item["process"].poll() is None
                    except Exception:
                        alive = False

                if not alive:
                    start_host(
                        rec["id"],
                        chat_id=rec["user_id"],
                        recovery=True
                    )
                time.sleep(0.2)

            # Clean stale temp directories.
            cutoff = time.time() - TEMP_TTL_SECONDS
            for child in TEMP_DIR.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                except Exception:
                    pass

        except Exception:
            logger.exception("supervisor error")

        time.sleep(SUPERVISOR_INTERVAL)

# ---------------------------------------------------------
# UI helpers
# ---------------------------------------------------------
def actions_kb(file_id, running):
    kb = InlineKeyboardMarkup()
    if running:
        kb.row(
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    kb.row(
        InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}")
    )
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="files"))
    return kb

def show_file(chat_id, user_id, file_id, message_id=None):
    rec = get_file(file_id)
    if not rec:
        bot.send_message(chat_id, "❌ File not found.")
        return
    if rec["user_id"] != user_id:
        bot.send_message(chat_id, "❌ Access denied.")
        return

    with proc_lock:
        item = processes.get(file_id)
    running = bool(item and item["process"].poll() is None)

    text = (
        "⚙️ <b>AY Hosting Dashboard</b>\n\n"
        f"📄 <b>File:</b> {safe_html(rec['orig_name'])}\n"
        f"⚙️ <b>Type:</b> {rec['file_type']}\n"
        f"📈 <b>Status:</b> {'🟢 Running' if running else '🔴 Stopped'}\n"
        f"🗄 <b>Storage:</b> Telegram Topic #{STORAGE_THREAD_ID}\n"
        f"🔄 <b>Auto Recovery:</b> {'ON' if rec['autostart'] else 'OFF'}\n"
        f"♻️ <b>Recovery Count:</b> {rec['restart_count'] or 0}\n"
    )
    if rec["last_error"]:
        text += f"\n⚠️ <code>{safe_html(rec['last_error'][-500:])}</code>"

    if message_id:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=actions_kb(file_id, running)
            )
            return
        except Exception:
            pass

    bot.send_message(
        chat_id,
        text,
        reply_markup=actions_kb(file_id, running)
    )

# ---------------------------------------------------------
# Commands / handlers
# ---------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    register_user(message.from_user)
    if not allowed(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ You are not allowed to use this hosting bot.")
        return

    bot.send_message(
        message.chat.id,
        "🔥 <b>AY HOSTING SERVERS</b>\n\n"
        f"👋 Welcome <b>{safe_html(message.from_user.first_name or 'User')}</b>\n\n"
        "🐍 Python <b>.py</b>\n"
        "🟢 Node.js <b>.js</b>\n"
        "🗄 Telegram Topic Storage\n"
        "♻️ Auto Recovery\n"
        "📊 Server Status\n\n"
        "📌 ZIP/TAR support নেই।",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_help(message):
    bot.send_message(
        message.chat.id,
        "📤 <b>Upload File</b>\n\n"
        "শুধু <code>.py</code> অথবা <code>.js</code> file পাঠান।\n\n"
        f"Maximum: <b>{MAX_FILE_MB} MB</b>\n"
        "ফাইল প্রথমে Telegram storage topic-এ সংরক্ষিত হবে।"
    )

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files(message):
    register_user(message.from_user)
    rows = user_files(message.from_user.id)
    if not rows:
        bot.send_message(message.chat.id, "📁 আপনার কোনো hosted file নেই।")
        return

    kb = InlineKeyboardMarkup()
    for r in rows:
        with proc_lock:
            item = processes.get(r["id"])
        running = bool(item and item["process"].poll() is None)
        icon = "🟢" if running else "🔴"
        kb.add(
            InlineKeyboardButton(
                f"{icon} {r['orig_name']}",
                callback_data=f"manage:{r['id']}"
            )
        )
    bot.send_message(
        message.chat.id,
        "📁 <b>Your Hosted Files</b>\n\nফাইল নির্বাচন করুন:",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def statistics(message):
    with db_lock:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='Running'"
        ).fetchone()[0]
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    bot.send_message(
        message.chat.id,
        "📊 <b>AY Hosting Statistics</b>\n\n"
        f"👥 Users: <code>{users}</code>\n"
        f"📁 Hosted files: <code>{files}</code>\n"
        f"🟢 Running: <code>{running}</code>\n"
        f"⚡ CPU: <code>{cpu:.1f}%</code>\n"
        f"💾 RAM: <code>{ram:.1f}%</code>"
    )

@bot.message_handler(func=lambda m: m.text == "⚡ Server Status")
def server_status(message):
    bot.send_message(
        message.chat.id,
        "🟢 <b>AY Hosting Server</b>\n\n"
        f"🗄 Storage: Telegram Topic #{STORAGE_THREAD_ID}\n"
        f"🔄 Supervisor: {SUPERVISOR_INTERVAL}s\n"
        f"📦 Max file: {MAX_FILE_MB} MB\n"
        "🐍 Python: ON\n"
        "🟢 Node.js: ON\n"
        "📦 ZIP/TAR: OFF"
    )

@bot.message_handler(content_types=["document"])
def document_handler(message):
    register_user(message.from_user)

    if not allowed(message.from_user.id):
        bot.reply_to(message, "⛔ You are not allowed to use this hosting bot.")
        return

    name = message.document.file_name or ""
    typ = file_type(name)
    if typ == "unknown":
        bot.reply_to(
            message,
            "❌ শুধু <code>.py</code> এবং <code>.js</code> file allowed."
        )
        return

    if message.document.file_size and message.document.file_size > MAX_FILE_BYTES:
        bot.reply_to(
            message,
            f"❌ File too large. Maximum <b>{MAX_FILE_MB} MB</b>."
        )
        return

    status = bot.reply_to(message, "⏳ Telegram storage-এ file save হচ্ছে...")

    try:
        storage_msg = store_document_in_telegram(message)
        file_id = add_file_record(
            message.from_user,
            message.document,
            storage_msg
        )

        bot.edit_message_text(
            "✅ <b>File Stored Successfully</b>\n\n"
            f"📄 {safe_html(name)}\n"
            f"🗄 Telegram Topic #{STORAGE_THREAD_ID}\n"
            "🚀 Hosting শুরু হচ্ছে...",
            message.chat.id,
            status.message_id
        )

        start_host(file_id, chat_id=message.chat.id, recovery=False)

    except Exception as exc:
        logger.exception("upload failed")
        try:
            bot.edit_message_text(
                f"❌ <b>Upload failed</b>\n\n<code>{safe_html(exc)}</code>",
                message.chat.id,
                status.message_id
            )
        except Exception:
            bot.reply_to(message, f"❌ {safe_html(exc)}")

@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data or ""
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    if data == "files":
        my_files(call.message)
        return

    if ":" not in data:
        return

    action, raw_id = data.split(":", 1)
    try:
        file_id = int(raw_id)
    except ValueError:
        return

    rec = get_file(file_id)
    if not rec or rec["user_id"] != user_id:
        bot.send_message(chat_id, "❌ Access denied.")
        return

    if action == "manage":
        show_file(chat_id, user_id, file_id, call.message.message_id)

    elif action == "start":
        update_file(file_id, autostart=1)
        start_host(file_id, chat_id=chat_id)
        time.sleep(0.5)
        show_file(chat_id, user_id, file_id, call.message.message_id)

    elif action == "stop":
        stop_host(file_id, manual=True)
        show_file(chat_id, user_id, file_id, call.message.message_id)

    elif action == "restart":
        threading.Thread(
            target=restart_host,
            args=(file_id, chat_id),
            daemon=True
        ).start()
        time.sleep(0.5)
        show_file(chat_id, user_id, file_id, call.message.message_id)

    elif action == "logs":
        with proc_lock:
            item = processes.get(file_id)
        log_path = item["log_path"] if item else None
        if not log_path:
            with db_lock:
                row = conn.execute(
                    "SELECT log_path FROM runs WHERE file_id=? ORDER BY id DESC LIMIT 1",
                    (file_id,)
                ).fetchone()
            log_path = row["log_path"] if row else None

        if not log_path or not Path(log_path).exists():
            logs = "No logs found."
        else:
            lines = Path(log_path).read_text(
                encoding="utf-8", errors="ignore"
            ).splitlines()
            logs = "\n".join(lines[-50:]) or "No logs found."

        if len(logs) > 3500:
            logs = logs[-3500:]

        bot.send_message(
            chat_id,
            f"📄 <b>Logs — {safe_html(rec['orig_name'])}</b>\n\n"
            f"<pre>{safe_html(logs)}</pre>"
        )

    elif action == "delete":
        update_file(file_id, autostart=0)
        stop_host(file_id, manual=True)
        delete_file_record(file_id)
        bot.send_message(chat_id, "🗑 <b>Hosting record deleted.</b>")
        my_files(call.message)

# ---------------------------------------------------------
# Health server for Render Web Service
# ---------------------------------------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return jsonify({
        "service": "AY Telegram Hosting",
        "status": "online",
        "storage": "telegram",
        "storage_topic": STORAGE_THREAD_ID,
        "python": True,
        "nodejs": True,
        "zip": False
    })

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
def start_background():
    threading.Thread(target=supervisor, daemon=True).start()

def start_telegram():
    while True:
        try:
            logger.info("Telegram polling started")
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=50,
                skip_pending=True
            )
        except Exception:
            logger.exception("Telegram polling crashed")
            time.sleep(5)

if __name__ == "__main__":
    start_background()
    threading.Thread(target=start_telegram, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
