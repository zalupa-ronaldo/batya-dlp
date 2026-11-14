"""A private Telegram bot that replies to video links with playable MP3s."""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import httpx

from batya.telegram_media import MAX_AUDIO_BYTES, prepare_audio, video_url

LOG = logging.getLogger(__name__)
AudioSource = Callable[[str, Path], tuple[str, list[Path]]]


class TelegramError(Exception):
    def __init__(self, code: int):
        super().__init__(f"Telegram API: {code}")
        self.code = code


class TelegramAPI:
    def __init__(self, token: str, client: httpx.Client):
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client

    def call(self, method: str, fields: dict, audio: Path | None = None):
        for attempt in range(3):
            try:
                if audio is None:
                    response = self.client.post(f"{self.base}/{method}", json=fields)
                else:
                    with audio.open("rb") as stream:
                        response = self.client.post(
                            f"{self.base}/{method}",
                            data=fields,
                            files={"audio": (audio.name, stream, "audio/mpeg")},
                        )
                data = response.json()
                if data.get("ok"):
                    return data["result"]
                code = data.get("error_code", response.status_code)
            except (httpx.HTTPError, ValueError):
                # httpx exceptions may include the request URL, which contains the bot token.
                raise TelegramError(0) from None
            if code != 429 or attempt == 2:
                raise TelegramError(code)
            retry_after = data.get("parameters", {}).get("retry_after", 1)
            if not isinstance(retry_after, int) or not 0 <= retry_after <= 60:
                raise TelegramError(429)
            time.sleep(retry_after)

    def message(self, chat: int, text: str):
        return self.call("sendMessage", {"chat_id": chat, "text": text})

    def audio(self, chat: int, path: Path, title: str):
        if not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise ValueError("Размер MP3 превышает лимит Telegram.")
        return self.call("sendAudio", {"chat_id": chat, "title": title}, path)


def handle_message(api: TelegramAPI, message: dict, allowed: set[int], source: AudioSource):
    chat = message.get("chat", {})
    user = message.get("from", {}).get("id")
    if chat.get("type") != "private" or user is None:
        return
    chat_id = chat["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    if text == "/id":
        api.message(chat_id, f"Ваш Telegram ID: {user}")
        return
    if user not in allowed:
        if text == "/start":
            api.message(chat_id, "Чтобы получить доступ, отправьте /id и передайте номер владельцу бота.")
        return
    if text in {"/start", "/help"}:
        api.message(chat_id, "Пришлите ссылку на видео. Я пришлю MP3. Длинную запись отправлю по частям.")
        return
    if not text:
        return
    try:
        url = video_url(text)
    except ValueError as exc:
        api.message(chat_id, str(exc))
        return
    api.message(chat_id, "Готовлю MP3. Длинная запись может занять несколько минут.")
    sent = total = 0
    try:
        with tempfile.TemporaryDirectory(prefix="batya-telegram-") as directory:
            title, parts = source(url, Path(directory))
            total = len(parts)
            for index, part in enumerate(parts, 1):
                label = f"Часть {index}/{total} · {title}" if total > 1 else title
                api.audio(chat_id, part, label)
                sent += 1
    except TelegramError:
        progress = f" Успел отправить {sent} из {total}." if total else ""
        api.message(chat_id, "Telegram не подтвердил отправку аудио." + progress)
    except ValueError as exc:
        api.message(chat_id, str(exc))
    except subprocess.TimeoutExpired:
        api.message(chat_id, "Запись обрабатывается слишком долго. Попробуйте более короткое видео.")
    except OSError:
        api.message(chat_id, "Не удалось обработать аудио на компьютере с ботом.")


def poll(api: TelegramAPI, allowed: set[int], source: AudioSource):
    offset = 0
    while True:
        try:
            updates = api.call(
                "getUpdates", {"offset": offset, "limit": 1, "timeout": 30, "allowed_updates": ["message"]}
            )
        except TelegramError as exc:
            if exc.code in {401, 404, 409}:
                raise
            LOG.warning("Ошибка получения сообщений (%s). Повторяем через 5 секунд.", exc.code)
            time.sleep(5)
            continue
        for update in updates:
            try:
                if "message" in update:
                    handle_message(api, update["message"], allowed, source)
            except TelegramError as exc:
                LOG.warning("Telegram не подтвердил ответ (%s).", exc.code)
            offset = update["update_id"] + 1


def main():
    parser = argparse.ArgumentParser(description="Батя качает — Telegram бот для MP3")
    parser.add_argument("--config", type=Path, help="Путь к telegram.local.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        token = os.environ.get("BATYA_TELEGRAM_TOKEN") or config.get("token", "")
        if not isinstance(token, str) or not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Укажите токен BotFather в telegram.local.json или BATYA_TELEGRAM_TOKEN.")
        ids = os.getenv("BATYA_TELEGRAM_ALLOWED_USERS")
        allowed = {int(item) for item in ids.split(",")} if ids else set(config.get("allowed_users", []))
        if any(type(item) is not int or item <= 0 for item in allowed):
            raise ValueError("allowed_users должен содержать числовые Telegram ID.")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        # Never print raw configuration values or parsing exceptions containing them.
        LOG.error(
            "Проверьте файл настроек, токен BotFather и список числовых Telegram ID (%s).", type(exc).__name__
        )
        return 2
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        LOG.error("Для бота нужны ffmpeg и ffprobe в PATH.")
        return 2
    source = partial(prepare_audio, ffmpeg=ffmpeg, ffprobe=ffprobe)
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), trust_env=False) as client:
            api = TelegramAPI(token, client)
            me = api.call("getMe", {})
            print(f"Бот запущен: https://t.me/{me['username']}", flush=True)
            if not allowed:
                print("Список доступа пуст. Отправьте /id, чтобы узнать Telegram ID.", flush=True)
            poll(api, allowed, source)
    except KeyboardInterrupt:
        return 0
    except TelegramError as exc:
        LOG.error(
            "Ошибка подключения к Telegram (%s). Проверьте токен, сеть и другие запуски бота.", exc.code
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
