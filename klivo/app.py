"""Klivo (бывший «Радар»): CRM-напоминалка «кого не забыть сегодня».

Один бэкенд для двух интерфейсов: сайт klivo.ru и Telegram Mini App.
Аккаунт один — Telegram-id. Данные клиентов у каждого пользователя свои.

Запуск:  KLIVO_BOT_TOKEN=... KLIVO_BOT_USERNAME=... gunicorn -b 127.0.0.1:8080 app:app
Локально без Telegram:  KLIVO_DEV=1 python app.py   (кнопка «Войти как тестовый»)
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, abort, g, jsonify, redirect, request, send_from_directory, session

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("KLIVO_DATA", HERE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.environ.get("KLIVO_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("KLIVO_BOT_USERNAME", "")  # без @
DEV = os.environ.get("KLIVO_DEV") == "1"
MAX_CLIENTS = 5000
MAX_AGE = 24 * 3600  # initData / виджет не старше суток

app = Flask(__name__, static_folder=None)


def _secret_key():
    env = os.environ.get("KLIVO_SECRET")
    if env:
        return env
    f = DATA / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("KLIVO_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90,
    MAX_CONTENT_LENGTH=64 * 1024,
)


# ---------- БД ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DATA / "klivo.db")
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    c = sqlite3.connect(DATA / "klivo.db")
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,            -- Telegram id
      name TEXT NOT NULL DEFAULT '',
      username TEXT NOT NULL DEFAULT '',
      created INTEGER NOT NULL,
      last_seen INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS leads(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL, name TEXT NOT NULL, contact TEXT NOT NULL,
      message TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS clients(
      id TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      data TEXT NOT NULL,                -- JSON клиента
      updated INTEGER NOT NULL,
      PRIMARY KEY(user_id, id));
    """)
    have = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    for col, ddl in (("remind_on", "INTEGER NOT NULL DEFAULT 1"),
                     ("remind_hour", "INTEGER NOT NULL DEFAULT 9"),
                     ("tz", "INTEGER NOT NULL DEFAULT 180"),        # смещение от UTC в минутах (Москва)
                     ("bot_ok", "INTEGER NOT NULL DEFAULT 0"),      # бот может писать (пользователь нажал Start)
                     ("last_reminded", "TEXT NOT NULL DEFAULT ''")):
        if col not in have:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    c.commit()
    c.close()


init_db()


# ---------- Проверка подписи Telegram ----------
def _check_webapp(init_data: str):
    """Mini App: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    if not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > MAX_AGE:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


def _check_widget(args):
    """Login Widget на сайте: https://core.telegram.org/widgets/login#checking-authorization"""
    if not BOT_TOKEN:
        return None
    pairs = {k: v for k, v in args.items()}
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > MAX_AGE:
        return None
    return {"id": int(pairs["id"]), "first_name": pairs.get("first_name", ""),
            "last_name": pairs.get("last_name", ""), "username": pairs.get("username", "")}


def _login(tg):
    uid = int(tg["id"])
    name = (tg.get("first_name", "") + " " + tg.get("last_name", "")).strip()
    now = int(time.time())
    d = db()
    d.execute("""INSERT INTO users(id,name,username,created,last_seen) VALUES(?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username,
                 last_seen=excluded.last_seen""", (uid, name, tg.get("username", ""), now, now))
    d.commit()
    session.clear()
    session.permanent = True
    session["uid"] = uid


def need_user():
    uid = session.get("uid")
    if not uid:
        abort(401)
    return uid


# ---------- Авторизация ----------
@app.get("/api/config")
def config():
    return jsonify(bot=BOT_USERNAME, dev=DEV, configured=bool(BOT_TOKEN and BOT_USERNAME))


@app.post("/api/auth/webapp")
def auth_webapp():
    tg = _check_webapp((request.get_json(silent=True) or {}).get("initData", ""))
    if not tg:
        abort(403)
    _login(tg)
    return jsonify(ok=True)


@app.get("/auth/telegram")
def auth_widget():
    tg = _check_widget(request.args)
    if not tg:
        abort(403)
    _login(tg)
    return redirect("/app")


@app.post("/api/auth/dev")
def auth_dev():
    if not DEV:
        abort(404)
    _login({"id": 1, "first_name": "Тест"})
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    uid = need_user()
    u = db().execute("SELECT id,name,username FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        session.clear()
        abort(401)
    return jsonify(dict(u))


SETTINGS_COLS = "remind_on,remind_hour,tz,bot_ok"


@app.get("/api/settings")
def get_settings():
    uid = need_user()
    r = db().execute(f"SELECT {SETTINGS_COLS} FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        abort(401)
    return jsonify(remind_on=bool(r["remind_on"]), remind_hour=r["remind_hour"], tz=r["tz"],
                   bot_ok=bool(r["bot_ok"]), bot=BOT_USERNAME)


@app.put("/api/settings")
def put_settings():
    uid = need_user()
    d = request.get_json(silent=True) or {}
    sets, vals = [], []
    if "remind_on" in d:
        sets.append("remind_on=?"); vals.append(1 if d["remind_on"] else 0)
    if "remind_hour" in d:
        try:
            h = int(d["remind_hour"])
        except (TypeError, ValueError):
            abort(400)
        if not 0 <= h <= 23:
            abort(400)
        sets.append("remind_hour=?"); vals.append(h)
    if "tz" in d:
        try:
            tz = int(d["tz"])
        except (TypeError, ValueError):
            abort(400)
        if not -720 <= tz <= 840:
            abort(400)
        sets.append("tz=?"); vals.append(tz)
    if sets:
        db().execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
        db().commit()
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ---------- Клиенты ----------
ALLOWED = {"id", "nickname", "type", "lastVisitDate", "frequencyDays", "aftercareStage", "notes", "snoozeUntil",
           "check", "total", "visits"}


def _clean(cid, d):
    if not isinstance(d, dict):
        abort(400)
    out = {k: v for k, v in d.items() if k in ALLOWED}
    out["id"] = cid
    out["nickname"] = str(out.get("nickname", "")).strip()[:80]
    if not out["nickname"]:
        abort(400)
    out["type"] = "aftercare" if out.get("type") == "aftercare" else "return"
    out["notes"] = str(out.get("notes") or "")[:500]
    for k, hi in (("check", 10_000_000), ("total", 1_000_000_000), ("visits", 100_000)):
        try:
            out[k] = max(0, min(hi, int(float(out.get(k) or 0))))
        except (TypeError, ValueError):
            out[k] = 0
    for k in ("lastVisitDate", "snoozeUntil"):
        v = out.get(k)
        out[k] = v if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-" else None
    if not out["lastVisitDate"]:
        abort(400)
    try:
        out["frequencyDays"] = max(1, min(3650, int(out["frequencyDays"]))) if out["type"] == "return" else None
    except (TypeError, ValueError):
        out["frequencyDays"] = 30
    if out["type"] == "aftercare":
        try:
            out["aftercareStage"] = max(0, min(3, int(out.get("aftercareStage") or 0)))
        except (TypeError, ValueError):
            out["aftercareStage"] = 0
    else:
        out["aftercareStage"] = None
    return out


@app.get("/api/clients")
def list_clients():
    uid = need_user()
    rows = db().execute("SELECT data FROM clients WHERE user_id=? ORDER BY updated", (uid,)).fetchall()
    return jsonify([json.loads(r["data"]) for r in rows])


@app.put("/api/clients/<cid>")
def put_client(cid):
    uid = need_user()
    if not (1 <= len(cid) <= 40) or not all(ch.isalnum() or ch in "-_" for ch in cid):
        abort(400)
    data = _clean(cid, request.get_json(silent=True))
    d = db()
    exists = d.execute("SELECT 1 FROM clients WHERE user_id=? AND id=?", (uid, cid)).fetchone()
    if not exists and d.execute("SELECT COUNT(*) FROM clients WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_CLIENTS:
        abort(409)
    d.execute("INSERT INTO clients(id,user_id,data,updated) VALUES(?,?,?,?) "
              "ON CONFLICT(user_id,id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
              (cid, uid, json.dumps(data, ensure_ascii=False), int(time.time() * 1000)))
    d.commit()
    return jsonify(ok=True)


@app.delete("/api/clients/<cid>")
def del_client(cid):
    uid = need_user()
    db().execute("DELETE FROM clients WHERE user_id=? AND id=?", (uid, cid))
    db().commit()
    return jsonify(ok=True)


# ---------- Заявки с лендинга ----------
_last_lead = {}


@app.post("/api/lead")
def lead():
    d = request.get_json(silent=True) or {}
    kind = d.get("kind") if d.get("kind") in ("course", "turnkey", "other") else "other"
    name = str(d.get("name", "")).strip()[:80]
    contact = str(d.get("contact", "")).strip()[:120]
    message = str(d.get("message", "")).strip()[:1000]
    if not name or len(contact) < 3 or not d.get("consent"):
        abort(400)
    ip = request.headers.get("X-Real-IP") or request.remote_addr
    now = time.time()
    if now - _last_lead.get(ip, 0) < 20:  # не чаще раза в 20 секунд с одного адреса
        abort(429)
    _last_lead[ip] = now
    db().execute("INSERT INTO leads(kind,name,contact,message,created) VALUES(?,?,?,?,?)",
                 (kind, name, contact, message, int(now)))
    db().commit()
    return jsonify(ok=True)


# ---------- Страницы ----------
def _page(name):
    r = send_from_directory(HERE / "static", name)
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/")
def landing():
    return _page("landing.html")


@app.get("/app")
def app_page():
    return _page("app.html")


@app.errorhandler(401)
def _e401(_):
    return jsonify(error="auth"), 401


if __name__ == "__main__":
    app.run("127.0.0.1", int(os.environ.get("PORT", 8080)), debug=False)
