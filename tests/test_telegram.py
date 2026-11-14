import json
import shutil
import subprocess

import httpx
import pytest

from batya.telegram_bot import TelegramAPI, handle_message
from batya.telegram_media import split_mp3, video_url


def test_video_url_accepts_a_shared_youtube_link_and_rejects_other_hosts():
    assert video_url("Послушай https://youtu.be/example?t=2") == "https://youtu.be/example?t=2"
    for text in ["https://youtube.com.example.com/test", "http://127.0.0.1/video", "https://u@youtube.com/x"]:
        with pytest.raises(ValueError):
            video_url(text)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs FFmpeg")
def test_split_makes_independent_mp3s_below_the_limit(tmp_path):
    source = tmp_path / "audio.mp3"
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-b:a",
            "128k",
            str(source),
        ],
        check=True,
    )
    assert split_mp3(source, ffmpeg, ffprobe, 100_000) == [source]
    parts = split_mp3(source, ffmpeg, ffprobe, 24_000)
    assert len(parts) > 1
    duration = 0.0
    for part in parts:
        assert 0 < part.stat().st_size <= 24_000
        subprocess.run([ffmpeg, "-v", "error", "-xerror", "-i", str(part), "-f", "null", "-"], check=True)
        duration += float(
            subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(part),
                ]
            )
        )
    assert duration == pytest.approx(5.0, abs=0.15)


def test_bot_sends_playable_parts_in_order_and_ignores_unauthorized_accounts(tmp_path):
    parts = [tmp_path / "first.mp3", tmp_path / "second.mp3"]
    for part in parts:
        part.write_bytes(b"audio fixture")
    calls = []

    def telegram(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if calls[-1] == "sendAudio":
            calls.append(request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": {}})

    def audio(url, directory):
        assert url == "https://youtu.be/example"
        assert directory.is_dir()
        return "Пример", parts

    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        api = TelegramAPI("42:fake-token", client)
        message = {
            "chat": {"type": "private", "id": 123},
            "from": {"id": 123},
            "text": "https://youtu.be/example",
        }
        handle_message(api, message, {456}, audio)
        assert calls == []
        handle_message(api, message, {123}, audio)
        assert calls[0] == "sendMessage"
        assert calls[1] == calls[3] == "sendAudio"
        assert 'filename="first.mp3"' in calls[2] and "Часть 1/2" in calls[2]
        assert 'filename="second.mp3"' in calls[4] and "Часть 2/2" in calls[4]


def test_id_command_can_bootstrap_access_without_enabling_downloads():
    def telegram(request):
        assert json.loads(request.read()) == {"chat_id": 123, "text": "Ваш Telegram ID: 123"}
        return httpx.Response(200, json={"ok": True, "result": {}})

    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        handle_message(
            TelegramAPI("42:fake-token", client),
            {"chat": {"type": "private", "id": 123}, "from": {"id": 123}, "text": "/id"},
            set(),
            lambda *_: pytest.fail("/id must not start a download"),
        )
