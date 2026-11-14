"""A child-process fixture for queue/cancellation tests; integration smoke uses real yt-dlp."""

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

if "--version" in sys.argv:
    print("2026.11.14-test")
    raise SystemExit

url = sys.argv[-1]
resolving = "--dump-single-json" in sys.argv
if "--cookies-from-browser" in sys.argv and "chrome" in sys.argv:
    if resolving and "chrome-locked" in url:
        print("ERROR: Could not copy Chrome cookie database.")
        raise SystemExit(1)
    if not resolving and "chrome-encrypted" in url:
        print("ERROR: Failed to decrypt with DPAPI.")
        raise SystemExit(1)
    if "chrome-empty" in url:
        print("[Cookies] Extracted 0 cookies from chrome (4 could not be decrypted)")
if "needs-login" in url and "--cookies-from-browser" not in sys.argv and "--cookies" not in sys.argv:
    print("ERROR: Sign in to confirm your age. Use cookies.", flush=True)
    raise SystemExit(1)
if resolving:
    if "playlist" in url:
        print(
            json.dumps(
                {
                    "_type": "playlist",
                    "title": "Тестовый плейлист",
                    "entries": [
                        {"title": "Первый", "url": "https://example.com/first"},
                        {"title": "Недоступный", "url": "https://example.com/fail"},
                        {"title": "Последний", "url": "https://example.com/last"},
                    ],
                }
            )
        )
    else:
        print(json.dumps({"_type": "video", "title": "Тестовое видео <script>"}))
    raise SystemExit
folder = Path(sys.argv[sys.argv.index("--output") + 1]).parent
if "transient403" in url:
    marker = folder / ".403-attempt"
    if not marker.exists():
        marker.write_text("1")
        print("ERROR: HTTP Error 403: Forbidden", flush=True)
        raise SystemExit(1)
print('BATYA_META:{"title":"Тестовое видео <script>"}', flush=True)
if "slow" in url:
    print('BATYA_PROGRESS:{"downloaded_bytes":25,"total_bytes":100,"speed":12,"eta":6}', flush=True)
    time.sleep(60)
if "fail" in url:
    print("ERROR: video is unavailable", flush=True)
    raise SystemExit(1)
path = folder / ("Тестовое видео [" + url.rsplit("/", 1)[-1] + "].mp4")
path.write_bytes(b"fixture")
print('BATYA_PROGRESS:{"downloaded_bytes":100,"total_bytes":100,"status":"finished"}', flush=True)
print("BATYA_FILE:" + json.dumps(str(path)), flush=True)
