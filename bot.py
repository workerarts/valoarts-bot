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
        return {
            "initialized": False,
            "sent_ids": [],
            "auto_paused": False,
            "bot_enabled": True,
            "command_activity": 0,
            "idle_sleep_notified": False,
        }

    with STATE_FILE.open("r", encoding="utf-8") as file:
        state = json.load(file)

    if not isinstance(state, dict):
        raise RuntimeError("Invalid state.json")

    # Backward-compatible defaults for new control commands.
    state.setdefault("auto_paused", False)
    state.setdefault("bot_enabled", True)
    state.setdefault("command_activity", 0)
    state.setdefault("idle_sleep_notified", False)
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


def publish(post, state=None):
    post_id = str(post["id"])

    # Central duplicate guard: every publication path (automatic, /add and
    # test commands) checks the persistent sent_ids list before sending.
    if state is not None:
        sent_ids = {str(item) for item in state.get("sent_ids", [])}
        if post_id in sent_ids:
            print(f"SKIP duplicate post: {post_id}")
            return None
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

    # Save the ID immediately after Telegram confirms a successful send.
    # This minimizes the chance of reposting if the process stops mid-batch.
    if state is not None:
        sent_ids = {str(item) for item in state.get("sent_ids", [])}
        sent_ids.add(post_id)
        state["sent_ids"] = sorted(sent_ids, key=lambda value: int(value))
        save_state(state)

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


def notify_admin_error(state, context, error):
    """Notify the administrator once per distinct error to avoid spam."""
    details = f"{type(error).__name__}: {error}"
    fingerprint = details[:500]
    print(f"ERROR [{context}]: {details}")

    if not ADMIN_ID:
        print("Error notification skipped: TELEGRAM_ADMIN_ID is missing")
        return

    if state.get("last_notified_error") == fingerprint:
        print("Same error was already reported; suppressing duplicate notification")
        return

    message = (
        "⚠️ Ошибка ValoArtsBot\n\n"
        f"Этап: {context}\n"
        f"Ошибка: {details[:700]}"
    )

    try:
        send_message(ADMIN_ID, message)
        if not state.get("_do_not_save_state"):
            state["last_notified_error"] = fingerprint
            save_state(state)
        print("Error notification sent to administrator")
    except Exception as notify_error:
        # If Telegram itself is unavailable, there is no way to deliver a
        # Telegram alert; keep the original error visible in the Actions log.
        print(f"Could not send error notification: {notify_error}")


def poll_delay_commands(state):
    """Check for delay commands during long publishing batches."""
    if not ADMIN_ID:
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
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram getUpdates error: {result}")
    except Exception as error:
        print(f"Could not poll delay commands during publishing: {error}")
        return

    delay_commands = {"/setdelay1": 1, "/setdelay3": 3, "/setdelay5": 5}
    pending = state.setdefault("pending_updates", [])

    updates = result.get("result", [])

    for update in updates:
        state["last_update_id"] = max(
            int(state.get("last_update_id", 0)), int(update["update_id"])
        )
        message = update.get("message", {})
        text = (message.get("text") or "").strip()
        parts = text.split()
        command = parts[0].split("@", 1)[0] if parts else ""
        chat = message.get("chat", {})
        sender = message.get("from", {})
        chat_id = chat.get("id")

        is_admin_private = (
            str(sender.get("id", "")) == ADMIN_ID
            and chat.get("type") == "private"
        )

        if is_admin_private and command in delay_commands and len(parts) == 1:
            state["post_delay"] = delay_commands[command]
            save_state(state)
            try:
                send_message(
                    chat_id,
                    f"✅ Интервал изменён! Теперь между публикациями {state['post_delay']} сек. Настройка сохранена."
                )
            except Exception as error:
                print(f"Could not send delay confirmation: {error}")
        elif is_admin_private and command == "/setdelay" and len(parts) == 1:
            current_delay = int(state.get("post_delay", 5))
            try:
                send_message(
                    chat_id,
                    f"Текущий интервал между публикациями: {current_delay} сек."
                )
            except Exception as error:
                print(f"Could not send current delay: {error}")
        else:
            # Preserve other commands so they can be handled normally later.
            pending.append(update)

    pending.sort(key=lambda item: item.get("update_id", 0))
    save_state(state)


def sleep_with_command_polling(state, seconds):
    """Wait between posts while servicing administrator commands.

    Returns True when an admin command asks the automatic publishing loop to
    stop (for example /pause or a manual /add command).
    """
    elapsed = 0
    while elapsed < max(1, int(state.get("post_delay", seconds))):
        should_stop = process_test_commands(state)
        if should_stop or state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return True
        current_delay = max(1, int(state.get("post_delay", seconds)))
        if elapsed >= current_delay:
            break
        time.sleep(1)
        elapsed += 1
    return False


def wait_while_paused(state, idle_timeout=60):
    """While auto publishing is paused, handle admin commands for up to
    idle_timeout seconds after the most recent command, then let this run exit.
    """
    print(f"Auto publishing paused; waiting up to {idle_timeout}s for commands.")
    idle_seconds = 0
    while state.get("auto_paused", False) and idle_seconds < idle_timeout:
        previous_activity = int(state.get("command_activity", 0))
        process_test_commands(state)

        if not state.get("auto_paused", False):
            print("Pause cleared; automatic publishing can resume.")
            return

        current_activity = int(state.get("command_activity", 0))
        if current_activity != previous_activity:
            # Commands such as /add10 can take time; reset the timer after the
            # command has completely finished processing.
            idle_seconds = 0
        else:
            time.sleep(1)
            idle_seconds += 1

    if state.get("auto_paused", False):
        print("No administrator commands for the idle timeout; ending this run.")
        if not state.get("idle_sleep_notified", False) and ADMIN_ID:
            try:
                send_message(
                    ADMIN_ID,
                    "🌙 ValoArtsBot переходит в режим ожидания\n"
                    "⏸️ Автопубликация приостановлена.\n"
                    "💤 Команд за последнюю минуту не поступило.\n"
                    "🔄 Следующая проверка — при следующем запуске GitHub Actions.",
                )
                state["idle_sleep_notified"] = True
                save_state(state)
            except Exception as error:
                print(f"Could not send idle/waiting notification: {error}")


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
        notify_admin_error(state, "Проверка команд Telegram", error)
        print(
            "If a webhook is configured for this bot, "
            "getUpdates cannot be used until the webhook is removed."
        )
        return

    updates = state.pop("pending_updates", []) + result.get("result", [])
    updates.sort(key=lambda item: item.get("update_id", 0))

    for update in updates:
        state["last_update_id"] = max(
            int(state.get("last_update_id", 0)), int(update["update_id"])
        )

        message = update.get("message", {})
        raw_command_text = message.get("text", "").strip()
        command_parts = raw_command_text.split()
        command_text = command_parts[0].split("@", 1)[0] if command_parts else ""
        command_args = command_parts[1:] if command_parts else []
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
            "/setdelay",
            "/setdelay1",
            "/setdelay3",
            "/setdelay5",
            "/pause",
            "/resume",
            "/shutdown",
            "/restart",
            "/status",
        )
        if command_text not in supported_commands:
            continue

        chat_id = chat.get("id")

        if chat.get("type") != "private":
            print("Test command ignored: use a private chat with the bot")
            continue

        # Activity marker lets the paused runner restart its 60-second idle timer
        # after every valid administrator command.
        state["command_activity"] = int(state.get("command_activity", 0)) + 1
        # A new admin command starts a fresh idle period and allows the next
        # waiting notification to be sent after another full 60 seconds.
        state["idle_sleep_notified"] = False

        if command_text == "/shutdown":
            state["bot_enabled"] = False
            state["auto_paused"] = True
            save_state(state)
            send_message(
                chat_id,
                "🛑 ValoArtsBot выключен: автопубликация остановлена.\n"
                "Команда /start снова включит бота. GitHub Actions продолжит запускаться по расписанию, "
                "чтобы бот мог получить команду включения."
            )
            stop_auto_publish = True
            continue

        if command_text in ("/start", "/restart"):
            was_disabled = not bool(state.get("bot_enabled", True))
            state["bot_enabled"] = True
            state["auto_paused"] = False
            save_state(state)
            if command_text == "/restart":
                send_message(
                    chat_id,
                    "🔄 Команда перезапуска принята. Бот снова активен; новый цикл автопубликации "
                    "начнётся сейчас или при следующем запуске GitHub Actions. Состояние и история публикаций сохранены."
                )
                stop_auto_publish = True
            elif was_disabled:
                send_message(chat_id, "🟢 ValoArtsBot включён. Автопубликация возобновится.")
            else:
                send_message(chat_id, "🟢 ValoArtsBot активен.\nИспользуй /help для списка команд.")
            continue

        if command_text in ("/pause", "/resume", "/status"):
            if command_text == "/pause":
                state["auto_paused"] = True
                save_state(state)
                send_message(
                    chat_id,
                    "⏸ Автопубликация приостановлена. Ручные команды продолжают работать. "
                    "Бот будет ждать команды до 60 секунд после последней команды."
                )
                stop_auto_publish = True
            elif command_text == "/resume":
                state["bot_enabled"] = True
                state["auto_paused"] = False
                save_state(state)
                send_message(chat_id, "▶️ Автопубликация возобновлена; бот включён.")
            else:
                paused = bool(state.get("auto_paused", False))
                enabled = bool(state.get("bot_enabled", True))
                current_delay = int(state.get("post_delay", 5))
                status = "выключен" if not enabled else ("приостановлена" if paused else "включена")
                send_message(
                    chat_id,
                    f"Статус бота/автопубликации: {status}.\n"
                    f"Интервал между публикациями: {current_delay} сек."
                )
            save_state(state)
            continue

        if command_text == "/help":
            help_text = (
                "📋 Команды ValoArtsBot\n\n"
                "/help — Показать список команд\n"
                "/add1 — Опубликовать 1 старый пост, которого ещё нет в канале\n"
                "/add10 — Опубликовать 10 старых постов, которых ещё нет в канале\n"
                "/add50 — Опубликовать 50 старых постов, которых ещё нет в канале\n"
                "/addv1 — Опубликовать 1 старое видео MP4, которого ещё нет в канале\n"
                "/addv10 — Опубликовать 10 старых видео MP4, которого ещё нет в канале\n"
                "/setdelay1 — Интервал 1 секунда между публикациями\n"
                "/setdelay3 — Интервал 3 секунды между публикациями\n"
                "/setdelay5 — Интервал 5 секунд между публикациями\n"
                "/setdelay — Показать текущий интервал\n"
                "/pause — Приостановить автопубликацию\n"
                "/resume — Возобновить автопубликацию\n"
                "/shutdown — Выключить бота (остановить публикации)\n"
                "/start — Включить бота после выключения\n"
                "/restart — Перезапустить цикл бота без сброса истории\n"
                "/status — Показать статус бота\n\n"
                "🧪 Тестовые команды (публикуют последние доступные файлы):\n"
                "/testimage — Последняя доступная картинка\n"
                "/testgif — Последняя доступная GIF-анимация\n"
                "/testvideo — Последнее доступное видео\n"
                "/test10 — Последние 10 доступных постов\n\n"
                "Команды доступны только администратору."
            )
            send_message(chat_id, help_text)
            continue

        if command_text == "/setdelay":
            current_delay = int(state.get("post_delay", 5))
            send_message(
                chat_id,
                f"Текущий интервал между публикациями: {current_delay} сек."
            )
            continue

        delay_commands = {
            "/setdelay1": 1,
            "/setdelay3": 3,
            "/setdelay5": 5,
        }
        if command_text in delay_commands:
            state["post_delay"] = delay_commands[command_text]
            save_state(state)
            send_message(
                chat_id,
                f"✅ Интервал изменён! Теперь между публикациями {state['post_delay']} сек. Настройка сохранена."
            )
            continue

        try:
            posts = get_posts()
            sent_ids = set(str(item) for item in state.get("sent_ids", []))

            # Ручные /add-команды не зависят от last_post_id:
            # выбирают старые посты из доступной выборки Rule34, которых
            # ещё нет в sent_ids. Автоматическая публикация использует watermark
            # отдельно, ниже в main().
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

                pending = [
                    post for post in posts
                    if str(post["id"]) not in sent_ids
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
                        "Не найдено подходящих постов, которых ещё нет в sent_ids, в доступной выборке Rule34."
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
                    sent_ids = set(str(item) for item in state.get("sent_ids", []))
                    add_id = str(add_post["id"])
                    if add_id in sent_ids:
                        continue
                    try:
                        published = publish(add_post, state)
                        if published is True:
                            successes.append(add_id)
                        elif published is None:
                            print(f"Add post {add_id} skipped: already published")
                        else:
                            failures.append(f"{add_id} (формат пропущен)")
                    except Exception as error:
                        notify_admin_error(state, f"Ручная публикация поста ID {add_id}", error)
                        failures.append(f"{add_id} (ошибка отправки)")
                    sleep_with_command_polling(state, max(1, int(state.get("post_delay", 5))))

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
                sent_ids = {str(item) for item in state.get("sent_ids", [])}
                candidates = [
                    post
                    for post in reversed(posts)
                    if str(post["id"]) not in sent_ids
                    and Path(urlparse(post["file_url"]).path).suffix.lower()
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
                    sent_ids = set(str(item) for item in state.get("sent_ids", []))
                    test_id = str(test_post["id"])
                    try:
                        published = publish(test_post, state)
                        if published is True:
                            successes.append(test_id)
                        elif published is None:
                            print(f"Test post {test_id} skipped: already published")
                        else:
                            failures.append(f"{test_id} (формат пропущен)")
                    except Exception as error:
                        notify_admin_error(state, f"Тестовая публикация поста ID {test_id}", error)
                        failures.append(f"{test_id} (ошибка отправки)")
                    sleep_with_command_polling(state, max(1, int(state.get("post_delay", 5))))

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
                sent_ids = {str(item) for item in state.get("sent_ids", [])}
                candidates = [
                    post
                    for post in reversed(posts)
                    if str(post["id"]) not in sent_ids
                    and (
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
                sent_ids = {str(item) for item in state.get("sent_ids", [])}
                candidates = [
                    post
                    for post in reversed(posts)
                    if str(post["id"]) not in sent_ids
                    and Path(urlparse(post["file_url"]).path).suffix.lower()
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
            published = publish(test_post, state)

            if published is True:
                send_message(
                    chat_id,
                    f"Тест успешен: {label} опубликовано в канале (ID {test_id})."
                )
            elif published is None:
                send_message(
                    chat_id,
                    f"Этот материал уже отмечен как опубликованный (ID {test_id}); повторно не отправлял."
                )
            else:
                send_message(
                    chat_id,
                    f"Не удалось опубликовать выбранный файл ({label})."
                )

            # A command may have arrived while the media was uploading.
            # Process it now, before returning to the previous task.
            process_test_commands(state)

        except Exception as error:
            notify_admin_error(state, f"Обработка команды {command_text}", error)
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

    # Commands are handled before any scheduled publishing.
    stop_requested = process_test_commands(state)
    if not state.get("bot_enabled", True):
        state["auto_paused"] = True
        save_state(state)
    if state.get("auto_paused", False):
        wait_while_paused(state, idle_timeout=60)
        if state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return
    elif stop_requested:
        print("Manual publishing command handled; skipping scheduled publishing this run.")
        return

    posts = get_posts()
    # The API request can take time; handle commands that arrived while it ran.
    stop_requested = process_test_commands(state)
    if not state.get("bot_enabled", True):
        state["auto_paused"] = True
        save_state(state)
    if state.get("auto_paused", False):
        wait_while_paused(state, idle_timeout=60)
        if state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return
    elif stop_requested:
        print("Command handled during API request; skipping scheduled publishing this run.")
        return
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
            publish(latest_post, state)
        except Exception as error:
            # Не сохраняем watermark при ошибке: следующий запуск попробует снова.
            notify_admin_error(state, f"Публикация начального поста ID {post_id}", error)
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
        # A manual command may have published a post while auto-publishing was
        # paused, so refresh the set from shared state before every item.
        sent_ids = set(str(item) for item in state.get("sent_ids", []))
        post_id = str(post["id"])

        if post_id in sent_ids:
            state["last_post_id"] = post_id
            state["initialized"] = True
            save_state(state)
            print(f"SKIP already published by test command: {post_id}")
            continue

        try:
            published = publish(post, state)
        except Exception as error:
            # Останавливаемся на ошибке, чтобы не перепрыгнуть через этот ID.
            # Следующий запуск повторит попытку с него.
            notify_admin_error(state, f"Автопубликация поста ID {post_id}", error)
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

        should_stop = sleep_with_command_polling(
            state, max(1, int(state.get("post_delay", 5)))
        )
        if should_stop or state.get("auto_paused", False) or not state.get("bot_enabled", True):
            print("Stopping automatic publishing because of an administrator command.")
            break

    if state.get("auto_paused", False) or not state.get("bot_enabled", True):
        if not state.get("bot_enabled", True):
            state["auto_paused"] = True
            save_state(state)
        wait_while_paused(state, idle_timeout=60)
        if state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return
        # /resume was received during the paused wait; restart this run so
        # automatic publishing resumes without waiting for the next schedule.
        print("Pause cleared; restarting automatic publishing.")
        return main()

    print(
        f"Run completed. Last processed post ID: "
        f"{state.get('last_post_id')}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Covers uncaught failures such as Rule34 API or state-file errors.
        try:
            current_state = load_state()
        except Exception as state_error:
            print(f"Could not load state while reporting failure: {state_error}")
            current_state = {"_do_not_save_state": True}
        notify_admin_error(current_state, "Критическая ошибка запуска", error)
        raise
