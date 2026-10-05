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
|--------|-------|
| `GET /api/health` | всегда — `storage_backend`, лимиты, `admin_enabled` |
| `POST /api/uploads` | local — multipart `files` (фото + видео) |
| `GET /api/files/{key}` | local — отдать файл (также превью `thumbs/…`, `posters/…`) |
| `POST /api/uploads/presign` | yandex — policy для браузера |
| `POST /api/uploads/confirm` | yandex — запись в SQLite + генерация превью |
| `GET /api/photos` | всегда — галерея (`limit`, `offset`, `kind=all\|image\|video`, `guest=Имя`, только видимые и одобренные) |
| `GET /api/photos/guests` | всегда — список имён гостей (для фильтра) |
| `GET /api/admin/photos` | админка — список (вкл. скрытые: `hidden=true`, на модерации: `pending=true`, по гостю: `guest=…`) |
| `PATCH /api/admin/photos/{id}` | админка — скрыть/показать (`{"hidden": bool}`) и одобрить/отклонить (`{"pending": bool}`) |
| `DELETE /api/admin/photos/{id}` | админка — удалить файл + превью + запись |
| `GET /api/admin/photos/archive.zip` | админка — zip всех опубликованных оригиналов |

- **Превью** генерируются сервером (Pillow, JPEG, `THUMBNAIL_SIZE` по умолчанию 400px)
  и отдаются в галерее вместо оригиналов — страница не скачивает все фото целиком.
  Полный файл открывается только по клику в лайтбоксе.
- **Первая страница** (`index.html`) просит гостя ввести имя; оно запоминается в
  cookie (`guest_name`, год) и подставляется к каждой загрузке, так что возвращающийся
  гость имя повторно не вводит. Имя видно в админке рядом с каждым файлом, а в
  галерее и админке можно отфильтровать фото **по человеку**, который их загрузил.
- **Видео** (mp4/webm/mov/m4v) имеют отдельный лимит `MAX_VIDEO_SIZE_MB` (500 по
  умолчанию; фото — `MAX_FILE_SIZE_MB`, 40); в галерее показываются без скачивания, проигрываются в лайтбоксе.
  Для видео сервер через ffmpeg генерирует **постер-кадр** (`VIDEO_POSTER=true`),
  чтобы плитка не была пустой.
- **Премодерация** (`PRE_MODERATION=true`): новые загрузки скрыты от гостей, пока
  админ не нажмёт «Одобрить» в админке (раздел «На модерации»).
- **Архив для пары**: кнопка «Скачать архив» в админке отдаёт zip всех
  опубликованных оригиналов (`/api/admin/photos/archive.zip`).
- **Лимит файлов** `MAX_FILES_PER_REQUEST=0` означает «без лимита за заход».
- **Админка** на `/admin.html`: требует `ADMIN_PASSWORD` (Bearer-токен), позволяет
  скрывать, удалять, одобрять/отклонять файлы и скачивать архив.

## Обслуживание (фоновое)

Фоновый процесс (`app/maintenance.py`) при запуске и далее по таймеру:

- **Версионирование бакета** (yandex) включается автоматически, чтобы удаление
  объектов можно было откатить.
- **Очистка «осиротевших» объектов** — файлы, у которых нет записи в БД (например,
  браузер получил signed URL на presign, но загрузку не завершил), старше
  `ORPHAN_MAX_AGE_HOURS` (по умолчанию 24) удаляются. Для yandex это эквивалент
  lifecycle-правила на префикс `uploads/`, но выполняется на стороне приложения.
- **Бэкап SQLite** — каждые `BACKUP_HOURS` часов копия БД пишется в
  `data/backups/photos-<ts>.db`, хранятся последние `BACKUP_KEEP` копий.

Для yandex можно также выставить lifecycle-правило на уровне бакета (сниппет в
`scripts/bucket-lifecycle.json`), если приложение не всегда запущено.

## Защита без авторизации

| Мера | Где |
|------|-----|
| jpeg/png/webp/heic + mp4/webm/mov/m4v | клиент + API (+ policy в yandex) |
| фото ≤ 15 МБ, видео ≤ 200 МБ | клиент + API |
| лимит файлов за заход (опционально) | клиент + API (`MAX_FILES_PER_REQUEST`, 0 = нет) |
| rate limit по IP (галерея — вне лимита) | API, реальный IP берётся справа из `X-Forwarded-For` по `TRUSTED_PROXIES` |
| EXIF/GPS удаляются из оригиналов (JPEG/HEIC) и display-версий | API (`STRIP_EXIF`) |
| скрытые фото не отдаются по прямой ссылке (local) | `serve_file` → 404 |
| админка защищена от перебора | отдельный rate limit (`ADMIN_PASSWORD`) |
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
