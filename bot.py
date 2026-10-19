import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API_URL = "https://api.rule34.xxx/index.php"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
RULE34_API_KEY = os.environ["RULE34_API_KEY"]
RULE34_USER_ID = os.environ["RULE34_USER_ID"]
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()

STATE_FILE = Path("state.json")
MAX_FILE_BYTES = 50 * 1024 * 1024
TAGS = "valorant rating:explicit"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ValoArtsBot/1.0"})


def load_state():
    if not STATE_FILE.exists():
        return {"initialized": False, "sent_ids": []}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        state = json.load(file)

    if not isinstance(state, dict):
        raise RuntimeError("Invalid state.json")

    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

    temporary.replace(STATE_FILE)


def get_posts():
    # Rule34 иногда возвращает пустой ответ даже при HTTP 200.
    # Повторяем запрос несколько раз, прежде чем считать запуск неудачным.
    last_error = None

    for attempt in range(1, 5):
        try:
            response = SESSION.get(
                API_URL,
                params={
                    "page": "dapi",
                    "s": "post",
                    "q": "index",
                    "json": "1",
                    "limit": "1000",
                    "pid": "0",
                    "tags": TAGS,
                    "api_key": RULE34_API_KEY,
                    "user_id": RULE34_USER_ID,
                },
                timeout=40,
            )
            response.raise_for_status()

            if not response.text.strip():
                raise RuntimeError(
                    f"Rule34 API вернул пустой ответ "
                    f"(HTTP {response.status_code})"
                )

            try:
                data = response.json()
            except ValueError as error:
                raise RuntimeError(
                    "Rule34 API вернул не JSON. "
                    f"HTTP: {response.status_code}; "
                    f"Content-Type: {response.headers.get('Content-Type')}; "
                    f"ответ: {response.text[:200]!r}"
                ) from error

            break

        except (requests.RequestException, RuntimeError) as error:
            last_error = error
            print(
                f"Rule34 API attempt {attempt}/4 failed: {error}"
            )

            if attempt < 4:
                time.sleep(attempt * 10)
    else:
        raise RuntimeError(
            f"Не удалось получить данные Rule34 API после 4 попыток: "
            f"{last_error}"
        )

    if isinstance(data, dict):
        if data.get("success") is False:
            raise RuntimeError(
                "Ошибка API Rule34: "
                + str(data.get("message", data.get("error", "без описания")))
            )

        data = data.get("post", data.get("posts", data.get("data", [])))

    if isinstance(data, dict):
        if "id" in data:
            data = [data]
        else:
            raise RuntimeError(
                f"Неизвестная структура JSON: {list(data.keys())}"
            )

    if data is None:
        data = []

    if not isinstance(data, list):
        raise RuntimeError(
            f"Неожиданный тип ответа Rule34: {type(data).__name__}"
        )

    results = []

    for post in data:
        if not isinstance(post, dict):
            continue

        rating = str(post.get("rating", "")).lower()
        tags = set(str(post.get("tags", "")).lower().split())

        if rating not in ("e", "explicit"):
            continue

        if "valorant" not in tags:
            continue

        if not post.get("id") or not post.get("file_url"):
            continue

        results.append(post)

    return sorted(results, key=lambda post: int(post["id"]))


def send_media(method, field_name, file_data, filename, caption):
    response = SESSION.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data={
            "chat_id": CHANNEL_ID,
            "caption": caption,
            "parse_mode": "HTML",
        },
        files={
            field_name: (filename, file_data),
        },
        timeout=90,
    )

    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Telegram вернул некорректный ответ")

    if not response.ok or not result.get("ok"):
        description = result.get(
            "description", "Неизвестная ошибка Telegram"
        )
        raise RuntimeError(
            f"Telegram error {response.status_code}, "
            f"method={method}, file={filename}: {description}"
        )


def publish(post):
    post_id = str(post["id"])
    original_url = post["file_url"]
    original_extension = Path(urlparse(original_url).path).suffix.lower()

    # For still images, use Rule34's sample_url when available.
    # It is typically a smaller, web-friendly JPG than the full-resolution file.
    # Keep the original file for GIFs and videos so animation is not lost.
    media_url = original_url
    if original_extension in (".jpg", ".jpeg", ".png"):
        sample_url = post.get("sample_url")
        sample_extension = (
            Path(urlparse(sample_url).path).suffix.lower()
            if sample_url
            else ""
        )
        if sample_url and sample_extension in (".jpg", ".jpeg", ".png"):
            media_url = sample_url

    extension = Path(urlparse(media_url).path).suffix.lower()

    response = SESSION.get(media_url, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    file_data = response.content

    if len(file_data) > MAX_FILE_BYTES:
        print(f"SKIP oversized file: {post_id}")
        return False

    if extension in (".jpg", ".jpeg", ".png"):
        if not content_type.startswith("image/"):
            print(f"SKIP invalid image: {post_id}")
            return False

        # Telegram sendPhoto has a 10 MB limit. Larger images can be sent
        # as documents (within the bot's document upload limit).
        if len(file_data) > 10 * 1024 * 1024:
            method = "sendDocument"
            field_name = "document"
        else:
            method = "sendPhoto"
            field_name = "photo"

    elif extension == ".gif" or content_type == "image/gif":
        if file_data[:6] not in (b"GIF87a", b"GIF89a"):
            print(f"SKIP invalid GIF: {post_id}")
            return False
        method = "sendAnimation"
        field_name = "animation"
        extension = ".gif"

    elif extension == ".mp4" or content_type.startswith("video/mp4"):
        if extension != ".mp4" and file_data[4:8] != b"ftyp":
            print(f"SKIP invalid MP4: {post_id}")
            return False
        method = "sendVideo"
        field_name = "video"
        extension = ".mp4"

    else:
        print(
            f"SKIP unsupported format: {post_id} "
            f"({extension}, {content_type})"
        )
        return False

    caption = '<a href="https://t.me/valorantnsfv">R34 Valorant</a>'

    send_media(
        method,
        field_name,
        file_data,
        f"valorant_{post_id}{extension}",
        caption,
    )

    print(f"PUBLISHED: {post_id} ({method})")
    return True


def send_message(chat_id, message_text):
    response = SESSION.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": chat_id, "text": message_text},
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def process_test_commands(state):
    stop_auto_publish = False
    if not ADMIN_ID:
        print("Test commands disabled: TELEGRAM_ADMIN_ID is missing")
        return

    offset = int(state.get("last_update_id", 0)) + 1

    try:
        response = SESSION.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={
                "offset": offset,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=30,
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram getUpdates error: {result}")

    except Exception as error:
        print(f"Could not check Telegram commands: {error}")
        print(
            "If a webhook is configured for this bot, "
            "getUpdates cannot be used until the webhook is removed."
        )
        return

    for update in result.get("result", []):
        state["last_update_id"] = update["update_id"]

        message = update.get("message", {})
        command_text = message.get("text", "").strip()
        chat = message.get("chat", {})
        sender = message.get("from", {})

        if str(sender.get("id", "")) != ADMIN_ID:
            continue

        supported_commands = (
            "/help",
            "/start",
            "/testgif",
            "/testvideo",
            "/testimage",
            "/test10",
            "/add1",
            "/add10",
            "/add50",
            "/addv1",
            "/addv10",
        )
        if command_text not in supported_commands:
            continue

        chat_id = chat.get("id")

        if chat.get("type") != "private":
            print("Test command ignored: use a private chat with the bot")
            continue

        if command_text in ("/help", "/start"):
            help_text = (
                "📋 Команды ValoArtsBot\\n\\n"
                "/help — Показать список команд\\n"
                "/add1 — Опубликовать 1 старый пост, которого ещё нет в канале\\n"
                "/add10 — Опубликовать 10 старых постов, которых ещё нет в канале\\n"
                "/add50 — Опубликовать 50 старых постов, которых ещё нет в канале\\n"
                "/addv1 — Опубликовать 1 старое видео MP4, которого ещё нет в канале\\n"
                "/addv10 — Опубликовать 10 старых видео MP4, которых ещё нет в канале\\n\\n"
                "🧪 Тестовые команды (публикуют последние доступные файлы):\\n"
                "/testimage — Последняя доступная картинка\\n"
                "/testgif — Последняя доступная GIF-анимация\\n"
                "/testvideo — Последнее доступное видео\\n"
                "/test10 — Последние 10 доступных постов\\n\\n"
                "Команды доступны только администратору."
            )
            send_message(chat_id, help_text)
            continue

        try:
            posts = get_posts()
            sent_ids = set(str(item) for item in state.get("sent_ids", []))

            # /add-команды публикуют только очередь новее watermark, то есть
            # посты, которые обычный цикл бота ещё не обработал. Если бот ещё
            # не инициализирован, доступен только самый новый пост — как при
            # первом обычном запуске.
            if command_text in ("/add1", "/add10", "/add50", "/addv1", "/addv10"):
                stop_auto_publish = True
                limit = {
                    "/add1": 1,
                    "/add10": 10,
                    "/add50": 50,
                    "/addv1": 1,
                    "/addv10": 10,
                }[command_text]
                videos_only = command_text in ("/addv1", "/addv10")
                supported_extensions = {".jpg", ".jpeg", ".png", ".gif", ".mp4"}
                last_post_id = state.get("last_post_id")

                if last_post_id is None:
                    pending = [posts[-1]] if posts and str(posts[-1]["id"]) not in sent_ids else []
                else:
                    pending = [
                        post for post in posts
                        if int(post["id"]) > int(last_post_id)
                        and str(post["id"]) not in sent_ids
                    ]

                candidates = []
                for post in pending:
                    extension = Path(urlparse(post["file_url"]).path).suffix.lower()
                    if extension not in supported_extensions:
                        continue
                    if videos_only and extension != ".mp4":
                        continue
                    candidates.append(post)

                candidates.sort(key=lambda post: int(post["id"]))
                candidates = candidates[:limit]

                if not candidates:
                    send_message(
                        chat_id,
                        "Нет новых неопубликованных подходящих постов в доступной очереди."
                    )
                    continue

                label = "видео" if videos_only else "медиа"
                send_message(
                    chat_id,
                    f"Начинаю публикацию: {len(candidates)} {label}."
                )
                successes = []
                failures = []

                for add_post in candidates:
                    add_id = str(add_post["id"])
                    try:
                        published = publish(add_post)
                        if published:
                            successes.append(add_id)
                            sent_ids.add(add_id)
                            state["sent_ids"] = sorted(
                                sent_ids, key=lambda value: int(value)
                            )
                            save_state(state)
                        else:
                            failures.append(f"{add_id} (формат пропущен)")
                    except Exception as error:
                        print(f"Add post {add_id} failed: {error}")
                        failures.append(f"{add_id} (ошибка отправки)")
                    time.sleep(1)

                summary = (
                    f"Готово. Опубликовано: {len(successes)} из {len(candidates)}; "
                    f"ошибок/пропусков: {len(failures)}."
                )
                if successes:
                    summary += "\nID публикаций: " + ", ".join(successes)
                if failures:
                    summary += "\nНе удалось: " + ", ".join(failures)
                send_message(chat_id, summary)
                continue

            if command_text == "/test10":
                supported_extensions = {
                    ".jpg", ".jpeg", ".png", ".gif", ".mp4"
                }
                candidates = [
                    post
                    for post in reversed(posts)
                    if Path(urlparse(post["file_url"]).path).suffix.lower()
                    in supported_extensions
                ][:10]

                if not candidates:
                    send_message(
                        chat_id,
                        "Не нашёл подходящих изображений, GIF или видео для теста."
                    )
                    continue

                successes = []
                failures = []

                send_message(
                    chat_id,
                    f"Начинаю тест: попробую опубликовать {len(candidates)} постов."
                )

                for test_post in candidates:
                    test_id = str(test_post["id"])
                    try:
                        published = publish(test_post)
                        if published:
                            successes.append(test_id)
                            sent_ids.add(test_id)
                            state["sent_ids"] = sorted(
                                sent_ids, key=lambda value: int(value)
                            )
                            save_state(state)
                        else:
                            failures.append(f"{test_id} (формат пропущен)")
                    except Exception as error:
                        print(f"Test post {test_id} failed: {error}")
                        failures.append(f"{test_id} (ошибка отправки)")
                    time.sleep(1)

                summary = (
                    f"Тест 10 постов завершён. Успешно: {len(successes)}, "
                    f"ошибок/пропусков: {len(failures)}."
                )
                if successes:
                    summary += "\nОпубликованные ID: " + ", ".join(successes)
                if failures:
                    summary += "\nНе удалось: " + ", ".join(failures)
                send_message(chat_id, summary)
                continue

            if command_text == "/testimage":
                image_extensions = {".jpg", ".jpeg", ".png"}
                candidates = [
                    post
                    for post in reversed(posts)
                    if (
                        Path(urlparse(post["file_url"]).path).suffix.lower()
                        in image_extensions
                        or Path(
                            urlparse(post.get("sample_url", "")).path
                        ).suffix.lower() in image_extensions
                    )
                ]
                label = "изображение"
            else:
                extension = ".gif" if command_text == "/testgif" else ".mp4"
                label = "GIF" if extension == ".gif" else "видео"
                candidates = [
                    post
                    for post in reversed(posts)
                    if Path(urlparse(post["file_url"]).path).suffix.lower()
                    == extension
                ]

            if not candidates:
                send_message(
                    chat_id,
                    f"Не нашёл файл типа «{label}» среди доступных результатов API."
                )
                continue

            test_post = candidates[0]
            test_id = str(test_post["id"])
            published = publish(test_post)

            if published:
                sent_ids.add(test_id)
                state["sent_ids"] = sorted(
                    sent_ids, key=lambda value: int(value)
                )
                save_state(state)
                send_message(
                    chat_id,
                    f"Тест успешен: {label} опубликовано в канале (ID {test_id})."
                )
            else:
                send_message(
                    chat_id,
                    f"Не удалось опубликовать выбранный файл ({label})."
                )

        except Exception as error:
            print(f"Test command failed: {error}")
            try:
                send_message(
                    chat_id,
                    f"Тест не пройден: {str(error)[:300]}"
                )
            except Exception as message_error:
                print(f"Could not send test result: {message_error}")

        finally:
            save_state(state)

    save_state(state)
    return stop_auto_publish


def main():
    state = load_state()

    # Проверяем команды администратора перед обработкой новых публикаций.
    # Если была команда /add, в этом запуске не запускаем автопубликацию,
    # иначе бот мог бы выложить больше постов, чем запросил администратор.
    if process_test_commands(state):
        print("Manual /add command handled; skipping scheduled publishing this run.")
        return

    posts = get_posts()
    if not posts:
        print("No matching posts returned by Rule34 API.")
        return

    latest_post = posts[-1]
    latest_id = int(latest_post["id"])

    # При первом запуске новой логики публикуем только самый новый
    # доступный пост и сохраняем его ID как точку отсчёта.
    # Это также работает со старым state.json, в котором ещё нет last_post_id.
    if state.get("last_post_id") is None:
        post_id = str(latest_post["id"])
        sent_ids = set(str(item) for item in state.get("sent_ids", []))

        if post_id in sent_ids:
            state["last_post_id"] = post_id
            state["initialized"] = True
            save_state(state)
            print(
                f"Initial latest post {post_id} was already published by a test command; "
                "saved it as the starting point."
            )
            return

        print(f"First run: publishing latest available post {post_id}")

        try:
            publish(latest_post)
        except Exception as error:
            # Не сохраняем watermark при ошибке: следующий запуск попробует снова.
            print(f"FAILED initial post {post_id}: {error}")
            return

        state["last_post_id"] = post_id
        state["initialized"] = True
        sent_ids = set(str(item) for item in state.get("sent_ids", []))
        sent_ids.add(post_id)
        state["sent_ids"] = sorted(sent_ids, key=lambda value: int(value))
        save_state(state)
        print(f"Initial latest post saved: {post_id}")
        return

    last_post_id = int(state["last_post_id"])

    # На следующих запусках берём только посты с ID выше сохранённого.
    new_posts = [
        post for post in posts
        if int(post["id"]) > last_post_id
    ]
    new_posts.sort(key=lambda post: int(post["id"]))

    if not new_posts:
        print(
            f"No new posts. Latest available ID: {latest_id}; "
            f"last published/processed ID: {last_post_id}"
        )
        return

    print(
        f"Found {len(new_posts)} new post(s), "
        f"IDs {new_posts[0]['id']}–{new_posts[-1]['id']}"
    )

    sent_ids = set(str(item) for item in state.get("sent_ids", []))

    for post in new_posts:
        post_id = str(post["id"])

        if post_id in sent_ids:
            state["last_post_id"] = post_id
            state["initialized"] = True
            save_state(state)
            print(f"SKIP already published by test command: {post_id}")
            continue

        try:
            published = publish(post)
        except Exception as error:
            # Останавливаемся на ошибке, чтобы не перепрыгнуть через этот ID.
            # Следующий запуск повторит попытку с него.
            print(f"FAILED post {post_id}: {error}")
            break

        # Обновляем watermark и для опубликованного поста, и для пропущенного
        # неподдерживаемого/слишком большого файла, чтобы не зациклиться на нём.
        state["last_post_id"] = post_id
        sent_ids.add(post_id)
        state["sent_ids"] = sorted(
            sent_ids, key=lambda value: int(value)
        )
        state["initialized"] = True
        save_state(state)

        time.sleep(1)

    print(
        f"Run completed. Last processed post ID: "
        f"{state.get('last_post_id')}"
    )


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
if __name__ == "__main__":
    main()
