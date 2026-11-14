import hashlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from batya.app import create_app
from batya.downloads import DownloadManager, validate_url
from batya.launcher import InstanceLock
from batya.updater import verified_asset

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def manager(tmp_path):
    obj = DownloadManager(
        tmp_path / "данные", command=[sys.executable, str(Path(__file__).with_name("fake_ytdlp.py"))]
    )
    obj.save_settings(str(tmp_path / "Мои видео с пробелами"), "1080")
    yield obj
    obj.close()


@pytest.fixture
def client(manager):
    app = create_app(manager, ORIGIN, "test-secret")
    with TestClient(app, base_url=ORIGIN) as session:
        session.get("/?key=test-secret")
        yield session


def wait_for(manager, job_id, statuses, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = next(j for j in manager.snapshot()["jobs"] if j["id"] == job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.025)
    raise AssertionError(manager.snapshot())


def test_auth_origin_host_and_static(manager):
    app = create_app(manager, ORIGIN, "secret")
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/?key=wrong").status_code == 401
        login = client.get("/?key=secret", follow_redirects=False)
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state", headers={"Host": "evil.test"}).status_code == 400
        assert client.post("/api/folder/open", headers={"Origin": "https://evil.test"}).status_code == 403
        assert client.post("/api/folder/open", headers={"Origin": "null"}).status_code == 403
        assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_download_persistence_and_open(client, manager, monkeypatch):
    created = client.post("/api/jobs", json={"url": "https://example.com/video", "quality": "720"})
    assert created.status_code == 201
    job = wait_for(manager, created.json()["id"], {"completed", "error"})
    assert job["status"] == "completed", job
    assert job["title"] == "Тестовое видео <script>"
    assert manager.media_path(job["id"]).read_bytes() == b"fixture"
    opened = []
    monkeypatch.setattr("batya.downloads.open_native", opened.append)
    assert client.post(f"/api/jobs/{job['id']}/open").status_code == 200
    assert opened == [Path(job["filename"])]
    loaded = DownloadManager(manager.data_dir, command=manager.command)
    assert loaded.snapshot()["jobs"][0]["status"] == "completed"
    assert loaded.snapshot()["settings"]["quality"] == "1080"
    Path(job["filename"]).unlink()
    assert client.post(f"/api/jobs/{job['id']}/open").status_code == 400


def test_cancel_active_and_queued_then_continue(client, manager):
    first = client.post("/api/jobs", json={"url": "https://example.com/slow"}).json()
    wait_for(manager, first["id"], {"downloading"})
    second = client.post("/api/jobs", json={"url": "https://example.com/queued"}).json()
    assert client.post(f"/api/jobs/{second['id']}/cancel").status_code == 200
    assert client.post(f"/api/jobs/{first['id']}/cancel").status_code == 200
    third = client.post("/api/jobs", json={"url": "https://example.com/next"}).json()
    assert wait_for(manager, third["id"], {"completed", "error"})["status"] == "completed"
    jobs = {j["id"]: j for j in manager.snapshot()["jobs"]}
    assert jobs[first["id"]]["status"] == jobs[second["id"]]["status"] == "cancelled"


def test_failed_download_has_details(client, manager):
    job = client.post("/api/jobs", json={"url": "https://example.com/fail"}).json()
    result = wait_for(manager, job["id"], {"error"})
    assert "unavailable" in result["error"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "-x",
        "https://",
        "https://user:pass@example.com",
        "https://example.com:bad",
        "https://example.com\n--exec foo",
    ],
)
def test_url_validation(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_no_file_open_outside_download_folder(manager, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    job = manager.enqueue("https://example.com", "1080")
    manager.jobs[0].update(status="completed", filename=str(outside))
    with pytest.raises(ValueError, match="нельзя открыть"):
        manager.media_path(job["id"])
    exe = Path(job["folder"]) / "danger.exe"
    exe.write_bytes(b"x")
    manager.jobs[0]["filename"] = str(exe)
    with pytest.raises(ValueError):
        manager.media_path(job["id"])


def test_interrupted_state_recovery(manager):
    manager.enqueue("https://example.com", "1080")
    recovered = DownloadManager(manager.data_dir, command=manager.command)
    assert recovered.snapshot()["jobs"][0]["status"] == "interrupted"


def test_snapshot_does_not_mutate_live_state(manager):
    job = manager.enqueue("https://example.com", "1080")
    snapshot = manager.snapshot()
    snapshot["jobs"][0]["status"] = "completed"
    assert manager._job(job["id"])["status"] == "queued"


def test_update_checksums_fail_closed():
    data = b"test release"
    digest = hashlib.sha256(data).hexdigest()
    verified_asset(data, f"{digest}  yt-dlp.exe\n", "yt-dlp.exe")
    for sums in ["", f"{digest}  yt-dlp_x86.exe", "0" * 64 + "  yt-dlp.exe"]:
        with pytest.raises(ValueError):
            verified_asset(data, sums, "yt-dlp.exe")


def test_settings_validation_and_bounded_queue(client, manager):
    assert client.post("/api/settings", json={"folder": "relative", "quality": "1080"}).status_code == 400
    assert client.post("/api/jobs", json={"url": "https://example.com", "quality": "8k"}).status_code == 422
    assert client.post("/api/jobs", json={"url": "file:///C:/Windows/notepad.exe"}).status_code == 400
    # Stop the consumer to inspect the queue capacity deterministically.
    manager.stopping.set()
    manager.pending.put(None)
    manager.thread.join(timeout=5)
    manager.stopping.clear()
    for _ in range(20):
        manager.enqueue("https://example.com", "1080")
    with pytest.raises(ValueError, match="20"):
        manager.enqueue("https://example.com", "1080")


def test_single_instance_lock(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


def test_corrupt_state_is_preserved(tmp_path):
    (tmp_path / "state.json").write_text("{ broken", encoding="utf-8")
    manager = DownloadManager(tmp_path, command=[sys.executable])
    assert manager.snapshot()["jobs"] == []
    assert len(list(tmp_path.glob("state.damaged-*.json"))) == 1
    assert json.loads(json.dumps(manager.snapshot()))["app_version"] == "1.0.1"
