"""Resolve official release assets to an auditable URL/SHA-256 lock file."""

import json
import os
import re
import urllib.request
from pathlib import Path


def api(path):
    headers = {"User-Agent": "BatyaDLP-build", "Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + (os.environ.get("GH_TOKEN") or os.environ["GITHUB_TOKEN"])
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def asset(release, pattern):
    selected = next(a for a in release["assets"] if re.fullmatch(pattern, a["name"]))
    digest = selected.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Missing GitHub SHA-256: {selected['name']}")
    return {
        "name": selected["name"],
        "url": selected["browser_download_url"],
        "sha256": digest.removeprefix("sha256:"),
        "size": selected["size"],
        "release": release["html_url"],
        "tag": release["tag_name"],
    }


def main():
    ffmpeg = next(
        r
        for r in api("repos/FFmpeg-Builds/FFmpeg-Builds/releases?per_page=10")
        if r["tag_name"].startswith("autobuild-")
    )
    lock = {
        "yt-dlp": asset(api("repos/yt-dlp/yt-dlp/releases/latest"), r"yt-dlp\.exe"),
        "deno": asset(api("repos/denoland/deno/releases/latest"), r"deno-x86_64-pc-windows-msvc\.zip"),
        "ffmpeg": asset(ffmpeg, r"ffmpeg-n8\.1.*-win64-lgpl-shared-8\.1\.zip"),
    }
    target = Path(__file__).parent / "tools.lock.json"
    target.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
