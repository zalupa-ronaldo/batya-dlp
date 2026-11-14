"""One bounded download queue; yt-dlp stays an independently updateable process."""

import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from batya import __version__
from batya.system import (
    atomic_json,
    external_environment,
    hidden_process_options,
    open_native,
    terminate_tree,
    tool_filename,
    tools_directory,
)
from batya.updater import update_binary

LOG = logging.getLogger(__name__)
ACTIVE = {"queued", "starting", "downloading", "processing"}
MEDIA = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
QUALITIES = {"1080", "720", "best", "audio"}
BROWSERS = {"none", "chrome", "firefox", "edge", "brave", "safari", "file"}


def validate_url(value: str) -> str:
    value = value.strip()
    if len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("Ссылка слишком длинная или содержит недопустимые символы.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError("Вставьте полную ссылку на видео, начинающуюся с https://") from None
    return value


def writable_folder(value: str) -> Path:
    path = Path(value).expanduser()
    if not value.strip() or not path.is_absolute():
        raise ValueError("Укажите полный путь к папке.")
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        return path.resolve()
    except OSError as exc:
        raise ValueError("В эту папку нельзя записывать файлы. Выберите другую папку.") from exc


def number(value, fallback=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (ValueError, TypeError):
        return fallback


class DownloadManager:
    def __init__(self, data_dir: Path, *, command: list[str] | None = None):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.playlist_pending = deque()
        self.stopping = threading.Event()
        self.process = None
        self.current_id = None
        self.thread = None
        self.update_thread = None
        self.update = {"busy": False, "message": "", "error": False}
        self.version = "Проверяем…"
        self.jobs = []
        self.settings = {
            "folder": str(Path.home() / "Downloads" / "Батя качает"),
            "quality": "1080",
            "browser": "none",
            "cookies_file": "",
        }
        self._load()
        self.command = command or self._command()

    def _command(self) -> list[str]:
        name = tool_filename("yt-dlp")
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            runtime = tools_directory().parent.parent / "Resources" / "yt-dlp-runtime"
            if runtime.is_dir():
                installed = self.data_dir / "tools/yt-dlp-runtime-initial"
                installed.parent.mkdir(parents=True, exist_ok=True)
                if not installed.exists():
                    shutil.copytree(runtime, installed)
                target = installed.parent / name
                if not target.is_symlink() or not target.exists():
                    link = target.with_name("yt-dlp.link")
                    link.unlink(missing_ok=True)
                    link.symlink_to(installed / name)
                    os.replace(link, target)
                return [str(target)]
        bundled = tools_directory() / name
        if bundled.exists():
            mutable = self.data_dir / "tools" / name
            mutable.parent.mkdir(parents=True, exist_ok=True)
            if not mutable.exists():
                shutil.copy2(bundled, mutable)
            if os.name != "nt":
                mutable.chmod(0o755)
            return [str(mutable)]
        if getattr(sys, "frozen", False):
            raise RuntimeError(f"Не найден tools/{name}. Распакуйте ZIP-архив целиком.")
        return [sys.executable, "-m", "yt_dlp"]

    def _load(self):
        path = self.data_dir / "state.json"
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("settings"), dict):
                raise ValueError("Invalid state")
            self.settings.update(
                {k: v for k, v in state["settings"].items() if k in self.settings and isinstance(v, str)}
            )
            self.jobs = [j for j in state.get("jobs", []) if isinstance(j, dict) and "id" in j][-20000:]
            for job in self.jobs:
                if job.get("status") in ACTIVE:
                    job.update(status="interrupted", error="Приложение было закрыто. Нажмите «Повторить».")
        except (OSError, ValueError, TypeError):
            LOG.exception("Could not load state; preserving corrupt file")
            shutil.copy2(path, path.with_name(f"state.damaged-{time.time_ns()}.json"))
            self.jobs = []

    def _save(self):
        finished = [j["id"] for j in self.jobs if j["status"] in {"completed", "error", "cancelled"}]
        discard = set(finished[:-200])
        self.jobs = [j for j in self.jobs if j["id"] not in discard]
        atomic_json(self.data_dir / "state.json", {"settings": self.settings, "jobs": self.jobs})

    def start(self):
        self.thread = threading.Thread(target=self._worker, name="download-queue", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            process = self.process
        if process:
            terminate_tree(process)
        self.pending.put(None)
        if self.thread:
            self.thread.join(timeout=12)
        with self.lock:
            for job in self.jobs:
                if job["status"] in ACTIVE:
                    job.update(status="interrupted", error="Приложение было закрыто. Нажмите «Повторить».")
            self._save()

    def snapshot(self):
        with self.lock:
            # Serialization creates an isolated copy, including mutable nested job dictionaries.
            return json.loads(
                json.dumps(
                    {
                        "app_version": __version__,
                        "yt_dlp_version": self.version,
                        "settings": self.settings,
                        "jobs": list(reversed(self.jobs)),
                        "update": self.update,
                    }
                )
            )

    def save_settings(
        self, folder: str, quality: str, browser: str | None = None, cookies_file: str | None = None
    ):
        if quality not in QUALITIES:
            raise ValueError("Выберите качество из списка.")
        path = writable_folder(folder)
        with self.lock:
            selected = self.settings["browser"] if browser is None else browser
            cookie_path = self.settings["cookies_file"] if cookies_file is None else cookies_file.strip()
            if selected not in BROWSERS:
                raise ValueError("Выберите браузер из списка.")
            if selected == "safari" and sys.platform != "darwin":
                raise ValueError("Safari доступен только на Mac.")
            if selected == "file":
                p = Path(cookie_path).expanduser()
                if not p.is_absolute() or not p.is_file():
                    raise ValueError("Укажите полный путь к существующему файлу cookies.txt.")
                cookie_path = str(p.resolve())
            self.settings = {
                "folder": str(path),
                "quality": quality,
                "browser": selected,
                "cookies_file": cookie_path,
            }
            self._save()
        return self.settings.copy()

    def enqueue(self, url: str, quality: str):
        url = validate_url(url)
        if quality not in QUALITIES:
            raise ValueError("Выберите качество из списка.")
        with self.lock:
            if self.stopping.is_set():
                raise ValueError("Приложение закрывается.")
            if self.update["busy"]:
                raise ValueError("Дождитесь завершения обновления yt-dlp.")
            if sum(j["status"] in ACTIVE and not j.get("playlist_url") for j in self.jobs) >= 20:
                raise ValueError("В очереди уже 20 ссылок. Дождитесь завершения загрузок.")
            folder = writable_folder(self.settings["folder"])
            job = {
                "id": uuid.uuid4().hex,
                "url": url,
                "quality": quality,
                "folder": str(folder),
                "title": "Получаем название видео…",
                "status": "queued",
                "percent": 0,
                "speed": 0,
                "eta": None,
                "downloaded": 0,
                "total": 0,
                "filename": None,
                "created": time.time(),
                "error": "",
                "resolved": False,
            }
            self.jobs.append(job)
            self._save()
            self.pending.put(job["id"])
            return job.copy()

    def _job(self, job_id: str):
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        raise ValueError("Загрузка не найдена.")

    def cancel(self, job_id: str):
        with self.lock:
            job = self._job(job_id)
            if job["status"] not in ACTIVE:
                return
            job["status"] = "cancelled"
            process = self.process if self.current_id == job_id else None
            self._save()
        if process:
            terminate_tree(process)

    def media_path(self, job_id: str) -> Path:
        with self.lock:
            job = self._job(job_id).copy()
        if job["status"] != "completed" or not job["filename"]:
            raise ValueError("Видео ещё не скачано.")
        path = Path(job["filename"]).resolve()
        if not path.is_relative_to(Path(job["folder"]).resolve()) or path.suffix.lower() not in MEDIA:
            raise ValueError("Этот файл нельзя открыть из приложения.")
        if not path.is_file():
            raise ValueError("Файл перемещён или удалён. Откройте папку загрузок.")
        return path

    def open_media(self, job_id: str):
        open_native(self.media_path(job_id))

    def open_folder(self, job_id: str | None = None):
        with self.lock:
            value = self._job(job_id)["folder"] if job_id else self.settings["folder"]
        open_native(writable_folder(value))

    def begin_update(self):
        with self.lock:
            if self.update["busy"]:
                raise ValueError("Обновление уже идёт.")
            if self.current_id is not None or any(j["status"] in ACTIVE for j in self.jobs):
                raise ValueError("Сначала дождитесь завершения загрузок или отмените их.")
            if len(self.command) != 1:
                raise ValueError("В режиме разработки: pip install -U yt-dlp[default]")
            self.update = {"busy": True, "message": "Проверяем обновление…", "error": False}
            self.update_thread = threading.Thread(target=self._update, name="yt-dlp-updater", daemon=True)
            self.update_thread.start()

    def _update(self):
        def report(message):
            with self.lock:
                self.update["message"] = message

        try:
            version = update_binary(Path(self.command[0]), report)
            with self.lock:
                self.version = version
                self.update = {
                    "busy": False,
                    "message": f"Установлена актуальная версия {version}",
                    "error": False,
                }
        except Exception as exc:
            LOG.exception("Update failed")
            with self.lock:
                self.update = {"busy": False, "message": f"Не удалось обновить yt-dlp: {exc}", "error": True}

    def _arguments(self, job: dict, authentication: list[str]) -> list[str]:
        args = self.command + [
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-playlist",
            "--no-simulate",
            "--newline",
            "--progress",
            "--no-colors",
            "--encoding",
            "utf-8",
            "--windows-filenames",
            "--no-overwrites",
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--abort-on-unavailable-fragments",
            "--http-chunk-size",
            "10M",
            "--cache-dir",
            str(self.data_dir / "cache"),
            "--progress-template",
            "download:BATYA_PROGRESS:%(progress)j",
            "--progress-template",
            "postprocess:BATYA_POST:%(progress)j",
            "--print",
            "before_dl:BATYA_META:%(.{title,id,duration})j",
            "--print",
            "after_move:BATYA_FILE:%(filepath)j",
            "--output",
            str(Path(job["folder"]) / "%(title).140B [%(id)s].%(ext)s"),
        ]
        ffmpeg = tools_directory() / tool_filename("ffmpeg")
        deno = tools_directory() / tool_filename("deno")
        if ffmpeg.exists():
            args += ["--ffmpeg-location", str(ffmpeg.parent)]
        if deno.exists():
            args += ["--js-runtimes", f"deno:{deno}"]
        args += authentication
        if job["quality"] == "audio":
            args += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K"]
        else:
            cap = "" if job["quality"] == "best" else f"[height<={job['quality']}]"
            # Prefer H.264 + AAC for Windows Media Player; keep a fallback for other sites.
            formats = f"bv{cap}[vcodec^=avc1]+ba[acodec^=mp4a]/b{cap}[ext=mp4]/bv{cap}+ba/b{cap}/b"
            args += ["--format", formats, "--merge-output-format", "mp4", "--remux-video", "mp4"]
        return args + ["--", job["url"]]

    def _authentication(self, job: dict) -> list[str]:
        if job.get("cookie_warning"):
            return []
        with self.lock:
            browser = self.settings.get("browser", "none")
            filename = self.settings.get("cookies_file", "")
        if browser == "file":
            # yt-dlp may refresh this user-selected cookie jar; it never enters state.json or logs.
            return ["--cookies", filename]
        return ["--cookies-from-browser", browser] if browser in BROWSERS - {"none", "file"} else []

    @staticmethod
    def _chrome_cookie_problem(authentication: list[str], line: str) -> str:
        if authentication != ["--cookies-from-browser", "chrome"]:
            return ""
        message = line.lower()
        if "could not copy chrome cookie database" in message:
            return "Не удалось прочитать cookies Chrome. Полностью закройте Chrome и попробуйте снова."
        if "could not find chrome cookies database" in message:
            return "Не найдена база cookies Chrome. Проверьте, что вы вошли на сайт в Chrome."
        if "failed to decrypt with dpapi" in message or re.search(r"\bextracted 0 cookies from chrome\b", message):
            return "Сессия Chrome недоступна. Для входа используйте Firefox или файл cookies.txt."
        return ""

    def _resolve(self, job: dict):
        if job.get("resolved"):
            return
        args = self.command + [
            "--ignore-config",
            "--no-plugin-dirs",
            "--flat-playlist",
            "--dump-single-json",
            "--yes-playlist",
            "--skip-download",
            "--encoding",
            "utf-8",
            "--socket-timeout",
            "20",
            "--extractor-retries",
            "2",
        ]
        deno = tools_directory() / tool_filename("deno")
        if deno.exists():
            args += ["--js-runtimes", f"deno:{deno}"]
        authentication = self._authentication(job)
        args += authentication + ["--", job["url"]]
        with self.lock:
            if job["status"] == "cancelled" or self.stopping.is_set():
                return
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                env=external_environment(),
                **hidden_process_options(),
            )
            self.process = process
        metadata = None
        tail = deque(maxlen=8)
        cookie_problem = ""
        for line in process.stdout:
            if len(line) > 50_000_000:
                terminate_tree(process)
                raise ValueError("Слишком большой плейлист. Попробуйте ссылку на его часть.")
            if line.startswith("{"):
                metadata = json.loads(line)
            elif line.strip():
                tail.append(line.strip())
                cookie_problem = self._chrome_cookie_problem(authentication, line) or cookie_problem
        code = process.wait()
        with self.lock:
            if job["status"] == "cancelled" or self.stopping.is_set():
                return
            if cookie_problem:
                job["cookie_warning"] = cookie_problem + " Пробуем без входа."
                job["note"] = "Проверяем ссылку без входа…"
        if cookie_problem and (code or not isinstance(metadata, dict)):
            return self._resolve(job)
        with self.lock:
            if job["status"] == "cancelled" or self.stopping.is_set():
                return
            if code or not isinstance(metadata, dict):
                raise ValueError("\n".join(tail)[-2500:] or "Не удалось прочитать ссылку.")
            job["resolved"] = True
            if metadata.get("_type") not in {"playlist", "multi_video"}:
                if metadata.get("title"):
                    job["title"] = str(metadata["title"])[:500]
                return
            entries = [e for e in (metadata.get("entries") or []) if isinstance(e, dict)]
            children = []
            for index, entry in enumerate(entries, 1):
                url = entry.get("webpage_url") or entry.get("url") or ""
                if not url.startswith(("https://", "http://")) and entry.get("ie_key") == "Youtube":
                    url = "https://www.youtube.com/watch?v=" + str(entry.get("id", ""))
                try:
                    url = validate_url(url)
                except ValueError:
                    continue
                child = {
                    **job,
                    "id": uuid.uuid4().hex,
                    "url": url,
                    "status": "queued",
                    "title": str(entry.get("title") or "Видео")[:500],
                    "resolved": True,
                    "playlist_url": job["url"],
                    "playlist_title": str(metadata.get("title") or "Плейлист")[:300],
                    "playlist_index": index,
                    "playlist_count": len(entries),
                    "created": time.time(),
                }
                children.append(child)
            if not children:
                raise ValueError("В плейлисте нет доступных видео.")
            first = children.pop(0)
            first["id"] = job["id"]
            first["status"] = "starting"
            position = self.jobs.index(job)
            job.update(first)
            self.jobs[position + 1 : position + 1] = children
            self.playlist_pending.extend(child["id"] for child in children)
            self._save()

    def _worker(self):
        try:
            result = subprocess.run(
                self.command + ["--ignore-config", "--version"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=external_environment(),
                **hidden_process_options(),
            )
            with self.lock:
                self.version = result.stdout.strip() if result.returncode == 0 else "Не найден"
        except (OSError, subprocess.TimeoutExpired):
            with self.lock:
                self.version = "Не найден"
        while not self.stopping.is_set():
            job_id = self.playlist_pending.popleft() if self.playlist_pending else self.pending.get()
            if job_id is None or self.stopping.is_set():
                break
            with self.lock:
                job = next((j for j in self.jobs if j["id"] == job_id), None)
                if not job or job["status"] != "queued":
                    continue
                job["status"] = "starting"
                self.current_id = job_id
            try:
                self._resolve(job)
                self._download(job)
            except Exception as exc:
                LOG.exception("Could not resolve URL")
                with self.lock:
                    if job["status"] != "cancelled":
                        job.update(status="error", error=self._friendly_error(str(exc), job))
            with self.lock:
                self.current_id = None
                self.process = None
                self._save()

    def _line(self, job: dict, line: str):
        prefix, separator, payload = line.partition(":")
        if not separator or not prefix.startswith("BATYA_"):
            return
        try:
            value = json.loads(payload)
            with self.lock:
                if job["status"] == "cancelled":
                    return
                if prefix == "BATYA_META":
                    job["title"] = str(value.get("title") or "Видео")[:500]
                elif prefix == "BATYA_PROGRESS":
                    total = number(value.get("total_bytes") or value.get("total_bytes_estimate"))
                    downloaded = number(value.get("downloaded_bytes"))
                    job.update(
                        status="processing" if value.get("status") == "finished" else "downloading",
                        downloaded=downloaded,
                        total=total,
                        speed=number(value.get("speed")),
                        eta=value.get("eta"),
                        percent=min(100, max(0, downloaded / total * 100)) if total else 0,
                    )
                elif prefix == "BATYA_POST":
                    job["status"] = "processing"
                elif prefix == "BATYA_FILE" and isinstance(value, str):
                    job["filename"] = value
        except (ValueError, TypeError, AttributeError):
            LOG.warning("Unrecognized yt-dlp event: %s", prefix)

    @staticmethod
    def _friendly_error(details: str, job: dict) -> str:
        text = details.lower()
        if "403" in text or "sign in" in text or "cookies" in text or "login" in text:
            if job.get("cookie_warning"):
                return (
                    "Без входа сайт не дал скачать видео. В блоке «Доступ к сайтам» выберите Firefox, "
                    "где вы вошли на сайт, или файл cookies.txt и нажмите «Повторить».\n\n" + details
                )
            return (
                "Сайт ограничил доступ к видео. В блоке «Доступ к сайтам» выберите браузер, "
                "в котором видео открывается, или файл cookies.txt, затем нажмите «Повторить».\n\n" + details
            )
        return details or "Не удалось скачать видео. Проверьте ссылку и интернет."

    def _download(self, job: dict, attempt: int = 0):
        tail = deque(maxlen=12)
        cookie_problem = ""
        try:
            with self.lock:
                if job["status"] == "cancelled" or self.stopping.is_set():
                    return
                authentication = self._authentication(job)
                arguments = self._arguments(job, authentication)
                if attempt:
                    arguments[len(self.command) : len(self.command)] = ["--force-ipv4"]
                    if "--format" in arguments:
                        index = arguments.index("--format") + 1
                        cap = "" if job["quality"] == "best" else f"[height<={job['quality']}]"
                        arguments[index] = (
                            f"b{cap}[vcodec^=avc1][protocol^=m3u8]/"
                            f"bv{cap}[vcodec^=avc1][protocol^=m3u8]+ba/"
                            f"b{cap}[protocol^=m3u8]/bv{cap}[protocol^=m3u8]+ba/" + arguments[index]
                        )
                process = subprocess.Popen(
                    arguments,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=external_environment(),
                    **hidden_process_options(),
                )
                self.process = process
            for raw in process.stdout:
                line = raw.strip()
                if line.startswith("BATYA_"):
                    self._line(job, line)
                elif line:
                    tail.append(line)
                    cookie_problem = self._chrome_cookie_problem(authentication, line) or cookie_problem
            result = process.wait()
            details = "\n".join(tail)[-3000:]
            with self.lock:
                if job["status"] == "cancelled" or self.stopping.is_set():
                    return
                if cookie_problem:
                    job["cookie_warning"] = cookie_problem + " Пробуем без входа."
                    if result != 0:
                        job.update(status="starting", note="Повторяем без входа…")
            if cookie_problem and result != 0:
                self._download(job, attempt=attempt)
                return
            with self.lock:
                retry = (
                    result != 0
                    and attempt < 2
                    and not self.stopping.is_set()
                    and job["status"] != "cancelled"
                    and "403" in details
                )
                if retry:
                    job.update(status="starting", note="Обновляем ссылку и продолжаем загрузку…")
            if retry:
                self._download(job, attempt=attempt + 1)
                return
            with self.lock:
                if job["status"] == "cancelled" or self.stopping.is_set():
                    return
                if result != 0:
                    job.update(
                        status="error",
                        error=self._friendly_error(details, job),
                    )
                elif not job["filename"] or not Path(job["filename"]).is_file():
                    job.update(status="error", error="Загрузка завершилась, но готовый файл не найден.")
                else:
                    job.update(status="completed", percent=100, speed=0, eta=0, completed=time.time())
        except Exception as exc:
            LOG.exception("Download failed")
            with self.lock:
                if job["status"] != "cancelled":
                    job.update(status="error", error=f"Не удалось запустить загрузку: {exc}")
