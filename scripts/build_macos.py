"""Build a self-contained Apple Silicon app using the local Homebrew FFmpeg build."""

import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from build_windows import download

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Run this build on an Apple Silicon Mac.")
    os.chdir(ROOT)
    cache = ROOT / ".cache/macos"
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / "scripts/tools.macos.lock.json").read_text())
    for name, item in lock.items():
        target = cache / item["name"]
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            print(f"Downloading {item['name']}", flush=True)
            download(item["url"], target, item["sha256"])
        if name == "deno":
            with zipfile.ZipFile(target) as archive:
                (cache / "deno").write_bytes(archive.read("deno"))
    (cache / "deno").chmod(0o755)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("Install FFmpeg for the build: brew install ffmpeg")
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
            "dist/macos",
            "--workpath",
            "build/macos",
            "--paths",
            str(ROOT),
            "--osx-bundle-identifier",
            "org.batya-dlp.app",
            "--target-architecture",
            "arm64",
            "--add-data",
            "batya/static:batya/static",
            "--collect-data",
            "certifi",
            "--add-binary",
            f"{ffmpeg}:tools",
            "--add-binary",
            f"{ffprobe}:tools",
            "--exclude-module",
            "yt_dlp",
            "--exclude-module",
            "pytest",
            "--exclude-module",
            "tkinter",
            "--hidden-import",
            "uvicorn.logging",
            "--hidden-import",
            "uvicorn.loops.asyncio",
            "--hidden-import",
            "uvicorn.protocols.http.h11_impl",
            "--hidden-import",
            "uvicorn.lifespan.on",
            "batya/launcher.py",
        ],
        check=True,
    )
    app = ROOT / "dist/macos/Batya.app"
    tools = app / "Contents/Frameworks/tools"
    tools.mkdir(parents=True, exist_ok=True)
    # The official onedir runtime avoids extracting and revalidating hundreds of native
    # Python modules for each video. Keep its original signed binaries intact.
    runtime = app / "Contents/Resources/yt-dlp-runtime"
    with zipfile.ZipFile(cache / "yt-dlp_macos.zip") as zipped:
        for item in zipped.infolist():
            path = runtime / item.filename
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zipped.read(item))
                path.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
    (runtime / "yt-dlp_macos").chmod(0o755)
    for name in ["deno"]:
        shutil.copy2(cache / name, tools / name)
        (tools / name).chmod(0o755)
    entitlements = cache / "deno-entitlements.plist"
    entitlements.write_bytes(
        plistlib.dumps(
            {
                "com.apple.security.cs.allow-jit": True,
                "com.apple.security.cs.allow-unsigned-executable-memory": True,
            }
        )
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--entitlements", str(entitlements), str(tools / "deno")],
        check=True,
    )
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(
        {
            "CFBundleDisplayName": "Батя качает",
            "CFBundleShortVersionString": "1.1.2",
            "CFBundleVersion": "1.1.2",
            "LSMinimumSystemVersion": platform.mac_ver()[0],
            "NSHighResolutionCapable": True,
        }
    )
    info_path.write_bytes(plistlib.dumps(info))
    licenses = app / "Contents/Resources/licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for filename in ["LICENSE", "THIRD_PARTY.md", "README.md"]:
        shutil.copy2(ROOT / filename, licenses / filename)
    for distribution in importlib.metadata.distributions():
        for item in distribution.files or []:
            name = str(item).lower()
            if ".dist-info/" in name and ("license" in name or "copying" in name):
                source = distribution.locate_file(item)
                if source.is_file():
                    dest = licenses / "python" / distribution.metadata["Name"] / Path(str(item)).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    # Preserve Homebrew package notices and machine-readable source provenance for all
    # non-system libraries copied by PyInstaller, without relying on Homebrew at runtime.
    origins = {Path(ffmpeg).resolve().parents[1], Path(ffprobe).resolve().parents[1]}
    toc = ROOT / "build/macos/Batya/Analysis-00.toc"
    if toc.exists():
        import ast

        def collect(value):
            if isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif isinstance(value, str) and value.startswith("/opt/homebrew/Cellar/"):
                parts = Path(value).parts
                origins.add(Path(*parts[:6]))

        collect(ast.literal_eval(toc.read_text()))
    packages = []
    for origin in sorted(origins):
        if not origin.is_dir():
            continue
        destination = licenses / "homebrew" / origin.parent.name / origin.name
        for source in origin.iterdir():
            if source.is_file() and (
                source.name.startswith(("LICENSE", "COPYING")) or source.name == "sbom.spdx.json"
            ):
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / source.name)
        recipe = origin / ".brew"
        if recipe.exists():
            shutil.copytree(recipe, destination / "recipe", dirs_exist_ok=True)
        packages.append(f"{origin.parent.name}/{origin.name}")
    for filename, url in {
        "yt-dlp-LICENSE": "https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.10.26/LICENSE",
        "yt-dlp-THIRD_PARTY_LICENSES.txt": "https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.10.26/THIRD_PARTY_LICENSES.txt",
        "deno-LICENSE.md": "https://raw.githubusercontent.com/denoland/deno/v2.9.8/LICENSE.md",
    }.items():
        download(url, licenses / filename)
    manifest = {
        "architecture": "arm64",
        "minimum_macos": info["LSMinimumSystemVersion"],
        "tools": lock,
        "homebrew_packages": packages,
        "ffmpeg_source": "https://ffmpeg.org/download.html",
        "build_recipe": "scripts/build_macos.py",
    }
    (app / "Contents/Resources/tools-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    # Mach-O files have already been signed individually, then seal the containing bundle.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    for name in ["deno", "ffmpeg", "ffprobe"]:
        subprocess.run(
            [str(tools / name), "-version" if name.startswith("ff") else "--version"],
            check=True,
            capture_output=True,
            timeout=45,
            env={"PATH": "/usr/bin:/bin"},
        )
    subprocess.run(
        [str(runtime / "yt-dlp_macos"), "--ignore-config", "--no-plugin-dirs", "--version"],
        check=True,
        capture_output=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin"},
    )
    print(f"Built and verified {app}", flush=True)


if __name__ == "__main__":
    main()
