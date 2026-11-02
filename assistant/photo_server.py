"""Мини-сервер фото: отдаёт фото заметок по длинной случайной ссылке, чтобы Google Таблица показала миниатюру
через =IMAGE(...). Ничего, кроме фото с известной ссылкой, не отдаёт.

Адрес — публичный домен Railway (Settings → Networking → Generate Domain); Railway сам кладёт его
в переменную RAILWAY_PUBLIC_DOMAIN. Без домена сервер не нужен, и в таблице остаётся текстовая пометка.
"""
import logging
import mimetypes
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("assistant.photo_server")
PORT = int(os.environ.get("PORT", "8080"))

_files = {}  # ссылка → путь к файлу; заполняется из заметок


def public_photos_enabled():
    """Публичные фото выключены по умолчанию: ссылки доступны любому, кто их знает."""
    return os.environ.get("ENABLE_PUBLIC_PHOTOS", "").lower() == "true"


def base_url():
    if not public_photos_enabled():
        return ""
    domain = os.environ.get("PUBLIC_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return ""
    return domain if domain.startswith("http") else f"https://{domain}"


def new_token():
    return secrets.token_urlsafe(24)


def register(token, path):
    _files[token] = path


def url(token):
    root = base_url()
    return f"{root}/p/{token}" if root and token else ""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not public_photos_enabled():
            self.send_error(404)
            return
        token = self.path[len("/p/"):] if self.path.startswith("/p/") else ""
        if "/" in token or "?" in token or "#" in token:
            self.send_error(404)
            return
        path = _files.get(token)
        if not path or not os.path.exists(path):
            self.send_error(404)
            return
        mime = mimetypes.guess_type(path)[0] or ""
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            self.send_error(415)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # не засоряем журнал каждым запросом Google
        pass


def start():
    if not base_url():
        log.info("Публичные фото отключены или нет домена — таблицы покажут текстовую пометку")
        return
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Фото для таблиц отдаются через %s", base_url())
