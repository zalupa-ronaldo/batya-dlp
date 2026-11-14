"""Download only official yt-dlp assets, verify SHA-256 before atomic replacement."""

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import certifi

from batya.system import external_environment, hidden_process_options, tool_filename

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
    if os.name != "nt" and sys.platform != "darwin":
        raise ValueError("В режиме разработки обновите yt-dlp командой: pip install -U yt-dlp[default]")
    name = "yt-dlp_macos.zip" if sys.platform == "darwin" else tool_filename("yt-dlp")
    report("Проверяем новую версию…")
    release = json.loads(fetch(RELEASE_API))
    tag = release["tag_name"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        raise ValueError("Неверный номер выпуска yt-dlp.")
    base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/"
    sums = fetch(base + "SHA2-256SUMS").decode("utf-8")
    report(f"Скачиваем yt-dlp {tag}…")
    content = fetch(base + name, limit=100_000_000)
    verified_asset(content, sums, name)
    if sys.platform == "darwin":
        return install_macos_runtime(content, target)
    headers = (
        (b"MZ",)
        if os.name == "nt"
        else (
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xca\xfe\xba\xbe",
            b"\xca\xfe\xba\xbf",
        )
    )
    if not content.startswith(headers):
        raise ValueError("Обновление не является приложением для вашей системы.")
    staged = target.with_name("yt-dlp.update.exe" if os.name == "nt" else "yt-dlp.update")
    try:
        staged.write_bytes(content)
        if os.name != "nt":
            staged.chmod(0o755)
        checked = subprocess.run(
            [str(staged), "--ignore-config", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=external_environment(),
            **hidden_process_options(),
        )
        version = checked.stdout.strip()
        if checked.returncode or not version or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
            raise ValueError("Новая версия не запускается. Старый yt-dlp сохранён.")
        os.replace(staged, target)
        return version
    finally:
        staged.unlink(missing_ok=True)


def install_macos_runtime(content: bytes, target: Path) -> str:
    """Verify an immutable onedir runtime, then atomically switch the executable symlink."""
    runtime = Path(tempfile.mkdtemp(prefix="yt-dlp-runtime-", dir=target.parent))
    link = target.with_name("yt-dlp.link")
    installed = False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zipped:
            for item in zipped.infolist():
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ValueError("Небезопасный путь в архиве yt-dlp.")
                destination = runtime.joinpath(*path.parts)
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(zipped.read(item))
                    destination.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
        binary = runtime / "yt-dlp_macos"
        binary.chmod(0o755)
        checked = subprocess.run(
            [str(binary), "--ignore-config", "--no-plugin-dirs", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=external_environment(),
            **hidden_process_options(),
        )
        version = checked.stdout.strip()
        if checked.returncode or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
            raise ValueError("Новая версия не запускается. Старый yt-dlp сохранён.")
        link.unlink(missing_ok=True)
        link.symlink_to(binary)
        os.replace(link, target)
        installed = True
        return version
    finally:
        link.unlink(missing_ok=True)
        if not installed:
            shutil.rmtree(runtime, ignore_errors=True)
