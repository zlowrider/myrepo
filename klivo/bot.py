"""Telegram-бот Klivo: утренние напоминания «кому написать сегодня».

Один процесс: принимает команды (long polling) и раз в минуту проверяет,
кому пора отправить утреннее сообщение. Запуск:  python bot.py
Разовая проверка (для cron / тестов):  python bot.py --once

Переменные: KLIVO_BOT_TOKEN, KLIVO_URL (по умолчанию https://klivo.ru), KLIVO_DATA,
KLIVO_TG_API (по умолчанию https://api.telegram.org; нужен в тестах).

Бот может написать пользователю только после того, как тот нажал Start в чате с ботом.
"""
import datetime as dt
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import app as klivo  # даёт init_db() с миграциями и путь к данным

TOKEN = os.environ.get("KLIVO_BOT_TOKEN", "")
SITE = os.environ.get("KLIVO_URL", "https://klivo.ru").rstrip("/")
API = os.environ.get("KLIVO_TG_API", "https://api.telegram.org").rstrip("/")
DB_PATH = klivo.DATA / "klivo.db"
AFTERCARE_STAGES = (1, 5, 10)
MAX_LINES = 15


# ---------- Telegram API ----------
def tg(method, _http=35, **params):
    req = urllib.request.Request(f"{API}/bot{TOKEN}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_http) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            return {"ok": False, "error_code": e.code}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error_code": 0, "description": str(e)}


def open_button():
    return {"inline_keyboard": [[{"text": "Открыть Klivo", "web_app": {"url": SITE + "/app"}}]]}


def send(chat_id, text):
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
              disable_web_page_preview=True, reply_markup=open_button())


# ---------- Логика «пора ли писать» (повторяет getDueInfo из app.html) ----------
def due_diff(c, today):
    """Сколько дней до срока: <0 просрочено, 0 сегодня. None — цикл завершён / данных нет."""
    try:
        last = dt.date.fromisoformat(c["lastVisitDate"])
        if c.get("type") == "aftercare":
            stage = int(c.get("aftercareStage") or 0)
            if stage >= 3:
                return None
            due = last + dt.timedelta(days=AFTERCARE_STAGES[stage])
        else:
            due = last + dt.timedelta(days=int(c.get("frequencyDays") or 30))
            sn = c.get("snoozeUntil")
            if sn:
                sn = dt.date.fromisoformat(sn)
                if sn >= today and sn > due:
                    due = sn
        return (due - today).days
    except (KeyError, ValueError, TypeError):
        return None


def money(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def build_message(clients, today):
    items = []
    for c in clients:
        d = due_diff(c, today)
        if d is not None and d <= 0:
            items.append((d, c))
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    risk = sum(int(c.get("check") or 0) for d, c in items if d < 0)
    lines = [f"☀️ <b>Доброе утро!</b> Сегодня написать: <b>{len(items)}</b>\n"]
    for d, c in items[:MAX_LINES]:
        if c.get("type") == "aftercare":
            what = f"уход, этап {min(int(c.get('aftercareStage') or 0) + 1, 3)}/3"
        elif d < 0:
            what = f"просрочено на {-d} дн."
        else:
            what = "сегодня"
        chk = f" · {money(c['check'])}" if c.get("check") else ""
        lines.append(f"• <b>{html.escape(str(c.get('nickname', '?')))}</b>: {what}{chk}")
    if len(items) > MAX_LINES:
        lines.append(f"…и ещё {len(items) - MAX_LINES}")
    if risk:
        lines.append(f"\n💸 Под угрозой: <b>{money(risk)}</b>")
    lines.append("\nГотовый текст для каждой клиентки уже в приложении.")
    return "\n".join(lines)


# ---------- Рассылка ----------
def reminders_cycle(now_utc=None):
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    sent = 0
    try:
        users = c.execute("SELECT id,tz,remind_hour,last_reminded FROM users "
                          "WHERE remind_on=1 AND bot_ok=1").fetchall()
        for u in users:
            local = now_utc + dt.timedelta(minutes=u["tz"])
            today = local.date()
            # окно: с нужного часа и до 22:00, чтобы наверстать, если бот был выключен
            if not (u["remind_hour"] <= local.hour < 22) or u["last_reminded"] == today.isoformat():
                continue
            rows = c.execute("SELECT data FROM clients WHERE user_id=?", (u["id"],)).fetchall()
            text = build_message([json.loads(r["data"]) for r in rows], today)
            if text:
                res = send(u["id"], text)
                if not res.get("ok"):
                    code = res.get("error_code")
                    if code in (400, 403):      # бот заблокирован или чат недоступен
                        c.execute("UPDATE users SET bot_ok=0 WHERE id=?", (u["id"],))
                    elif code in (0, 429, 500, 502, 503):
                        continue                # временная ошибка: попробуем в следующую минуту
                    c.commit()
                    continue
                sent += 1
            c.execute("UPDATE users SET last_reminded=? WHERE id=?", (today.isoformat(), u["id"]))
            c.commit()
    finally:
        c.close()
    return sent


# ---------- Команды ----------
HELP = ("Klivo напоминает, кому из клиенток пора написать.\n\n"
        "/on — включить утренние напоминания\n"
        "/off — выключить\n"
        "/time 9 — присылать в 9 утра (час от 0 до 23)\n"
        "/help — эта подсказка")


def upsert_user(c, tgu, bot_ok=None):
    now = int(time.time())
    name = (tgu.get("first_name", "") + " " + tgu.get("last_name", "")).strip()
    c.execute("""INSERT INTO users(id,name,username,created,last_seen) VALUES(?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
              (tgu["id"], name, tgu.get("username", ""), now, now))
    if bot_ok is not None:
        c.execute("UPDATE users SET bot_ok=? WHERE id=?", (bot_ok, tgu["id"]))


def handle_message(c, m):
    chat = m.get("chat", {})
    tgu = m.get("from") or {}
    if chat.get("type") != "private" or not tgu.get("id"):
        return
    text = (m.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if not cmd.startswith("/"):
        return
    upsert_user(c, tgu, bot_ok=1)
    if cmd == "/start":
        send(chat["id"], "Привет! Я пришлю утром список клиенток, которым пора написать. "
                         "Добавьте клиенток в приложении, остальное сделаю я.\n\n" + HELP)
    elif cmd == "/on":
        c.execute("UPDATE users SET remind_on=1 WHERE id=?", (tgu["id"],))
        send(chat["id"], "Напоминания включены.")
    elif cmd == "/off":
        c.execute("UPDATE users SET remind_on=0 WHERE id=?", (tgu["id"],))
        send(chat["id"], "Напоминания выключены. Включить обратно: /on")
    elif cmd == "/time":
        try:
            h = int(arg.strip())
            assert 0 <= h <= 23
        except (ValueError, AssertionError):
            send(chat["id"], "Напишите час числом, например: /time 9")
        else:
            c.execute("UPDATE users SET remind_hour=? WHERE id=?", (h, tgu["id"]))
            send(chat["id"], f"Буду писать в {h}:00 по вашему времени.")
    else:
        send(chat["id"], HELP)


def handle_update(c, u):
    if "message" in u:
        handle_message(c, u["message"])
    elif "my_chat_member" in u:
        mcm = u["my_chat_member"]
        if mcm.get("chat", {}).get("type") == "private":
            ok = 0 if mcm.get("new_chat_member", {}).get("status") in ("kicked", "left") else 1
            c.execute("UPDATE users SET bot_ok=? WHERE id=?", (ok, mcm["from"]["id"]))


def get_offset(c):
    r = c.execute("SELECT v FROM kv WHERE k='tg_offset'").fetchone()
    return int(r[0]) if r else 0


def set_offset(c, v):
    c.execute("INSERT INTO kv(k,v) VALUES('tg_offset',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(v),))


def setup_bot():
    tg("deleteWebhook")
    tg("setMyCommands", commands=[
        {"command": "on", "description": "Включить утренние напоминания"},
        {"command": "off", "description": "Выключить напоминания"},
        {"command": "time", "description": "Час рассылки, например /time 9"},
        {"command": "help", "description": "Справка"}])
    tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Открыть", "web_app": {"url": SITE + "/app"}})


def poll_once(c, timeout=25):
    res = tg("getUpdates", _http=timeout + 10, offset=get_offset(c) + 1, timeout=timeout,
             allowed_updates=["message", "my_chat_member"])
    if not res.get("ok"):
        time.sleep(5)
        return
    for u in res.get("result", []):
        try:
            handle_update(c, u)
        except Exception as e:  # noqa: BLE001  одно кривое сообщение не должно ронять бота
            print("update error:", e, file=sys.stderr)
        set_offset(c, u["update_id"])
        c.commit()


def main():
    if not TOKEN:
        sys.exit("KLIVO_BOT_TOKEN не задан")
    klivo.init_db()
    if "--once" in sys.argv:
        print("отправлено:", reminders_cycle())
        return
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    c.commit()
    setup_bot()
    last = 0
    print("Klivo bot запущен", flush=True)
    while True:
        poll_once(c)
        if time.time() - last >= 60:
            last = time.time()
            try:
                reminders_cycle()
            except Exception as e:  # noqa: BLE001
                print("reminders error:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
