"""Тест бота на заглушке Telegram API:  python tests/test_bot.py  (из папки klivo)"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ.update(KLIVO_DATA=tmp, KLIVO_BOT_TOKEN="1:T", KLIVO_URL="https://klivo.ru")

SENT, QUEUE, FAIL = [], [], {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        method = self.path.rsplit("/", 1)[1]
        code, out = 200, {"ok": True, "result": True}
        if method == "getUpdates":
            out = {"ok": True, "result": [QUEUE.pop(0)] if QUEUE else []}
        elif method == "sendMessage":
            if body["chat_id"] in FAIL:
                code, out = 403, {"ok": False, "error_code": 403}
            else:
                SENT.append(body)
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(out).encode())


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["KLIVO_TG_API"] = f"http://127.0.0.1:{srv.server_port}"

import bot  # noqa: E402

bot.klivo.init_db()
c = sqlite3.connect(bot.DB_PATH)
c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)"); c.commit()

# 1. /start создаёт пользователя и включает отправку
QUEUE.append({"update_id": 10, "message": {"chat": {"id": 42, "type": "private"},
              "from": {"id": 42, "first_name": "Аня"}, "text": "/start"}})
bot.poll_once(c, timeout=0)
assert c.execute("SELECT bot_ok FROM users WHERE id=42").fetchone()[0] == 1
assert "Привет" in SENT[-1]["text"] and SENT[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].endswith("/app")
assert bot.get_offset(c) == 10
# /time 8 и /off
for t in ("/time 8", "/off", "/on"):
    QUEUE.append({"update_id": bot.get_offset(c) + 1, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": t}})
    bot.poll_once(c, timeout=0)
assert c.execute("SELECT remind_hour,remind_on FROM users WHERE id=42").fetchone() == (8, 1)

# 2. клиенты: просрочена, на сегодня, будущая, цикл ухода завершён
today = dt.date(2026, 11, 3)
def day(n): return (today - dt.timedelta(days=n)).isoformat()
cl = [
 {"id": "a", "nickname": "Аня <b>", "type": "return", "lastVisitDate": day(34), "frequencyDays": 30, "check": 3500},
 {"id": "b", "nickname": "Лена", "type": "return", "lastVisitDate": day(30), "frequencyDays": 30, "check": 2000},
 {"id": "c", "nickname": "Оля", "type": "return", "lastVisitDate": day(5), "frequencyDays": 30},
 {"id": "d", "nickname": "Ира", "type": "aftercare", "lastVisitDate": day(1), "aftercareStage": 3},
 {"id": "e", "nickname": "Вика", "type": "aftercare", "lastVisitDate": day(5), "aftercareStage": 1},
 {"id": "f", "nickname": "Зоя", "type": "return", "lastVisitDate": day(40), "frequencyDays": 30,
  "snoozeUntil": (today + dt.timedelta(days=3)).isoformat(), "check": 9999},
]
for x in cl:
    c.execute("INSERT INTO clients(id,user_id,data,updated) VALUES(?,?,?,0)", (x["id"], 42, json.dumps(x, ensure_ascii=False)))
c.commit()

SENT.clear()
# до часа рассылки (07:00 по Москве, 8:00 выставлено) ничего не уходит
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 4, 0)) == 0 and not SENT
# 08:30 по Москве (UTC+3) — уходит
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 5, 30)) == 1
txt = SENT[-1]["text"]
print(txt)
assert "Сегодня написать: <b>3</b>" in txt          # Аня, Лена, Вика (Зоя отложена, Оля рано, Ира завершена)
assert "Аня &lt;b&gt;" in txt                        # экранирование
assert "просрочено на 4 дн." in txt and "сегодня" in txt and "уход, этап 2/3" in txt
assert "Под угрозой: <b>3 500 ₽</b>" in txt  # только просроченные, Зоя не считается
assert "Зоя" not in txt and "Оля" not in txt and "Ира" not in txt
# повторно в тот же день не шлём
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 6, 30)) == 0
# на следующий день снова
assert bot.reminders_cycle(dt.datetime(2026, 11, 4, 5, 30)) == 1
# после 22:00 по местному не наверстываем
c.execute("UPDATE users SET last_reminded=''"); c.commit()
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 20, 30)) == 0
# выключено — молчим
c.execute("UPDATE users SET remind_on=0"); c.commit()
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 5, 30)) == 0
c.execute("UPDATE users SET remind_on=1"); c.commit()

# 3. заблокировал бота (403) — bot_ok сбрасывается, last_reminded не ставится
FAIL[42] = 1
assert bot.reminders_cycle(dt.datetime(2026, 11, 3, 5, 30)) == 0
assert c.execute("SELECT bot_ok,last_reminded FROM users WHERE id=42").fetchone() == (0, "")
print("OK")
