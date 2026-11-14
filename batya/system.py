"""Paths, atomic state files, native dialogs and safe process handling."""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def data_directory() -> Path:
    if os.environ.get("BATYA_DATA_DIR"):
        return Path(os.environ["BATYA_DATA_DIR"]).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "BatyaDLP"


def resource_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def tools_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "tools"
    return resource_directory() / "tools"


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def hidden_process_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def open_native(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def select_folder(initial: str) -> str | None:
    if os.name == "nt":
        # Encoded script + environment variable: no user path is interpolated into executable code.
        script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Выберите папку для видео'
$dialog.SelectedPath = $env:BATYA_PICKER_DIR
$dialog.ShowNewFolderButton = $true
try {
  if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
  }
} finally { $dialog.Dispose(); $owner.Dispose() }
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env={**os.environ, "BATYA_PICKER_DIR": initial},
            **hidden_process_options(),
        )
    elif sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Папка для видео")'],
            capture_output=True,
            text=True,
            timeout=300,
        )
    elif shutil.which("zenity"):
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Папка для видео"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    else:
        raise ValueError("Укажите путь к папке вручную.")
    if result.returncode != 0:
        # Windows returns 0 on Cancel, macOS returns 1. Neither is an app error.
        if os.name == "nt":
            raise ValueError("Не удалось открыть окно выбора папки. Укажите путь вручную.")
        return None
    return result.stdout.strip().lstrip("\ufeff") or None
