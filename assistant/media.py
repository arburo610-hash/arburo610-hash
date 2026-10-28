"""Обработка материалов с выезда: расшифровка голосовых и видео, кадры из видео, картинки для Claude.

Расшифровка — локально через faster-whisper (бесплатно, файлы никуда не уходят).
Кадры из видео — через ffmpeg. Оба компонента необязательны: без них бот работает, но хуже понимает видео и голос.
"""
import base64
import json
import logging
import os
import shutil
import subprocess

_whisper = None
log = logging.getLogger("assistant.media")


last_error = ""  # почему не получилась последняя расшифровка — бот показывает это пользователю


def _model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        # модель (~150 МБ) кладём в хранилище, чтобы не скачивать её заново при каждом перезапуске
        root = os.path.join(os.environ.get("MEDIA_ROOT", "media"), "models")
        _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8",
                                download_root=root)
    return _whisper


def preload():
    """Загружает модель заранее (в фоне при старте бота), чтобы первое голосовое не ждало скачивания."""
    try:
        _model()
        log.info("Модель расшифровки готова")
    except Exception:
        log.exception("Модель расшифровки не загрузилась")


def _load_audio(path):
    """Звук в виде массива 16 кГц моно через ffmpeg.

    Свой декодер faster-whisper (библиотека av) ломается на новых версиях av, а ffmpeg на сервере есть всегда.
    """
    if not shutil.which("ffmpeg"):
        return path
    import numpy as np
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(path):
    """Текст голосового / звуковой дорожки видео или None, если расшифровать не удалось (причина — в last_error)."""
    global last_error
    try:
        segments, _ = _model().transcribe(_load_audio(path), language="ru", vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
    except ImportError:
        last_error = "не установлен faster-whisper"
        return None
    except Exception as e:
        log.exception("transcribe failed")
        last_error = f"{type(e).__name__}: {e}"[:300]
        return None
    last_error = "" if text else "в записи не нашлось речи"
    return text or None


def video_frames(path, out_dir, count=4):
    """Сохраняет несколько равномерно распределённых кадров из видео, возвращает пути к ним."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return []
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True,
    )
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return []
    base = os.path.splitext(os.path.basename(path))[0]
    frames = []
    for i in range(count):
        at = duration * (i + 0.5) / count
        out = os.path.join(out_dir, f"{base}_кадр{i + 1}.jpg")
        subprocess.run(
            ["ffmpeg", "-v", "quiet", "-y", "-ss", f"{at:.2f}", "-i", path, "-frames:v", "1", "-vf", "scale=1280:-2", out],
            check=False,
        )
        if os.path.exists(out):
            frames.append(out)
    return frames


def image_block(path):
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    media_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
