import json
import os
import subprocess
import sys
import time
from http.client import HTTPConnection

from batya import __version__

LAUNCH = """
import sys
import batya.downloads as downloads
import batya.launcher as launcher
downloads.__version__ = launcher.__version__ = sys.argv.pop(1)
launcher.main()
"""


def wait_for_instance(directory, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, f"Launcher exited with {process.returncode}"
        try:
            info = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
            if info["pid"] == process.pid:
                return info
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("Application did not publish its local address")


def call_instance(info, endpoint, method="GET", body=None):
    port = int(info["origin"].rsplit(":", 1)[1])
    key = info["url"].split("?key=", 1)[1]
    client = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {
            "Cookie": f"batya_{port}={key}",
            "Origin": info["origin"],
            "Content-Type": "application/json",
        }
        client.request(method, endpoint, body=json.dumps(body) if body is not None else None, headers=headers)
        response = client.getresponse()
        assert response.status == 200
        return json.load(response)
    finally:
        client.close()


def test_new_launcher_replaces_old_instance_without_losing_settings(tmp_path):
    env = {**os.environ, "BATYA_DATA_DIR": str(tmp_path)}
    old = subprocess.Popen([sys.executable, "-c", LAUNCH, "1.2.0", "--headless"], env=env)
    new = None
    try:
        previous = wait_for_instance(tmp_path, old)
        assert call_instance(previous, "/api/state")["app_version"] == "1.2.0"
        saved = call_instance(
            previous, "/api/settings", "POST", {"folder": str(tmp_path / "Видео"), "quality": "720"}
        )
        new = subprocess.Popen([sys.executable, "-m", "batya", "--headless"], env=env)
        assert old.wait(timeout=12) == 0
        current = wait_for_instance(tmp_path, new)
        state = call_instance(current, "/api/state")
        assert state["app_version"] == __version__
        assert state["settings"] == saved
        older = subprocess.run([sys.executable, "-c", LAUNCH, "1.2.0", "--headless"], env=env, timeout=12)
        assert older.returncode == 0
        assert call_instance(current, "/api/state")["app_version"] == __version__
        call_instance(current, "/api/quit", "POST")
        assert new.wait(timeout=12) == 0
    finally:
        for process in (new, old):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
