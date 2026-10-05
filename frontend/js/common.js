/* Shared helpers for upload and gallery pages. */

export const IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "image/heif",
]);

export const VIDEO_TYPES = new Set([
  "video/mp4",
  "video/webm",
  "video/quicktime",
  "video/x-m4v",
]);

export const ALLOWED_TYPES = new Set([...IMAGE_TYPES, ...VIDEO_TYPES]);

export const MAX_SIZE_MB_DEFAULT = 40;
export const MAX_VIDEO_SIZE_MB_DEFAULT = 500;
// 0 = no per-request file count limit.
export const MAX_FILES_DEFAULT = 0;

export function isVideoType(type) {
  return VIDEO_TYPES.has((type || "").toLowerCase());
}

/** Editable wedding copy — change names/date here and in HTML titles. */
export const SITE = {
  groom: "Данил",
  bride: "Полина",
  dateLabel: "17 октября 2026",
  dateShort: "17 · 10 · 2026",
  tagline: "Поделитесь своими снимками с нашего дня",
};

/** Public prefix when the UI is served under a subpath (e.g. /wedding). */
export function detectBasePath() {
  const meta = document.querySelector('meta[name="base-path"]');
  if (meta) {
    return (meta.getAttribute("content") || "").replace(/\/$/, "");
  }
  const path = window.location.pathname || "";
  if (path === "/wedding" || path.startsWith("/wedding/")) {
    return "/wedding";
  }
  return "";
}

export const BASE_PATH = detectBasePath();

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

export function formatWhen(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).replace(",", "").replace(":", ".");
  } catch {
    return iso;
  }
}

export async function api(path, options = {}) {
  const url = path.startsWith("http") ? path : `${BASE_PATH}${path}`;
  const headers = { ...(options.headers || {}) };
  // Do not force JSON Content-Type on FormData / no-body GET.
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, { ...options, headers });
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text || res.statusText };
  }
  if (!res.ok) {
    const detail = data?.detail;
    const message = Array.isArray(detail)
      ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
      : detail || `Ошибка ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function showAlert(el, message, kind) {
  if (!el) return;
  el.hidden = !message;
  el.className = `alert alert-${kind || "error"}`;
  el.textContent = message || "";
}

export function applySiteCopy() {
  document.querySelectorAll("[data-site]").forEach((node) => {
    const key = node.getAttribute("data-site");
    if (key && SITE[key] != null) node.textContent = SITE[key];
  });
  const names = `${SITE.groom} & ${SITE.bride}`;
  document.querySelectorAll("[data-site-names]").forEach((node) => {
    node.textContent = names;
  });
}

/* ---- Guest identity (remembered name) ---- */

export const GUEST_NAME_COOKIE = "guest_name";
export const HOME_URL = "index.html";
export const UPLOAD_URL = "upload.html";

function _cookieEscape(name) {
  return name.replace(/([.*+?^${}()|[\]\\])/g, "\\$1");
}

export function getCookie(name) {
  const m = document.cookie.match(
    new RegExp("(?:^|; )" + _cookieEscape(name) + "=([^;]*)"),
  );
  return m ? decodeURIComponent(m[1]) : "";
}

/** Remember the guest name for a year so returning guests skip the form. */
export function setGuestName(name) {
  document.cookie =
    `${GUEST_NAME_COOKIE}=${encodeURIComponent(name)}; path=/; max-age=${60 * 60 * 24 * 365}; samesite=lax`;
}

export function clearGuestName() {
  document.cookie = `${GUEST_NAME_COOKIE}=; path=/; max-age=0; samesite=lax`;
}

export function guestName() {
  return getCookie(GUEST_NAME_COOKIE).trim();
}

/** A guest whose name is remembered never needs the home (name) page in the nav. */
export function hideHomeForKnownGuest() {
  if (!guestName()) return;
  document.querySelectorAll(`.nav-link[href="${HOME_URL}"]`).forEach((link) => link.remove());
}
