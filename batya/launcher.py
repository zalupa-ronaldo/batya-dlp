"""Double-click entry point, single instance, browser and graceful server lifecycle."""

import argparse
import json
import logging
import os
import socket
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

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
        if self.file.read(1) == b"":
            self.file.write(b"0")
            self.file.flush()
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


def _open_running(directory: Path, headless: bool):
    for _ in range(30):
        try:
            info = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
            if not headless:
                webbrowser.open(info["url"])
            return
        except (OSError, ValueError, KeyError):
            time.sleep(0.1)
    raise RuntimeError("Приложение уже запускается. Попробуйте открыть его через несколько секунд.")


def main():
    parser = argparse.ArgumentParser(description="Батя качает — простой загрузчик видео")
    parser.add_argument("--headless", action="store_true", help="Не открывать браузер")
    parser.add_argument("--port", type=int, default=0, help="Локальный порт (0 = автоматически)")
    args = parser.parse_args()
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(
        handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    lock = InstanceLock(directory)
    if not lock.acquire():
        _open_running(directory, args.headless)
        return
    listener = None
    try:
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
        (directory / "instance.json").unlink(missing_ok=True)
        if listener:
            listener.close()
        lock.close()


if __name__ == "__main__":
    main()
