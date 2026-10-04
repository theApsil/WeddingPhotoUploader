# Система загрузки фото (свадьба)

Публичная страница для гостей **без авторизации**. Файлы можно класть
**на локальный диск** сервера (`STORAGE_BACKEND=local`, по умолчанию) или
отправлять **напрямую в Yandex Object Storage** (`STORAGE_BACKEND=yandex`).

Дизайн совпадает с приглашением: Данил & Полина, 17 октября 2026
(цвета emerald/gold, шрифты Old Classic / Jost / Cormorant — локально, без CDN).

## Архитектура

### local (по умолчанию)

```
Браузер ──multipart POST /api/uploads──► FastAPI ──► STORAGE_DIR/uploads/…
FastAPI ──SQLite──► метаданные
Галерея ──GET /api/photos──► /api/files/uploads/…
```

### yandex

```
Браузер ──JSON──► FastAPI ──presigned POST──► браузер
Браузер ──multipart POST──► Yandex Object Storage (напрямую)
Браузер ──confirm──► FastAPI (SQLite)
Галерея ──GET /api/photos──► signed GET (бакет приватный) или публичный URL
```

Ключи объектов: `uploads/YYYY-MM-DD/<uuid>.ext` (оригинальное имя не используется).

Без ключей Яндекса проект **полностью** работает на `local`.

## Быстрый старт (локально)

```bash
cp .env.example .env
# CORS_ORIGINS=http://localhost:8080
# STORAGE_BACKEND=local

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

STORAGE_DIR=../data DATABASE_PATH=../data/photos.db \
  uvicorn app.main:app --reload --port 8000
```

Открой `http://127.0.0.1:8000/` (статика отдаётся самим API).

## Деплой на VPS (рекомендуется)

Интерактивный установщик + пошаговая инструкция по домену и HTTPS:

```bash
chmod +x install.sh
sudo ./install.sh
```

- [`install.sh`](install.sh) — зависимости, `.env`, systemd, nginx, certbot, health-check
- [`DEPLOY.md`](DEPLOY.md) — DNS (A-запись), HTTPS, Yandex, бэкап, типичные ошибки

Неинтерактивно: `sudo WP_SITE_DOMAIN=… WP_ENABLE_HTTPS=yes ./install.sh --non-interactive`  
Сухой прогон: `./install.sh --dry-run`

### Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

Сайт: `http://<сервер>/`, API: `/api/health`. Файлы — в томе `photo_data`.

## Деплой на этом сервере — `/wedding`

Живёт на **https://adybov.pro/wedding** (Caddy → `127.0.0.1:8200`).

| Что | Где |
|-----|-----|
| systemd | `deploy/photo-upload-wedding.service` → `/etc/systemd/system/` |
| `.env` | `STORAGE_BACKEND=local`, `BASE_PATH=/wedding`, `STORAGE_DIR=…/photo-upload/data` |
| Файлы | `photo-upload/data/uploads/YYYY-MM-DD/` |
| Пульс | `/pulse`, `/pulse-demo` не затрагиваются |

```bash
systemctl restart photo-upload-wedding
curl -sS https://adybov.pro/wedding/api/health
```

## Переключение на Yandex Object Storage

1. В Object Storage (`ru-central1`) создай бакет.
2. Сервисный аккаунт: роли `storage.uploader` + `storage.viewer` на бакет.
3. Статический ключ → в `.env`:
   `YANDEX_ACCESS_KEY_ID`, `YANDEX_SECRET_ACCESS_KEY`, `S3_BUCKET`.
4. CORS бакета — только `https://SITE_DOMAIN`
   (шаблон [`scripts/bucket-cors.json`](scripts/bucket-cors.json)).
5. В `.env`: `STORAGE_BACKEND=yandex`, заполни ключи, `SITE_DOMAIN` / `CORS_ORIGINS`.
6. Перезапусти сервис / compose.
7. По желанию: lifecycle на префикс `uploads/`, `STORAGE_DOMAIN` + CNAME.

`S3_MOCK=true` — только для разработки API без облака (реальный POST из браузера
не сработает).

Вернуться на диск: `STORAGE_BACKEND=local` и перезапуск.

## Как забрать все фото / бэкап

### local

```bash
# Архив загрузок
tar -czf wedding-photos-$(date +%F).tar.gz -C /path/to/photo-upload/data uploads

# Вместе с метаданными галереи
tar -czf wedding-backup-$(date +%F).tar.gz -C /path/to/photo-upload data
```

На этом сервере каталог: `/root/pulse_demo_data/files/photo-upload/data/`.

### yandex

```bash
aws --endpoint-url=https://storage.yandexcloud.net \
  s3 sync "s3://$S3_BUCKET/uploads/" ./wedding-photos-backup/
```

Плюс скопируй `photos.db` (список для галереи).

## Как поменять тексты (имена, дата)

- Фронт: [`frontend/js/common.js`](frontend/js/common.js) — объект `SITE`
  (`groom`, `bride`, `dateLabel`, `dateShort`, `tagline`).
- Заголовки страниц: `frontend/index.html`, `frontend/gallery.html` (`<title>`).
- После правки при деплое на `/wedding` достаточно обновить файлы и
  `systemctl restart photo-upload-wedding` (статика читается с диска).

## Как обновлять код

1. Внеси правки в каталог проекта (или `git pull`).
2. `cd backend && source .venv/bin/activate && pip install -r requirements.txt && pytest -q`
3. `systemctl restart photo-upload-wedding` (или `docker compose up -d --build`).
4. Проверь `https://adybov.pro/wedding/api/health` и тестовую загрузку.

Памятка для гостей (QR): [`docs/guest-guide.md`](docs/guest-guide.md).

## API

| Метод | Когда |
|-------|--------|
| `GET /api/health` | всегда — `storage_backend`, лимиты |
| `POST /api/uploads` | local — multipart `files` |
| `GET /api/files/{key}` | local — отдать файл |
| `POST /api/uploads/presign` | yandex — policy для браузера |
| `POST /api/uploads/confirm` | yandex — запись в SQLite |
| `GET /api/photos` | всегда — галерея |

## Защита без авторизации

| Мера | Где |
|------|-----|
| jpeg/png/webp/heic | клиент + API (+ policy в yandex) |
| размер ≤ 15 МБ | клиент + API |
| лимит файлов за заход | клиент + API |
| rate limit по IP | API (`X-Forwarded-For`) |
| случайные ключи | API |
| path traversal | local resolve под `STORAGE_DIR` |
| CORS бакета | только домен сайта (yandex) |

## Структура

```
photo-upload/
  install.sh        интерактивный установщик на VPS
  DEPLOY.md         DNS, HTTPS, Yandex, бэкап
  backend/          FastAPI + тесты (local и yandex/moto)
  frontend/         mobile-first UI + локальные шрифты
  docs/             памятка гостям
  data/             SQLite + uploads/ (local)
  deploy/           systemd / nginx-шаблоны для install.sh
  nginx/            конфиги для compose
  scripts/          CORS бакета
  docker-compose.yml
  .env.example
```

## Переменные окружения

См. [`.env.example`](.env.example). Главные: `STORAGE_BACKEND`, `STORAGE_DIR`,
ключи Яндекса (опционально), `SITE_DOMAIN` / `CORS_ORIGINS`, `BASE_PATH`, лимиты.
