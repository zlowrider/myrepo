# Klivo (бывший «Радар»)

CRM-напоминалка «кого не забыть сегодня»: клиент, дата визита, через сколько вернуться,
готовый текст сообщения. Один бэкенд, два интерфейса: сайт klivo.ru и Telegram Mini App.
Аккаунт общий (Telegram-id), данные подтягиваются на обоих.

## Запуск локально
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    KLIVO_DEV=1 .venv/bin/python app.py      # http://127.0.0.1:8080, кнопка «Войти как тестовый»

## Продакшен (сервер в РФ)
Переменные окружения:
- `KLIVO_BOT_TOKEN` — токен бота из @BotFather
- `KLIVO_BOT_USERNAME` — username бота без @
- `KLIVO_HTTPS=1` — когда сайт на https (secure-cookie)
- `KLIVO_DATA` — папка для базы (по умолчанию `./data`)

    gunicorn -w 2 -b 127.0.0.1:8080 app:app
nginx проксирует klivo.ru на 127.0.0.1:8080, сертификат через certbot.

## Настройка бота в @BotFather
1. `/setdomain` → выбрать бота → `klivo.ru` (нужно для кнопки входа на сайте).
2. Menu Button (`/mybots` → Bot Settings → Menu Button) → URL `https://klivo.ru/app`.
   Мини-апп открывает ту же страницу и входит автоматически по подписи Telegram.

## Страницы
- `/` — лендинг (`static/landing.html`), `/app` — приложение, заявки с лендинга пишутся в таблицу `leads` (sqlite).
- Файлы для сервера: `deploy/` (systemd, nginx, пример .env).

## Устройство
- `app.py` — Flask + SQLite; проверка подписи initData (Mini App) и Login Widget; API `/api/clients`.
- `static/app.html` — интерфейс (из прототипа), без сборки.
- Клиенты хранятся JSON-ом на пользователя; изменения уходят на сервер по одному клиенту.

## Что дальше
Напоминания в Telegram (бот пишет утром «сегодня писать: …»), экспорт, оплата, лендинг для klivo.ru.
