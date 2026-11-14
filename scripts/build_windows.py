"""Build a self-contained portable Windows directory; CI smoke-tests before zipping."""

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent


def download(url: str, target: Path, digest: str | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": "BatyaDLP-build"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest and actual != digest:
        target.unlink()
        raise RuntimeError(f"SHA-256 mismatch: {url}")
    return actual


def main():
    if os.name != "nt":
        raise SystemExit("Build this distribution on Windows (or use GitHub Actions).")
    os.chdir(ROOT)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name",
            "Batya",
            "--distpath",
            "build/frozen",
            "--contents-directory",
            "_internal",
            "--add-data",
            "batya/static:batya/static",
            "--collect-data",
            "certifi",
            "--hidden-import",
            "uvicorn.logging",
            "--hidden-import",
            "uvicorn.loops.asyncio",
            "--hidden-import",
            "uvicorn.protocols.http.h11_impl",
            "--hidden-import",
            "uvicorn.lifespan.on",
            "--exclude-module",
            "yt_dlp",
            "--exclude-module",
            "pytest",
            "--exclude-module",
            "tkinter",
            "batya/launcher.py",
        ],
        check=True,
    )
    target = ROOT / "dist" / "BatyaDLP"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT / "build/frozen/Batya", target)
    tools = target / "tools"
    licenses = target / "licenses"
    tools.mkdir()
    licenses.mkdir()
    cache = ROOT / ".cache" / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / "scripts/tools.lock.json").read_text(encoding="utf-8"))
    for name, item in lock.items():
        archive = cache / item["name"]
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != item["sha256"]:
            print(f"Downloading and verifying {item['name']}", flush=True)
            download(item["url"], archive, item["sha256"])
        if name == "yt-dlp":
            shutil.copy2(archive, tools / "yt-dlp.exe")
        else:
            with zipfile.ZipFile(archive) as zipped:
                for info in zipped.infolist():
                    if info.is_dir():
                        continue
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("Unsafe archive member")
                    binary = path.name.lower()
                    if name == "deno" and binary == "deno.exe":
                        (tools / binary).write_bytes(zipped.read(info))
                    elif (
                        name == "ffmpeg"
                        and "bin" in path.parts
                        and (binary in {"ffmpeg.exe", "ffprobe.exe"} or binary.endswith(".dll"))
                    ):
                        (tools / binary).write_bytes(zipped.read(info))
                    elif name == "ffmpeg" and (
                        "doc" in path.parts or "license" in binary or "readme" in binary
                    ):
                        destination = licenses / "ffmpeg" / Path(*path.parts[1:])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(zipped.read(info))
    notices = {
        "yt-dlp-LICENSE.txt": f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{lock['yt-dlp']['tag']}/LICENSE",
        "yt-dlp-THIRD_PARTY_LICENSES.txt": f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{lock['yt-dlp']['tag']}/THIRD_PARTY_LICENSES.txt",
        "deno-LICENSE.txt": f"https://raw.githubusercontent.com/denoland/deno/{lock['deno']['tag']}/LICENSE.md",
        "ffmpeg-LGPL-3.0.txt": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n8.1.3/COPYING.LGPLv3",
    }
    for filename, url in notices.items():
        download(url, licenses / filename)
    # Preserve each installed Python distribution's original license metadata.
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            normalized = str(file).lower()
            if ".dist-info/" in normalized and ("license" in normalized or "copying" in normalized):
                source = distribution.locate_file(file)
                if source.is_file():
                    dest = licenses / "python" / distribution.metadata["Name"] / Path(str(file)).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    # Exact, public provenance for the LGPL binary and its build recipe.
    ffmpeg_source_commit = lock["ffmpeg"]["name"].split("-g", 1)[1].split("-", 1)[0]
    lock["ffmpeg"]["ffmpeg_source"] = f"https://github.com/FFmpeg/FFmpeg/tree/{ffmpeg_source_commit}"
    lock["ffmpeg"]["build_recipe"] = (
        "https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/" + lock["ffmpeg"]["tag"]
    )
    (tools / "manifest.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    for name in ["LICENSE", "THIRD_PARTY.md", "README.md"]:
        shutil.copy2(ROOT / name, target / name)
    (target / "НАЧНИТЕ ЗДЕСЬ.txt").write_text(
        "БАТЯ КАЧАЕТ\n\n1. Распакуйте архив целиком.\n2. Запустите Batya.exe двойным кликом.\n"
        "3. Вставьте ссылку на видео и нажмите Скачать видео.\n\n"
        "Не перемещайте EXE отдельно от tools и _internal. Можно создать ярлык на рабочем столе.\n"
        "Чтобы закрыть приложение, нажмите значок питания справа сверху в интерфейсе.\n"
        "Настройки и история: %LOCALAPPDATA%\\BatyaDLP\n"
        "Исходники и новые выпуски: https://github.com/zalupa-ronaldo/batya-dlp\n",
        encoding="utf-8-sig",
    )
    for binary in ["yt-dlp", "ffmpeg", "ffprobe", "deno"]:
        subprocess.run(
            [str(tools / (binary + ".exe")), "-version" if binary.startswith("ff") else "--version"],
            check=True,
            capture_output=True,
            timeout=45,
        )
    print(f"Portable directory ready: {target}")


if __name__ == "__main__":
    main()
