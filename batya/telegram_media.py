"""Fetch audio from supported video sites and split large MP3s on frame boundaries."""

import json
import math
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from batya.downloads import validate_url
from batya.system import external_environment, hidden_process_options, terminate_tree

MAX_AUDIO_BYTES = 48_000_000
VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "vk.com",
    "vkvideo.ru",
    "rutube.ru",
    "vimeo.com",
    "soundcloud.com",
)


def video_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"\u00ab\u00bb]+", text, re.IGNORECASE)
    if not match:
        raise ValueError("Пришлите ссылку на видео или аудио.")
    url = validate_url(match[0].rstrip(".,;!?)]"))
    parsed = urlsplit(url)
    host = parsed.hostname.rstrip(".")
    if parsed.port not in {None, 80, 443} or not any(
        host == domain or host.endswith("." + domain) for domain in VIDEO_DOMAINS
    ):
        raise ValueError("Поддерживаются ссылки YouTube, VK, Rutube, Vimeo и SoundCloud.")
    return url


def run_tool(args: list[str], timeout: int) -> str:
    env = external_environment()
    env.pop("BATYA_TELEGRAM_TOKEN", None)
    with subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **hidden_process_options(),
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            terminate_tree(process)
            raise
    if process.returncode:
        if "timed out" in stderr.lower():
            raise ValueError("Сайт слишком долго отвечает. Попробуйте ещё раз.")
        raise ValueError("Не удалось обработать запись. Проверьте, что ссылка открывается без входа.")
    return stdout


def split_mp3(source: Path, ffmpeg: str, ffprobe: str, limit: int = MAX_AUDIO_BYTES) -> list[Path]:
    size = source.stat().st_size
    if not size:
        raise ValueError("Получился пустой MP3.")
    if size <= limit:
        return [source]
    output = run_tool(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)], 60
    )
    duration = float(json.loads(output)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Не удалось определить длительность записи.")
    segment_time = duration * limit * 0.90 / size
    for attempt in range(5):
        # A separate directory prevents stale segments from an earlier pass being sent.
        folder = source.parent / f"parts-{attempt}"
        folder.mkdir()
        run_tool(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-map_metadata",
                "-1",
                "-c:a",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                f"{segment_time:.6f}",
                "-segment_start_number",
                "1",
                "-reset_timestamps",
                "1",
                str(folder / "part-%06d.mp3"),
            ],
            300,
        )
        parts = sorted(folder.glob("part-*.mp3"))
        sizes = [part.stat().st_size for part in parts]
        if sizes and all(0 < part_size <= limit for part_size in sizes):
            return parts
        segment_time *= min(0.8, limit * 0.9 / max(sizes, default=limit + 1))
    raise ValueError("Не удалось разбить запись на файлы нужного размера.")


def prepare_audio(url: str, folder: Path, ffmpeg: str, ffprobe: str) -> tuple[str, list[Path]]:
    title_log = run_tool(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--no-simulate",
            "--no-colors",
            "--encoding",
            "utf-8",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--abort-on-unavailable-fragments",
            "--ffmpeg-location",
            ffmpeg,
            "-f",
            "ba/b",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "128K",
            "--print",
            "after_move:BATYA_TITLE:%(title)j",
            "-o",
            str(folder / "audio.%(ext)s"),
            "--",
            url,
        ],
        1800,
    )
    title = "Аудиозапись"
    for line in title_log.splitlines():
        if line.startswith("BATYA_TITLE:"):
            title = str(json.loads(line.removeprefix("BATYA_TITLE:")))[:160]
    source = folder / "audio.mp3"
    if not source.is_file():
        raise ValueError("Не удалось получить MP3 из этой ссылки.")
    return title, split_mp3(source, ffmpeg, ffprobe)
