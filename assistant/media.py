"""Обработка материалов с выезда: расшифровка голосовых и видео, кадры из видео, картинки для Claude.

Расшифровка — локально через faster-whisper (бесплатно, файлы никуда не уходят).
Кадры из видео — через ffmpeg. Оба компонента необязательны: без них бот работает, но хуже понимает видео и голос.
"""
import base64
import json
import os
import shutil
import subprocess

_whisper = None


def transcribe(path):
    """Текст голосового / звуковой дорожки видео или None, если faster-whisper не установлен."""
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    if _whisper is None:
        _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(path, language="ru", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


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
