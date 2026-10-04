# Развёртывание Wedding Photo Uploader

Пошаговая инструкция: VPS + домен + HTTPS. Хранилище по умолчанию —
локальный диск; Yandex Object Storage подключается опционально.

## Что нужно

- VPS: Ubuntu 22.04/24.04 или Debian 12, доступ root/sudo
- Домен (или поддомен), DNS под вашим контролем
- Открытые порты **80** и **443**
- Архив проекта или git-клон с `install.sh` в корне

## 1. DNS

У регистратора создайте **A-запись**:

| Тип | Имя | Значение |
|-----|-----|----------|
| A | `@` или `photos` | публичный IP VPS |

Пример: `photos.example.com` → `203.0.113.10`.

Проверка (с вашего компьютера):

```bash
dig +short photos.example.com A
# должен вернуть IP сервера
```

Подождите распространения DNS (иногда 5–60 минут), иначе Let's Encrypt
не выпустит сертификат.

Опционально для публичного бакета Яндекса: **CNAME**
`cdn.example.com` → `<bucket>.storage.yandexcloud.net`.

## 2. Распаковка

```bash
sudo mkdir -p /opt/wedding-photo-uploader
sudo unzip "2026-10-04 WeddingPhotoUploader.zip" -d /opt/wedding-photo-uploader
cd /opt/wedding-photo-uploader
# если архив распаковался во вложенную папку — перейдите в неё
ls install.sh README.md DEPLOY.md
sudo chmod +x install.sh
```

## 3. Установка (`install.sh`)

Интерактивно:

```bash
sudo ./install.sh
```

Скрипт спросит:

1. **Домен** — например `photos.example.com`
2. **Путь** — по умолчанию `/wedding` (сайт будет
   `https://photos.example.com/wedding/`); пустая строка = корень сайта
3. **Хранилище** — `local` (диск) или `yandex` (Object Storage)
4. При `yandex` — бакет, регион, ключи доступа
5. Лимиты загрузки и порт uvicorn (по умолчанию `8200`)
6. HTTPS через Let's Encrypt (email)

Что делает установщик:

- ставит `python3`, `venv`, `nginx`, при необходимости `certbot`
- создаёт venv и ставит зависимости из `backend/requirements.txt`
- пишет `.env` с правами `600` (старый копирует в `.env.bak.*`)
- ставит systemd-юнит `photo-upload`
- генерирует nginx-конфиг под домен и путь
- по желанию выпускает сертификат
- проверяет `http://127.0.0.1:8200<путь>/api/health`

Повторный запуск **идемпотентен**: данные в `data/` не удаляются.

### Неинтерактивный режим

```bash
sudo WP_SITE_DOMAIN=photos.example.com \
  WP_BASE_PATH=/wedding \
  WP_STORAGE_BACKEND=local \
  WP_LETSENCRYPT_EMAIL=admin@example.com \
  WP_ENABLE_HTTPS=yes \
  ./install.sh --non-interactive
```

Сухой прогон (ничего не меняет на сервере):

```bash
./install.sh --dry-run
# или
sudo WP_SITE_DOMAIN=photos.example.com WP_ENABLE_HTTPS=no \
  ./install.sh --non-interactive --dry-run
```

## 4. Проверка

```bash
systemctl status photo-upload
curl -sS http://127.0.0.1:8200/wedding/api/health
curl -sS https://photos.example.com/wedding/api/health
```

Откройте в телефоне `https://<домен>/wedding/` — загрузка и галерея.

## 5. HTTPS вручную (если отказались в install.sh)

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d photos.example.com \
  --email admin@example.com --agree-tos --no-eff-email
sudo systemctl reload nginx
```

Продление обычно уже в cron/timer от certbot (`certbot renew`).

## 6. Yandex Object Storage (опционально)

1. Бакет в Object Storage, регион `ru-central1`.
2. Сервисный аккаунт: роли `storage.uploader` + `storage.viewer` на бакет.
3. Статический ключ доступа.
4. CORS бакета — только ваш сайт. Шаблон:
   [`scripts/bucket-cors.json`](scripts/bucket-cors.json).
   Подставьте `https://photos.example.com` вместо плейсхолдера.
5. В `.env`:

```bash
STORAGE_BACKEND=yandex
YANDEX_ACCESS_KEY_ID=...
YANDEX_SECRET_ACCESS_KEY=...
S3_BUCKET=...
SITE_DOMAIN=photos.example.com
CORS_ORIGINS=https://photos.example.com
```

6. `sudo systemctl restart photo-upload`

Вернуться на диск: `STORAGE_BACKEND=local` и снова restart.

`PUBLIC_READ=false` (по умолчанию) — бакет приватный, галерея через
signed GET. Публичка и `STORAGE_DOMAIN` — только если настроите сами.

> **О подписанных ссылках.** Signed GET выдаётся на время `PRESIGN_EXPIRES_SECONDS`
> (по умолчанию 3600 с). Отозвать уже выданную подписанную ссылку нельзя — S3 не
> держит список выданных ссылок. Поэтому «скрыть» в админке на yandex **не** убирает
> уже открытую вкладку, пока ссылка не истечёт. Чтобы прятать фото мгновенно,
> отдавайте файлы через приложение (прокси) вместо прямого signed URL. Для `local`
> скрытие работает сразу (`/api/files/...` проверяет `hidden` при отдаче).

## 7. Обновление кода

```bash
cd /opt/wedding-photo-uploader
# скопируйте новые файлы поверх или git pull
sudo backend/.venv/bin/pip install -r backend/requirements.txt
sudo backend/.venv/bin/pytest -q   # из каталога backend/
sudo systemctl restart photo-upload
curl -sS https://photos.example.com/wedding/api/health
```

Либо снова `sudo ./install.sh` — `.env` сохранится в `.bak`, данные не тронет.

Новые настройки (необязательно): `PRE_MODERATION=true` для премодерации загрузок,
`BACKUP_HOURS` для авто-бэкапа SQLite, `VIDEO_POSTER=true` (по умолчанию) для
постеров видео через ffmpeg.

## 8. Бэкап

### local

```bash
sudo tar -czf wedding-backup-$(date +%F).tar.gz \
  -C /opt/wedding-photo-uploader data
```

Авто-бэкап SQLite: задайте `BACKUP_HOURS=24` — приложение само пишет копии в
`data/backups/` и держит последние `BACKUP_KEEP`.

### yandex

```bash
aws --endpoint-url=https://storage.yandexcloud.net \
  s3 sync "s3://$S3_BUCKET/uploads/" ./wedding-photos-backup/
# плюс файл data/photos.db (метаданные галереи)
```

Версионирование бакета приложение включает автоматически. Очистку не-подтверждённых
объектов (браузер запросил presign, но не загрузил) приложение делает само
(`ORPHAN_MAX_AGE_HOURS`). Если приложение не всегда запущено, продублируйте это
lifecycle-правилом на бакете:

```bash
aws --endpoint-url=https://storage.yandexcloud.net \
  s3api put-bucket-lifecycle-configuration \
  --bucket "$S3_BUCKET" \
  --lifecycle-configuration file://scripts/bucket-lifecycle.json
```

## 9. Docker Compose (альтернатива)

Если предпочитаете контейнеры вместо systemd+nginx на хосте:

```bash
cp .env.example .env
# SITE_DOMAIN, LETSENCRYPT_EMAIL, STORAGE_BACKEND=local
docker compose up -d --build
```

Сайт на порту 80/443 хоста. Подробности — в README.

## Частые проблемы

| Симптом | Что проверить |
|---------|----------------|
| 502 Bad Gateway | `systemctl status photo-upload`, порт в `.env`/nginx совпадает |
| Health 404 | `BASE_PATH` в `.env` и путь в URL (`/wedding/api/health`) |
| CORS / загрузка в Yandex падает | CORS бакета = точный origin `https://домен` |
| certbot failed | `dig` на домен, порты 80/443, нет другого веб-сервера на 80 |
| Не грузятся фото > лимита | `MAX_FILE_SIZE_MB` и `client_max_body_size` в nginx |
| Пустая галерея после смены storage | метаданные в SQLite общие; сами файлы — только в выбранном backend |

Логи:

```bash
journalctl -u photo-upload -n 100 --no-pager
sudo nginx -t && sudo tail -n 50 /var/log/nginx/error.log
```

## Памятка гостям

Короткая инструкция для QR: [`docs/guest-guide.md`](docs/guest-guide.md).
