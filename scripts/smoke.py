"""Real end-to-end download/transcode/restart test, including the frozen Windows EXE."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.cookiejar import CookieJar
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def wait_info(directory, process):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"App exited: {process.returncode}; {(directory / 'app.log').read_text(encoding='utf-8')}"
            )
        try:
            return json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError("Application startup timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    bundle = args.exe.resolve() if args.exe and args.exe.suffix == ".app" else None
    exe = bundle / "Contents/MacOS/Batya" if bundle else args.exe.resolve() if args.exe else None
    command = [str(exe)] if exe else [sys.executable, "-m", "batya"]
    tools = bundle / "Contents/Frameworks/tools" if bundle else exe.parent / "tools" if exe else None
    suffix = "" if bundle else ".exe"
    ffmpeg = tools / ("ffmpeg" + suffix) if tools else shutil.which("ffmpeg")
    ffprobe = tools / ("ffprobe" + suffix) if tools else shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("Install FFmpeg or test the complete packaged directory.")
    with tempfile.TemporaryDirectory(prefix="batya smoke ") as temp:
        root = Path(temp).resolve()
        source, output, data = root / "source", root / "Видео для бати", root / "данные приложения"
        source.mkdir()
        data.mkdir()
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=160x90:r=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "1",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                str(source / "sample.mp4"),
            ],
            check=True,
            timeout=30,
        )
        fixture = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(source)))
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        env = {**os.environ, "BATYA_DATA_DIR": str(data)}
        if bundle:
            env["PATH"] = "/usr/bin:/bin"
        process = subprocess.Popen(command + ["--headless"], cwd=ROOT, env=env)
        try:
            info = wait_info(data, process)
            origin = info["origin"]
            session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            session.open(info["url"], timeout=10).close()

            def api(path, body=None):
                request = urllib.request.Request(
                    origin + "/api/" + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json", "Origin": origin},
                )
                with session.open(request, timeout=15) as response:
                    return json.load(response)

            assert b"<!doctype html>" in session.open(origin, timeout=10).read().lower()
            try:
                urllib.request.urlopen(origin + "/api/state", timeout=10)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("Unauthenticated API must be blocked")
            api("settings", {"folder": str(output), "quality": "720"})
            url = f"http://127.0.0.1:{fixture.server_port}/sample.mp4"

            def download(quality):
                job = api("jobs", {"url": url, "quality": quality})
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    current = next(j for j in api("state")["jobs"] if j["id"] == job["id"])
                    if current["status"] in {"completed", "error"}:
                        assert current["status"] == "completed", current
                        final = Path(current["filename"])
                        assert final.is_relative_to(output) and final.is_file(), current
                        checked = subprocess.run(
                            [
                                str(ffprobe),
                                "-v",
                                "error",
                                "-show_entries",
                                "format=duration",
                                "-of",
                                "json",
                                str(final),
                            ],
                            capture_output=True,
                            check=True,
                            timeout=30,
                        )
                        assert float(json.loads(checked.stdout)["format"]["duration"]) > 0
                        return current
                    time.sleep(0.15)
                raise AssertionError("Download timed out")

            video = download("720")
            assert Path(video["filename"]).suffix == ".mp4"
            assert download("720")["filename"] == video["filename"], "Existing file/retry behavior"
            audio = download("audio")
            assert Path(audio["filename"]).suffix == ".mp3"
            items = []
            for index in range(1, 4):
                shutil.copy2(source / "sample.mp4", source / f"sample-{index}.mp4")
                media = f"http://127.0.0.1:{fixture.server_port}/sample-{index}.mp4"
                items.append(
                    f'<item><title>Видео {index}</title><enclosure url="{media}" type="video/mp4" length="11369"/></item>'
                )
            (source / "playlist.rss").write_text(
                '<?xml version="1.0"?><rss version="2.0"><channel><title>Мой плейлист</title>'
                + "".join(items)
                + "</channel></rss>",
                encoding="utf-8",
            )
            api("jobs", {"url": f"http://127.0.0.1:{fixture.server_port}/playlist.rss", "quality": "720"})
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                jobs = api("state")["jobs"]
                assert sum(j["status"] in {"starting", "downloading", "processing"} for j in jobs) <= 1
                if len(jobs) == 6 and all(j["status"] in {"completed", "error"} for j in jobs):
                    break
                time.sleep(0.1)
            assert len(jobs) == 6 and all(j["status"] == "completed" for j in jobs), jobs
            assert sum(bool(j.get("playlist_url")) for j in jobs) == 3
            second = subprocess.run(command + ["--headless"], cwd=ROOT, env=env, timeout=15)
            assert second.returncode == 0
            assert api("state")["settings"]["folder"] == str(output.resolve())
            api("quit", {})
            process.wait(timeout=30)
            assert process.returncode == 0
            process = subprocess.Popen(command + ["--headless"], cwd=ROOT, env=env)
            info = wait_info(data, process)
            origin = info["origin"]
            session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            session.open(info["url"], timeout=10).close()
            recovered = api("state")
            assert len(recovered["jobs"]) == 6
            assert all(j["status"] == "completed" for j in recovered["jobs"])
            assert recovered["settings"]["quality"] == "720"
            api("quit", {})
            process.wait(timeout=30)
            assert process.returncode == 0
            print(
                "PASS: startup, auth, Cyrillic paths, real MP4, MP3, sequential playlist, single instance, restart, history, exit"
            )
        except BaseException:
            log = data / "app.log"
            if log.exists():
                print(log.read_text(encoding="utf-8"), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
