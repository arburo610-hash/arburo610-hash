"""Google Диск: поиск и чтение файлов в папках, расшаренных на сервисный аккаунт.

Только чтение. Загружать файлы в «Мой диск» сервисный аккаунт не может (у него нет своего места на Диске),
для этого нужен вход под аккаунтом Анастасии — отдельный этап.
"""
import json
import os

import sheets

API = "https://www.googleapis.com/drive/v3/files"
MAX_TEXT = 20000  # сколько символов содержимого отдаём Claude за раз

KINDS = {
    "папка": "application/vnd.google-apps.folder",
    "таблица": "application/vnd.google-apps.spreadsheet",
    "документ": "application/vnd.google-apps.document",
    "pdf": "application/pdf",
}
NAMES = {v: k for k, v in KINDS.items()}

_session = None


def _api():
    global _session
    if _session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if raw:
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(
                os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"), scopes=scopes)
        _session = AuthorizedSession(creds)
    return _session


def _get(url, **params):
    resp = _api().get(url, params=params, timeout=60)
    if resp.status_code >= 300:
        raise ValueError(f"Google Диск ответил {resp.status_code}: {resp.text[:200]}")
    return resp


def _short(f):
    mime = f["mimeType"]
    kind = NAMES.get(mime) or ("фото" if mime.startswith("image/") else "видео" if mime.startswith("video/") else mime)
    return {"id": f["id"], "название": f["name"], "тип": kind, "изменён": f.get("modifiedTime", "")[:10],
            "ссылка": f.get("webViewLink", "")}


def _list(q, limit):
    fields = "files(id,name,mimeType,modifiedTime,webViewLink)"
    data = _get(API, q=q, fields=fields, pageSize=limit, orderBy="modifiedTime desc").json()
    return [_short(f) for f in data.get("files", [])]


def search(name, kind=""):
    """Файлы и папки, в названии которых есть name (без учёта регистра)."""
    q = f"name contains '{name.replace(chr(39), ' ')}' and trashed = false"
    if kind in KINDS:
        q += f" and mimeType = '{KINDS[kind]}'"
    elif kind == "фото":
        q += " and mimeType contains 'image/'"
    return _list(q, 30)


def list_folder(folder):
    """Содержимое папки: folder — id папки или часть её названия."""
    folder_id = folder
    if len(folder) < 25 or " " in folder:  # похоже на название, а не на id
        found = search(folder, "папка")
        if not found:
            raise ValueError(f"Папка «{folder}» не найдена среди доступных ассистенту")
        folder_id = found[0]["id"]
    return {"папка": folder_id, "содержимое": _list(f"'{folder_id}' in parents and trashed = false", 100)}


def read(file_id):
    """Текст файла: таблица — значения листов, документ — текст. Остальное — только описание."""
    meta = _get(f"{API}/{file_id}", fields="id,name,mimeType,modifiedTime,webViewLink").json()
    mime = meta["mimeType"]
    if mime == KINDS["таблица"]:
        book = sheets._gc().open_by_key(file_id)
        parts = []
        for ws in book.worksheets():
            rows = ws.get_all_values()
            parts.append(f"## Лист «{ws.title}»\n" + "\n".join(" | ".join(c for c in r if c) for r in rows if any(r)))
        text = "\n\n".join(parts)
    elif mime == KINDS["документ"]:
        text = _get(f"{API}/{file_id}/export", mimeType="text/plain").text
    else:
        return {**_short(meta), "содержимое": "Этот тип файла я пока не читаю — могу дать ссылку."}
    cut = len(text) > MAX_TEXT
    return {**_short(meta), "содержимое": text[:MAX_TEXT] + ("\n…(обрезано)" if cut else "")}
