#!/usr/bin/env bash
# Interactive installer for Wedding Photo Uploader (local disk or Yandex Object Storage).
# Safe defaults: does not delete existing data/ or .env secrets on re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"

NON_INTERACTIVE=0
DRY_RUN=0
SKIP_HTTPS=0
SKIP_PACKAGES=0

# Defaults (overridable by env in --non-interactive mode)
SITE_DOMAIN="${WP_SITE_DOMAIN:-}"
BASE_PATH="${WP_BASE_PATH:-/wedding}"
STORAGE_BACKEND="${WP_STORAGE_BACKEND:-local}"
APP_PORT="${WP_APP_PORT:-8200}"
SERVICE_NAME="${WP_SERVICE_NAME:-photo-upload}"
LETSENCRYPT_EMAIL="${WP_LETSENCRYPT_EMAIL:-}"
ENABLE_HTTPS="${WP_ENABLE_HTTPS:-ask}"
MAX_FILE_SIZE_MB="${WP_MAX_FILE_SIZE_MB:-40}"
MAX_VIDEO_SIZE_MB="${WP_MAX_VIDEO_SIZE_MB:-500}"
MAX_FILES_PER_REQUEST="${WP_MAX_FILES_PER_REQUEST:-10}"
RATE_LIMIT_PER_MINUTE="${WP_RATE_LIMIT_PER_MINUTE:-20}"
YANDEX_ACCESS_KEY_ID="${WP_YANDEX_ACCESS_KEY_ID:-}"
YANDEX_SECRET_ACCESS_KEY="${WP_YANDEX_SECRET_ACCESS_KEY:-}"
S3_BUCKET="${WP_S3_BUCKET:-}"
S3_REGION="${WP_S3_REGION:-ru-central1}"
S3_ENDPOINT="${WP_S3_ENDPOINT:-https://storage.yandexcloud.net}"
STORAGE_DOMAIN="${WP_STORAGE_DOMAIN:-}"
PUBLIC_READ="${WP_PUBLIC_READ:-false}"
CORS_ORIGINS="${WP_CORS_ORIGINS:-}"

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

Options:
  --non-interactive   Use WP_* environment variables (no prompts)
  --dry-run           Print plan only; do not change the system
  --skip-https        Do not run certbot
  --skip-packages     Do not apt/yum install dependencies
  -h, --help          Show this help

Non-interactive variables (examples):
  WP_SITE_DOMAIN=photos.example.com
  WP_BASE_PATH=/wedding
  WP_STORAGE_BACKEND=local|yandex
  WP_LETSENCRYPT_EMAIL=admin@example.com
  WP_ENABLE_HTTPS=yes|no
  WP_APP_PORT=8200
  WP_MAX_FILE_SIZE_MB=40
  WP_MAX_VIDEO_SIZE_MB=500
  WP_YANDEX_ACCESS_KEY_ID=...
  WP_YANDEX_SECRET_ACCESS_KEY=...
  WP_S3_BUCKET=...
  WP_S3_REGION=ru-central1

Dry-run example:
  ./install.sh --dry-run
  sudo WP_SITE_DOMAIN=example.com WP_ENABLE_HTTPS=no ./install.sh --non-interactive --dry-run
EOF
}

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

write_file() {
  # write_file PATH MODE content_via_stdin
  local path="$1"
  local mode="$2"
  local tmp
  tmp="$(mktemp)"
  cat >"${tmp}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: would write ${path} (mode ${mode}), $(wc -c <"${tmp}") bytes"
    rm -f "${tmp}"
    return 0
  fi
  mkdir -p "$(dirname "${path}")"
  install -m "${mode}" "${tmp}" "${path}"
  rm -f "${tmp}"
}

prompt() {
  # prompt VAR "Question" "default"
  local var="$1"
  local question="$2"
  local default="${3:-}"
  local current="${!var:-}"
  local answer
  if [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
    if [[ -z "${current}" && -n "${default}" ]]; then
      printf -v "${var}" '%s' "${default}"
    fi
    return 0
  fi
  if [[ -n "${default}" ]]; then
    read -r -p "${question} [${default}]: " answer || true
    answer="${answer:-${default}}"
  else
    read -r -p "${question}: " answer || true
  fi
  printf -v "${var}" '%s' "${answer}"
}

prompt_secret() {
  local var="$1"
  local question="$2"
  local answer
  if [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
    return 0
  fi
  read -r -s -p "${question}: " answer || true
  printf '\n'
  printf -v "${var}" '%s' "${answer}"
}

normalize_base_path() {
  local p="$1"
  p="${p// /}"
  if [[ -z "${p}" || "${p}" == "/" ]]; then
    printf ''
    return 0
  fi
  [[ "${p}" == /* ]] || p="/${p}"
  p="${p%/}"
  printf '%s' "${p}"
}

need_root() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: skip root check"
    return 0
  fi
  if [[ "$(id -u)" -ne 0 ]]; then
    die "Запусти от root или через sudo: sudo ./install.sh"
  fi
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
  else
    OS_ID="unknown"
    OS_LIKE=""
  fi
  case "${OS_ID}" in
    ubuntu|debian) PKG_MGR="apt" ;;
    *)
      case " ${OS_LIKE} " in
        *"debian"*|*"ubuntu"*) PKG_MGR="apt" ;;
        *) die "Поддерживаются Debian/Ubuntu. Обнаружено: ${OS_ID}" ;;
      esac
      ;;
  esac
  log "ОС: ${OS_ID} (пакетный менеджер: ${PKG_MGR})"
}

install_packages() {
  if [[ "${SKIP_PACKAGES}" -eq 1 ]]; then
    log "Пропуск установки пакетов (--skip-packages)"
    return 0
  fi
  case "${PKG_MGR}" in
    apt)
      run apt-get update -y
      run DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-venv python3-pip nginx curl ca-certificates ffmpeg
      if [[ "${ENABLE_HTTPS}" == "yes" ]]; then
        run DEBIAN_FRONTEND=noninteractive apt-get install -y certbot
      fi
      ;;
  esac
}

ensure_project_layout() {
  [[ -d "${PROJECT_ROOT}/backend/app" ]] || die "Не найден backend/ в ${PROJECT_ROOT}"
  [[ -d "${PROJECT_ROOT}/frontend" ]] || die "Не найден frontend/ в ${PROJECT_ROOT}"
  [[ -f "${PROJECT_ROOT}/.env.example" ]] || die "Не найден .env.example"
  run mkdir -p "${PROJECT_ROOT}/data/uploads"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    chmod 755 "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/data/uploads"
  fi
}

setup_venv() {
  local venv="${PROJECT_ROOT}/backend/.venv"
  if [[ ! -d "${venv}" ]]; then
    run python3 -m venv "${venv}"
  else
    log "venv уже есть — переиспользую"
  fi
  run "${venv}/bin/pip" install --upgrade pip
  run "${venv}/bin/pip" install -r "${PROJECT_ROOT}/backend/requirements.txt"
}

write_env_file() {
  local env_path="${PROJECT_ROOT}/.env"
  local cors="${CORS_ORIGINS}"
  local scheme="https"
  if [[ -z "${cors}" && -n "${SITE_DOMAIN}" ]]; then
    if [[ "${ENABLE_HTTPS}" == "yes" ]]; then
      cors="https://${SITE_DOMAIN}"
    else
      cors="https://${SITE_DOMAIN},http://${SITE_DOMAIN}"
      scheme="http"
    fi
  fi

  if [[ -f "${env_path}" && "${DRY_RUN}" -eq 0 ]]; then
    local bak="${env_path}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "${env_path}" "${bak}"
    log "Старый .env сохранён как ${bak}"
  fi

  local yandex_block=""
  if [[ "${STORAGE_BACKEND}" == "yandex" ]]; then
    yandex_block=$(cat <<EOF
YANDEX_ACCESS_KEY_ID=${YANDEX_ACCESS_KEY_ID}
YANDEX_SECRET_ACCESS_KEY=${YANDEX_SECRET_ACCESS_KEY}
S3_BUCKET=${S3_BUCKET}
S3_ENDPOINT=${S3_ENDPOINT}
S3_REGION=${S3_REGION}
STORAGE_DOMAIN=${STORAGE_DOMAIN}
PUBLIC_READ=${PUBLIC_READ}
S3_MOCK=false
PRESIGN_EXPIRES_SECONDS=3600
EOF
)
  else
    yandex_block=$(cat <<'EOF'
YANDEX_ACCESS_KEY_ID=
YANDEX_SECRET_ACCESS_KEY=
S3_BUCKET=
S3_ENDPOINT=https://storage.yandexcloud.net
S3_REGION=ru-central1
STORAGE_DOMAIN=
PUBLIC_READ=false
S3_MOCK=false
PRESIGN_EXPIRES_SECONDS=3600
EOF
)
  fi

  write_file "${env_path}" 600 <<EOF
# Generated by install.sh on $(date -Iseconds)
STORAGE_BACKEND=${STORAGE_BACKEND}
STORAGE_DIR=${PROJECT_ROOT}/data
DATABASE_PATH=${PROJECT_ROOT}/data/photos.db
${yandex_block}
SITE_DOMAIN=${SITE_DOMAIN}
CORS_ORIGINS=${cors}
MAX_FILE_SIZE_MB=${MAX_FILE_SIZE_MB}
MAX_VIDEO_SIZE_MB=${MAX_VIDEO_SIZE_MB}
MAX_FILES_PER_REQUEST=${MAX_FILES_PER_REQUEST}
RATE_LIMIT_PER_MINUTE=${RATE_LIMIT_PER_MINUTE}
BASE_PATH=${BASE_PATH}
FRONTEND_DIR=${PROJECT_ROOT}/frontend
LETSENCRYPT_EMAIL=${LETSENCRYPT_EMAIL}
# scheme hint for docs: ${scheme}
EOF
  log "Записан ${env_path} (режим 600)"
}

render_template() {
  local src="$1"
  local server_name="$2"
  local base="$3"
  local port="$4"
  local body="$5"
  local base_loc="${base}"
  if [[ -z "${base_loc}" ]]; then
    base_loc=""
  fi
  # shellcheck disable=SC2002
  cat "${src}" \
    | sed "s|__SERVER_NAME__|${server_name}|g" \
    | sed "s|__APP_PORT__|${port}|g" \
    | sed "s|__CLIENT_MAX_BODY__|${body}|g" \
    | sed "s|__BASE_PATH__|${base_loc}|g" \
    | sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g"
}

write_nginx_root_http() {
  local server_name="$1"
  local port="$2"
  local body="$3"
  cat <<EOF
upstream photo_upload_app {
    server 127.0.0.1:${port};
}

server {
    listen 80;
    server_name ${server_name};

    client_max_body_size ${body};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://photo_upload_app;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
}

write_nginx_root_https() {
  local server_name="$1"
  local port="$2"
  local body="$3"
  cat <<EOF
upstream photo_upload_app {
    server 127.0.0.1:${port};
}

server {
    listen 80;
    server_name ${server_name};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name ${server_name};

    ssl_certificate     /etc/letsencrypt/live/${server_name}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${server_name}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size ${body};

    location / {
        proxy_pass http://photo_upload_app;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
EOF
}

nginx_conf_path() {
  if [[ -d /etc/nginx/sites-available ]]; then
    printf '/etc/nginx/sites-available/%s.conf' "${SERVICE_NAME}"
  else
    printf '/etc/nginx/conf.d/%s.conf' "${SERVICE_NAME}"
  fi
}

enable_nginx_site() {
  local conf="$1"
  if [[ -d /etc/nginx/sites-enabled ]]; then
    run ln -sfn "${conf}" "/etc/nginx/sites-enabled/$(basename "${conf}")"
    if [[ -L /etc/nginx/sites-enabled/default ]]; then
      warn "Отключаю default site nginx (sites-enabled/default)"
      run rm -f /etc/nginx/sites-enabled/default
    fi
  fi
}

write_nginx_config() {
  local conf
  conf="$(nginx_conf_path)"
  # Body limit must fit the largest allowed upload (videos are much bigger).
  local biggest=$(( MAX_FILE_SIZE_MB > MAX_VIDEO_SIZE_MB ? MAX_FILE_SIZE_MB : MAX_VIDEO_SIZE_MB ))
  local body_mb=$((biggest + 5))
  local body="${body_mb}m"
  local tmp
  tmp="$(mktemp)"

  if [[ -z "${BASE_PATH}" ]]; then
    if [[ "${ENABLE_HTTPS}" == "yes" && -f \
      "/etc/letsencrypt/live/${SITE_DOMAIN}/fullchain.pem" ]]; then
      write_nginx_root_https "${SITE_DOMAIN}" "${APP_PORT}" "${body}" >"${tmp}"
    else
      write_nginx_root_http "${SITE_DOMAIN}" "${APP_PORT}" "${body}" >"${tmp}"
    fi
  else
    local tpl="${PROJECT_ROOT}/deploy/nginx-host.conf.template"
    if [[ "${ENABLE_HTTPS}" == "yes" && -f \
      "/etc/letsencrypt/live/${SITE_DOMAIN}/fullchain.pem" ]]; then
      tpl="${PROJECT_ROOT}/deploy/nginx-host-https.conf.template"
    fi
    render_template "${tpl}" "${SITE_DOMAIN}" "${BASE_PATH}" \
      "${APP_PORT}" "${body}" >"${tmp}"
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: would write nginx config ${conf}"
    rm -f "${tmp}"
    return 0
  fi
  install -m 644 "${tmp}" "${conf}"
  rm -f "${tmp}"
  enable_nginx_site "${conf}"
  run mkdir -p /var/www/certbot
  run nginx -t
  run systemctl enable nginx
  run systemctl reload nginx || run systemctl restart nginx
  log "nginx: ${conf}"
}

write_systemd_unit() {
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  local tpl="${PROJECT_ROOT}/deploy/photo-upload.service.template"
  local tmp
  tmp="$(mktemp)"
  sed \
    -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
    -e "s|__APP_PORT__|${APP_PORT}|g" \
    -e "s|__BASE_PATH__|${BASE_PATH:-/}|g" \
    "${tpl}" >"${tmp}"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: would install systemd unit ${unit}"
    rm -f "${tmp}"
    return 0
  fi
  install -m 644 "${tmp}" "${unit}"
  rm -f "${tmp}"
  run systemctl daemon-reload
  run systemctl enable "${SERVICE_NAME}"
  run systemctl restart "${SERVICE_NAME}"
  log "systemd: ${unit}"
}

maybe_certbot() {
  if [[ "${SKIP_HTTPS}" -eq 1 || "${ENABLE_HTTPS}" != "yes" ]]; then
    log "HTTPS/certbot пропущен"
    return 0
  fi
  [[ -n "${SITE_DOMAIN}" ]] || die "Для HTTPS нужен SITE_DOMAIN"
  [[ -n "${LETSENCRYPT_EMAIL}" ]] || die "Для HTTPS нужен email (LETSENCRYPT_EMAIL)"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: certbot certonly --nginx -d ${SITE_DOMAIN}"
    return 0
  fi

  if [[ ! -f "/etc/letsencrypt/live/${SITE_DOMAIN}/fullchain.pem" ]]; then
    run certbot certonly --nginx \
      -d "${SITE_DOMAIN}" \
      --email "${LETSENCRYPT_EMAIL}" \
      --agree-tos \
      --no-eff-email \
      --non-interactive || \
      run certbot certonly --webroot -w /var/www/certbot \
        -d "${SITE_DOMAIN}" \
        --email "${LETSENCRYPT_EMAIL}" \
        --agree-tos \
        --no-eff-email \
        --non-interactive
  else
    log "Сертификат уже есть: /etc/letsencrypt/live/${SITE_DOMAIN}/"
  fi

  # Rewrite nginx with HTTPS block now that cert exists.
  ENABLE_HTTPS=yes write_nginx_config
}

health_check() {
  local url="http://127.0.0.1:${APP_PORT}${BASE_PATH}/api/health"
  log "Проверка здоровья: ${url}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "DRY-RUN: skip health check"
    return 0
  fi
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "${url}" >/tmp/photo-upload-health.json 2>/dev/null; then
      log "OK: $(cat /tmp/photo-upload-health.json)"
      rm -f /tmp/photo-upload-health.json
      return 0
    fi
    sleep 1
  done
  warn "Сервис не ответил на ${url}"
  if command -v journalctl >/dev/null 2>&1; then
    journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
  fi
  die "Health check не прошёл"
}

collect_answers() {
  log "Каталог проекта: ${PROJECT_ROOT}"
  prompt SITE_DOMAIN "Домен сайта (без https://)" "photos.example.com"
  [[ -n "${SITE_DOMAIN}" ]] || die "Домен обязателен"
  SITE_DOMAIN="${SITE_DOMAIN#http://}"
  SITE_DOMAIN="${SITE_DOMAIN#https://}"
  SITE_DOMAIN="${SITE_DOMAIN%%/*}"

  prompt BASE_PATH "Путь на сайте (пусто = корень)" "/wedding"
  BASE_PATH="$(normalize_base_path "${BASE_PATH}")"

  prompt STORAGE_BACKEND "Хранилище: local или yandex" "local"
  STORAGE_BACKEND="$(echo "${STORAGE_BACKEND}" | tr '[:upper:]' '[:lower:]')"
  [[ "${STORAGE_BACKEND}" == "local" || "${STORAGE_BACKEND}" == "yandex" ]] \
    || die "STORAGE_BACKEND должен быть local или yandex"

  if [[ "${STORAGE_BACKEND}" == "yandex" ]]; then
    prompt S3_BUCKET "Имя бакета S3" "${S3_BUCKET}"
    prompt S3_REGION "Регион" "${S3_REGION:-ru-central1}"
    prompt STORAGE_DOMAIN "Кастомный домен бакета (опц.)" "${STORAGE_DOMAIN}"
    prompt PUBLIC_READ "Публичное чтение бакета? true/false" "false"
    if [[ "${NON_INTERACTIVE}" -eq 0 ]]; then
      prompt_secret YANDEX_ACCESS_KEY_ID "YANDEX_ACCESS_KEY_ID"
      prompt_secret YANDEX_SECRET_ACCESS_KEY "YANDEX_SECRET_ACCESS_KEY"
    fi
    [[ -n "${YANDEX_ACCESS_KEY_ID}" ]] || die "Нужен YANDEX_ACCESS_KEY_ID"
    [[ -n "${YANDEX_SECRET_ACCESS_KEY}" ]] || die "Нужен YANDEX_SECRET_ACCESS_KEY"
    [[ -n "${S3_BUCKET}" ]] || die "Нужен S3_BUCKET"
  fi

  prompt MAX_FILE_SIZE_MB "Лимит фото, МБ" "${MAX_FILE_SIZE_MB}"
  prompt MAX_VIDEO_SIZE_MB "Лимит видео, МБ" "${MAX_VIDEO_SIZE_MB}"
  prompt MAX_FILES_PER_REQUEST "Файлов за один заход" "${MAX_FILES_PER_REQUEST}"
  prompt RATE_LIMIT_PER_MINUTE "Rate limit / IP в минуту" "${RATE_LIMIT_PER_MINUTE}"
  prompt APP_PORT "Локальный порт uvicorn" "${APP_PORT}"

  if [[ "${ENABLE_HTTPS}" == "ask" ]]; then
    if [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
      ENABLE_HTTPS="no"
    else
      local ans
      read -r -p "Выпустить HTTPS (Let's Encrypt)? [y/N]: " ans || true
      case "${ans}" in
        y|Y|yes|YES) ENABLE_HTTPS="yes" ;;
        *) ENABLE_HTTPS="no" ;;
      esac
    fi
  fi

  if [[ "${ENABLE_HTTPS}" == "yes" ]]; then
    prompt LETSENCRYPT_EMAIL "Email для Let's Encrypt" \
      "${LETSENCRYPT_EMAIL:-admin@${SITE_DOMAIN}}"
  fi

  log "План:"
  log "  domain=${SITE_DOMAIN}"
  log "  base_path=${BASE_PATH:-/}"
  log "  storage=${STORAGE_BACKEND}"
  log "  port=${APP_PORT}"
  log "  https=${ENABLE_HTTPS}"
  log "  service=${SERVICE_NAME}"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --skip-https) SKIP_HTTPS=1; ENABLE_HTTPS=no; shift ;;
      --skip-packages) SKIP_PACKAGES=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Неизвестный аргумент: $1 (см. --help)" ;;
    esac
  done

  need_root
  detect_os
  collect_answers
  ensure_project_layout
  install_packages
  setup_venv
  write_env_file
  write_systemd_unit
  write_nginx_config
  maybe_certbot
  health_check

  local public_url
  if [[ "${ENABLE_HTTPS}" == "yes" ]]; then
    public_url="https://${SITE_DOMAIN}${BASE_PATH}/"
  else
    public_url="http://${SITE_DOMAIN}${BASE_PATH}/"
  fi

  cat <<EOF

Готово.
  Сайт:     ${public_url}
  Health:   ${public_url}api/health
  Сервис:   systemctl status ${SERVICE_NAME}
  Логи:     journalctl -u ${SERVICE_NAME} -f
  Данные:   ${PROJECT_ROOT}/data/
  Конфиг:   ${PROJECT_ROOT}/.env

Дальше:
  1) DNS A-запись ${SITE_DOMAIN} → IP этого сервера
  2) Если HTTPS ещё нет: sudo certbot --nginx -d ${SITE_DOMAIN}
  3) Yandex: примени CORS из scripts/bucket-cors.json на бакет
  4) Инструкция: DEPLOY.md

EOF
}

main "$@"
