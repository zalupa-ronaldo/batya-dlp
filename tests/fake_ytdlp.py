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
folder = Path(sys.argv[sys.argv.index("--output") + 1]).parent
print('BATYA_META:{"title":"Тестовое видео <script>"}', flush=True)
if "slow" in url:
    print('BATYA_PROGRESS:{"downloaded_bytes":25,"total_bytes":100,"speed":12,"eta":6}', flush=True)
    time.sleep(60)
if "fail" in url:
    print("ERROR: video is unavailable", flush=True)
    raise SystemExit(1)
path = folder / "Тестовое видео [123].mp4"
path.write_bytes(b"fixture")
print('BATYA_PROGRESS:{"downloaded_bytes":100,"total_bytes":100,"status":"finished"}', flush=True)
print("BATYA_FILE:" + json.dumps(str(path)), flush=True)
