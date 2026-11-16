"""Double-click entry point, single instance, browser and graceful server lifecycle."""

import argparse
import json
import logging
import os
import re
import socket
import threading
import time
import webbrowser
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from batya import __version__
from batya.app import create_app
from batya.downloads import DownloadManager
from batya.system import atomic_json, data_directory


class InstanceLock:
    def __init__(self, directory: Path):
        self.directory = directory
        self.file = None

    def acquire(self) -> bool:
        self.file = (self.directory / "instance.lock").open("a+b")
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def close(self):
        if self.file:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


def _version_number(version: str) -> tuple[int, ...]:
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Unrecognized app version")
    return tuple(map(int, version.split(".")))


def _claim_instance(lock: InstanceLock, headless: bool) -> bool:
    """Start when free, ask older versions to quit, or open the running version."""
    deadline = time.monotonic() + 10
    quitting_url = None
    current = _version_number(__version__)
    while time.monotonic() < deadline:
        if lock.acquire():
            return True
        try:
            info = json.loads((lock.directory / "instance.json").read_text(encoding="utf-8"))
            url = info["url"]
            match = re.fullmatch(r"(http://127\.0\.0\.1:([0-9]{1,5}))/\?key=([A-Za-z0-9_-]{32,128})", url)
            if not match or match[1] != info["origin"] or not 0 < int(match[2]) <= 65535:
                raise ValueError("Invalid local instance address")
            if url != quitting_url:
                origin, port, key = match.groups()
                headers = {"Cookie": f"batya_{port}={key}", "Origin": origin}
                # Connect directly to loopback so a proxy or redirect cannot receive the key.
                with closing(HTTPConnection("127.0.0.1", int(port), timeout=2)) as connection:
                    connection.request("GET", "/api/state", headers=headers)
                    response = connection.getresponse()
                    if response.status != 200:
                        raise ValueError("Cannot verify the running instance")
                    running = _version_number(json.loads(response.read())["app_version"])
                    if running >= current:
                        if not headless:
                            webbrowser.open(url)
                        return False
                    connection.request("POST", "/api/quit", body=b"{}", headers=headers)
                    response = connection.getresponse()
                    if response.status != 200:
                        raise ValueError("The running instance could not close")
                    quitting_url = url
                    deadline = time.monotonic() + 45
        except (OSError, HTTPException, ValueError, KeyError, TypeError):
            # The owner may still be starting or may have closed between requests.
            pass
        time.sleep(0.1)
    if quitting_url:
        raise RuntimeError("Старая версия не успела закрыться. Подождите и снова запустите новый Batya.exe.")
    raise RuntimeError("Не удалось связаться с запущенной версией. Закройте её и снова запустите Batya.exe.")


def main():
    parser = argparse.ArgumentParser(description="Батя качает — простой загрузчик видео")
    parser.add_argument("--headless", action="store_true", help="Не открывать браузер")
    parser.add_argument("--port", type=int, default=0, help="Локальный порт (0 = автоматически)")
    args = parser.parse_args()
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(directory)
    listener = None
    try:
        if not _claim_instance(lock, args.headless):
            return
        handler = RotatingFileHandler(
            directory / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        logging.basicConfig(
            handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )
        manager = DownloadManager(directory)
        # Keep this socket bound until uvicorn takes it; no port discovery/rebind race.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        app = create_app(manager, origin)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_config=None,
                access_log=False,
                loop="asyncio",
                http="h11",
                ws="none",
                timeout_graceful_shutdown=15,
            )
        )
        app.state.shutdown = lambda: setattr(server, "should_exit", True)
        url = f"{origin}/?key={app.state.token}"

        def ready():
            while not server.started and not server.should_exit:
                time.sleep(0.05)
            if server.started:
                atomic_json(directory / "instance.json", {"url": url, "origin": origin, "pid": os.getpid()})
                if not args.headless:
                    webbrowser.open(url)

        threading.Thread(target=ready, daemon=True).start()
        server.run(sockets=[listener])
    except Exception as exc:
        logging.exception("Startup failed")
        if os.name == "nt" and not args.headless:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                f"Не удалось запустить приложение:\n{exc}\n\nЖурнал: {directory / 'app.log'}",
                "Батя качает",
                0x10,
            )
        else:
            raise
    finally:
        if lock.file is not None:
            try:
                (directory / "instance.json").unlink(missing_ok=True)
                if listener:
                    listener.close()
            finally:
                lock.close()


if __name__ == "__main__":
    main()
