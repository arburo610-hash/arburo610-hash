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


def base_url():
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
        token = self.path.rsplit("/", 1)[-1] if self.path.startswith("/p/") else ""
        path = _files.get(token)
        if not path or not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # не засоряем журнал каждым запросом Google
        pass


def start():
    if not base_url():
        log.info("Публичного адреса нет — фото в таблицах будут текстовой пометкой")
        return
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Фото для таблиц отдаются через %s", base_url())
