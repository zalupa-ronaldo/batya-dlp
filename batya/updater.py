"""Download only official yt-dlp assets, verify SHA-256 before atomic replacement."""

import hashlib
import json
import os
import re
import ssl
import subprocess
import urllib.request
from pathlib import Path

import certifi

from batya.system import hidden_process_options

RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
HEADERS = {"User-Agent": "BatyaDLP/1.0", "Accept": "application/vnd.github+json"}


def fetch(url: str, limit: int = 2_000_000) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Обновления должны загружаться по HTTPS.")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(
        request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())
    ) as r:
        if not r.url.startswith("https://"):
            raise ValueError("Небезопасное перенаправление при обновлении.")
        content = r.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Размер обновления превышает допустимый.")
        return content


def verified_asset(content: bytes, sums: str, name: str) -> None:
    expected = None
    for line in sums.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == name:
            expected = fields[0].lower()
            break
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("У обновления нет контрольной суммы. Старый yt-dlp сохранён.")
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Контрольная сумма обновления не совпала. Старый yt-dlp сохранён.")


def update_binary(target: Path, report) -> str:
    if os.name != "nt":
        raise ValueError("В режиме разработки обновите yt-dlp командой: pip install -U yt-dlp[default]")
    report("Проверяем новую версию…")
    release = json.loads(fetch(RELEASE_API))
    tag = release["tag_name"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        raise ValueError("Неверный номер выпуска yt-dlp.")
    base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/"
    sums = fetch(base + "SHA2-256SUMS").decode("utf-8")
    report(f"Скачиваем yt-dlp {tag}…")
    content = fetch(base + "yt-dlp.exe", limit=80_000_000)
    verified_asset(content, sums, "yt-dlp.exe")
    if not content.startswith(b"MZ"):
        raise ValueError("Обновление не является Windows-приложением.")
    staged = target.with_name("yt-dlp.update.exe")
    try:
        staged.write_bytes(content)
        checked = subprocess.run(
            [str(staged), "--ignore-config", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **hidden_process_options(),
        )
        version = checked.stdout.strip()
        if checked.returncode or not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
            raise ValueError("Новая версия не запускается. Старый yt-dlp сохранён.")
        os.replace(staged, target)
        return version
    finally:
        staged.unlink(missing_ok=True)
