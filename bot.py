import json
import io
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
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

# Finish the publishing cycle before GitHub Actions' 15-minute hard timeout.
# This gives the workflow time to commit state.json.
RUN_STARTED_AT = time.monotonic()
MAX_RUN_SECONDS = max(60, int(os.environ.get("VALOARTS_MAX_RUN_SECONDS", "870")))


def run_deadline_reached():
    return time.monotonic() - RUN_STARTED_AT >= MAX_RUN_SECONDS


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ValoArtsBot/1.0"})


def load_state():
    if not STATE_FILE.exists():
        return {
            "initialized": False,
            "sent_ids": [],
            "auto_paused": False,
            "bot_enabled": True,
            "shutdown_once": False,
            "restart_requested": False,
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
    state.setdefault("shutdown_once", False)
    state.setdefault("restart_requested", False)
    state.setdefault("command_activity", 0)
    state.setdefault("idle_sleep_notified", False)
    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

    temporary.replace(STATE_FILE)


def get_posts(search_tags=TAGS, post_id=None, page=0):
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
                    "pid": str(page),
                    "tags": search_tags if post_id is None else None,
                    "id": post_id,
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



def get_posts_after(last_post_id, max_pages=100):
    """Read Rule34 pages newest-first until the saved cursor is reached.

    Never advance the cursor on an incomplete API lookup. Pages are queried
    sequentially; the existing automatic publisher still handles ordering,
    duplicate detection, and state persistence after each post.
    """
    collected = {}
    previous_page_ids = None
    for page in range(max_pages):
        if run_deadline_reached():
            raise RuntimeError(
                "Лимит времени на поиск страниц достигнут. "
                "Состояние не изменено; продолжим на следующем запуске."
            )
        page_posts = get_posts(page=page)
        if not page_posts:
            # Empty pages may indicate API trouble; only treat an empty page
            # as the end after at least one successful non-empty page.
            if page == 0:
                raise RuntimeError("Rule34 вернул пустую первую страницу")
            print(f"Rule34 pagination finished at page {page} (empty page).")
            break
        page_ids = tuple(sorted(int(p["id"]) for p in page_posts))
        if page_ids == previous_page_ids:
            raise RuntimeError(f"Rule34 repeated page {page}; stopping to avoid skipping posts")
        previous_page_ids = page_ids
        for post in page_posts:
            if int(post["id"]) > last_post_id:
                collected[str(post["id"])] = post
        if min(page_ids) <= last_post_id:
            print(f"Reached saved cursor {last_post_id} on page {page}.")
            break
        print(f"Page {page}: {len(page_posts)} posts; continuing backwards to {last_post_id}.")
    else:
        raise RuntimeError(
            f"Reached the {max_pages}-page safety limit without finding ID "
            f"{last_post_id}. No cursor was advanced."
        )
    return sorted(collected.values(), key=lambda post: int(post["id"]))


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


# Telegram rejects these files permanently; retrying the same file blocks the queue.
# Network failures, rate limits and Telegram 5xx errors must remain retryable.
PERMANENT_TELEGRAM_MEDIA_ERRORS = (
    "PHOTO_INVALID_DIMENSIONS",
    "PHOTO_INVALID",
    "IMAGE_PROCESS_FAILED",
    "PHOTO_EXT_INVALID",
    "VIDEO_CONTENT_TYPE_INVALID",
    "VIDEO_FILE_INVALID",
    "ANIMATION_INVALID",
    "STICKER_INVALID",
    "FILE_PARTS_INVALID",
    "FILE_TOO_BIG",
    "PHOTO_SAVE_FILE_INVALID",
)


def is_permanent_media_error(error):
    message = str(error).upper()
    return (
        isinstance(error, RuntimeError)
        and "TELEGRAM ERROR 400" in message
        and any(code in message for code in PERMANENT_TELEGRAM_MEDIA_ERRORS)
    )


def _format_name(value):
    """Turn a Rule34 tag/name into a readable name."""
    value = str(value or "").strip().replace("_", " ")
    return " ".join(part[:1].upper() + part[1:] for part in value.split())


def _get_characters(raw_tags):
    """Find every character tag ending in _(valorant), including new agents."""
    characters = []
    seen = set()
    suffix = "_(valorant)"

    for tag in raw_tags:
        lowered = tag.lower()
        if lowered.endswith(suffix):
            name = tag[:-len(suffix)].strip("_ ")
            key = name.casefold()
            if name and key not in seen:
                characters.append(_format_name(name))
                seen.add(key)
        elif lowered in {"hush", "hush_valorant"}:
            # Hush is the newly revealed VALORANT agent; some sources may
            # provide the plain tag before the canonical _(valorant) tag exists.
            if "hush" not in seen:
                characters.append("Hush")
                seen.add("hush")

    return characters

def _hashtag(value):
    """Format a name as a lowercase Telegram hashtag."""
    result = []
    previous_underscore = False
    for char in str(value or "").strip().lower():
        if char.isalnum():
            result.append(char)
            previous_underscore = False
        elif char in " _-":
            if result and not previous_underscore:
                result.append("_")
                previous_underscore = True
    return "".join(result).strip("_")


def build_caption(post):
    """Caption format: hashtags first, then the channel name on its own line."""
    raw_tags = str(post.get("tags", "")).split()
    normalized_tags = {tag.lower() for tag in raw_tags}
    characters = _get_characters(raw_tags)
    hashtags = []
    seen = set()

    # Include every recognized Valorant character, in lowercase hashtag form.
    for character in characters:
        tag = _hashtag(character)
        if tag and tag not in seen:
            hashtags.append("#" + tag)
            seen.add(tag)

    ai_tags = {
        "ai_generated", "ai-generated", "ai_art", "ai_artwork",
        "generated_by_ai", "made_with_ai",
    }
    if normalized_tags.intersection(ai_tags):
        if "ai_generated" not in seen:
            hashtags.append("#ai_generated")
            seen.add("ai_generated")
    else:
        # Any artwork without AI-generation tags is marked as original.
        # Never include the artist's username in the caption.
        if "original" not in seen:
            hashtags.append("#original")
            seen.add("original")

    # Add a media hashtag only for animated formats; ordinary photos get none.
    file_url = str(post.get("file_url", "") or "")
    extension = Path(urlparse(file_url).path).suffix.lower()
    media_tag = "gif" if extension == ".gif" else (
        "video" if extension == ".mp4" else None
    )
    if media_tag and media_tag not in seen:
        hashtags.append("#" + media_tag)
        seen.add(media_tag)

    # Mark cosplay only when the source tags explicitly indicate cosplay.
    is_cosplay = any(
        tag == "cosplay"
        or tag.startswith("cosplay_")
        or tag.endswith("_cosplay")
        or tag == "cosplayer"
        for tag in normalized_tags
    )
    if is_cosplay and "cosplay" not in seen:
        hashtags.append("#cosplay")
        seen.add("cosplay")

    # The channel label is always the final line; no descriptive labels are used.
    channel_line = '<a href="https://t.me/valorantnsfv">R34 Valorant</a>'
    caption = (" ".join(hashtags) + "\n" if hashtags else "") + channel_line
    # Telegram media captions have a 1024-character limit.
    return caption[:1024]


def record_publication(state, post, method, source):
    """Persist successful publication metadata without changing sent_ids."""
    now = datetime.now(timezone.utc).isoformat()
    tags = set(str(post.get("tags", "")).lower().split())
    ai_tags = {"ai_generated", "ai-generated", "ai_art", "ai_artwork", "generated_by_ai", "made_with_ai"}
    media_type = "gif" if method == "sendAnimation" else ("video" if method == "sendVideo" else "image")
    record = {
        "id": str(post["id"]), "at": now, "source": source,
        "type": media_type, "art": "ai_generated" if tags & ai_tags else "original",
        "agents": sorted({_hashtag(name) for name in _get_characters(str(post.get("tags", "")).split())}),
    }
    records = state.setdefault("publication_stats", [])
    if not any(str(item.get("id")) == record["id"] for item in records):
        records.append(record)
    state["last_publication_at"] = now


def record_publication_error(state, context):
    """Count failed publishing attempts, including repeat attempts."""
    if state.get("_do_not_save_state"):
        return
    if not any(word in context.lower() for word in ("публикац", "поста id", "тестовая публикация")):
        return
    state.setdefault("publication_errors", []).append(datetime.now(timezone.utc).isoformat())
    save_state(state)


def build_stats(state):
    now = datetime.now(timezone.utc)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    records = state.get("publication_stats", [])
    errors = state.get("publication_errors", [])

    def date_of(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except (ValueError, TypeError, AttributeError):
            return None

    dates = [date_of(r.get("at")) for r in records]
    def within_24_hours(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            return now - timedelta(hours=24) <= dt <= now
        except (ValueError, TypeError, AttributeError):
            return False
    count_24h = sum(within_24_hours(r.get("at")) for r in records)
    error_dates = [date_of(e) for e in errors]
    agents = Counter(agent for r in records for agent in set(r.get("agents", [])))
    types = Counter(r.get("type") for r in records)
    arts = Counter(r.get("art") for r in records)
    sources = Counter(r.get("source") for r in records)
    enabled = state.get("bot_enabled", True)
    paused = state.get("auto_paused", False)
    status = "выключен" if not enabled else ("пауза" if paused else "работает")
    last_at = state.get("last_publication_at")
    try:
        last_text = datetime.fromisoformat(last_at).astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if last_at else "нет данных"
    except (ValueError, TypeError):
        last_text = "нет данных"
    top = "\n".join(f"{i}. #{name} — {count}" for i, (name, count) in enumerate(agents.most_common(5), 1)) or "Пока нет данных"
    return (
        "📊 Статистика ValoArtsBot\n"
        "(время и периоды: UTC)\n\n"
        f"🤖 Статус: {status}\n"
        f"✅ Успешных публикаций с обновления: {len(records)}\n"
        f"🕐 За 24 часа: {count_24h}\n"
        f"📅 Сегодня: {sum(d == today for d in dates)}\n"
        f"📆 Эта неделя: {sum(d is not None and d >= week_start for d in dates)}\n"
        f"🗓 Этот месяц: {sum(d is not None and d >= month_start for d in dates)}\n\n"
        "📈 Способ публикации\n"
        f"Автоматически: {sources['auto']}\n"
        f"Вручную и тесты: {sources['manual']}\n\n"
        "🎞 Типы медиа\n"
        f"Изображения: {types['image']}\nGIF: {types['gif']}\nВидео: {types['video']}\n\n"
        "🎨 Тип арта\n"
        f"#original: {arts['original']}\n#ai_generated: {arts['ai_generated']}\n\n"
        f"🏆 Топ-5 агентов\n{top}\n\n"
        "⚠️ Ошибки публикации (попытки)\n"
        f"Всего: {len(errors)}\n"
        f"Сегодня: {sum(d == today for d in error_dates)}\n"
        f"Неделя: {sum(d is not None and d >= week_start for d in error_dates)}\n"
        f"Месяц: {sum(d is not None and d >= month_start for d in error_dates)}\n\n"
        "⚙️ Состояние\n"
        f"Интервал: {state.get('post_delay', 5)} сек.\n"
        f"Последний обработанный ID: {state.get('last_post_id', 'нет данных')}\n"
        f"Последняя публикация: {last_text}\n\n"
        "Детальная статистика собирается только с обновления.\n"
        "Периоды считаются по UTC."
    )


def repair_photo_for_telegram(file_data):
    """Normalize an invalid still image into a Telegram-compatible JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    with Image.open(io.BytesIO(file_data)) as image:
        # Apply EXIF rotation before measuring dimensions.
        image = ImageOps.exif_transpose(image)
        image.load()
        if image.width <= 0 or image.height <= 0:
            raise ValueError("Invalid image dimensions")
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        # Telegram photos must not have an extreme width/height ratio.
        width, height = image.size
        if max(width, height) / min(width, height) > 18:
            if width > height:
                canvas_size = (width, max(height, (width + 17) // 18))
            else:
                canvas_size = (max(width, (height + 17) // 18), height)
            canvas = Image.new("RGB", canvas_size, "white")
            converted = image.convert("RGBA")
            x = (canvas_size[0] - width) // 2
            y = (canvas_size[1] - height) // 2
            canvas.paste(converted, (x, y), converted)
            image = canvas
        else:
            if image.mode == "RGBA" or "transparency" in image.info:
                canvas = Image.new("RGB", image.size, "white")
                converted = image.convert("RGBA")
                canvas.paste(converted, (0, 0), converted)
                image = canvas
            else:
                image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()


def publish(post, state=None, source="auto", allow_duplicate=False):
    post_id = str(post["id"])

    # Normal publishing avoids duplicates; /test commands may repost the newest media.
    if state is not None and not allow_duplicate:
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

    caption = build_caption(post)

    try:
        send_media(
            method,
            field_name,
            file_data,
            f"valorant_{post_id}{extension}",
            caption,
        )
    except RuntimeError as error:
        # Retry only a rejected still photo, not network/rate-limit errors.
        if method != "sendPhoto" or not (
            "TELEGRAM ERROR 400" in str(error).upper()
            and any(code in str(error).upper() for code in (
                "PHOTO_INVALID_DIMENSIONS", "PHOTO_INVALID", "IMAGE_PROCESS_FAILED",
                "PHOTO_SAVE_FILE_INVALID",
            ))
        ):
            raise
        print(f"Repairing rejected photo {post_id} and retrying as JPEG")
        try:
            repaired = repair_photo_for_telegram(file_data)
        except Exception as repair_error:
            # Preserve original permanent Telegram error so auto-publisher
            # skips this post instead of repeatedly getting stuck on it.
            print(f"Could not repair photo {post_id}: {repair_error}")
            raise error from repair_error
        send_media(
            "sendPhoto", "photo", repaired,
            f"valorant_{post_id}_repaired.jpg", caption,
        )
        method = "sendPhoto"

    print(f"PUBLISHED: {post_id} ({method})")

    # Record successful sends; tests may resend already recorded IDs.
    # This minimizes the chance of reposting if the process stops mid-batch.
    if state is not None:
        sent_ids = {str(item) for item in state.get("sent_ids", [])}
        sent_ids.add(post_id)
        state["sent_ids"] = sorted(sent_ids, key=lambda value: int(value))
        record_publication(state, post, method, source)
        # Track the actual last successfully sent art, not the auto watermark.
        state["last_published_id"] = post_id
        save_state(state)

    return True


def format_notification(message_text):
    """One consistent style for every administrator notification."""
    return "🤖 ValoArtsBot\n━━━━━━━━━━━━\n" + str(message_text).strip()


def notify_start(chat_id, action, requested):
    send_message(chat_id, f"⏳ Начинаю {action}\n📦 Запрошено: {requested}")


def notify_finish(chat_id, action, requested, successes, failures=0, details=""):
    message = (
        f"✅ Закончил {action}\n"
        f"📦 Запрошено: {requested}\n"
        f"📤 Опубликовано: {successes}\n"
        f"⚠️ Ошибок/пропусков: {failures}"
    )
    if details:
        message += "\n" + details
    send_message(chat_id, message)


def send_message(chat_id, message_text):
    message_text = format_notification(message_text)
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
    try:
        record_publication_error(state, context)
    except Exception as stats_error:
        print(f"Could not save publication error statistics: {stats_error}")

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
                    f"⏱ Интервал между публикациями: {current_delay} сек."
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
            "/stats",
            "/statistics",
            "/clearstats",
            "/clearstate",
            "/currentpost",
        )
        # Built-in commands take priority over the dynamic /add<agent><count>
        # and /add<post_id> syntax. In particular, /addv1 is NOT agent "v".
        is_builtin = command_text in supported_commands
        custom_add = None if is_builtin else re.fullmatch(
            r"/add([a-z][a-z_]*?)([1-9][0-9]*)", command_text, re.I
        )
        custom_id = None if is_builtin else re.fullmatch(
            r"/add([0-9]+)", command_text, re.I
        )
        if command_text not in supported_commands and not custom_add and not custom_id:
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
            # One-shot shutdown: stop this workflow run only. The next scheduled
            # workflow run clears this flag and resumes normal publishing.
            state["shutdown_once"] = True
            state["bot_enabled"] = False
            state["auto_paused"] = True
            state["restart_requested"] = False
            save_state(state)
            send_message(
                chat_id,
                "🛑 Текущий запуск ValoArtsBot остановлен.\n"
                "При следующем запуске GitHub Actions по расписанию бот автоматически продолжит работу.\n"
                "/start не включает бота и не выполняет действий."
            )
            stop_auto_publish = True
            continue

        if command_text == "/start":
            # Intentionally no start/resume behavior for this command.
            send_message(
                chat_id,
                "ℹ️ Команда /start не имеет функции управления ботом.\n"
                "Используй /restart для перезапуска текущего рабочего цикла."
            )
            continue

        if command_text == "/restart":
            state["bot_enabled"] = True
            state["auto_paused"] = False
            state["shutdown_once"] = False
            state["restart_requested"] = True
            save_state(state)
            send_message(
                chat_id,
                "🔄 Перезапуск принят. Рабочий цикл начнётся заново; история опубликованных постов сохранена."
            )
            stop_auto_publish = True
            continue

        if command_text in ("/pause", "/resume"):
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
            save_state(state)
            continue

        if command_text == "/clearstats":
            # Clear only accumulated analytics, never duplicate protection,
            # the automatic watermark, or bot control settings.
            state["publication_stats"] = []
            state["publication_errors"] = []
            state["last_publication_at"] = None
            state["stats_reset_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            send_message(
                chat_id,
                "🧹 Статистика публикаций и ошибок очищена.\n"
                "История ID (sent_ids) и точка автопубликации сохранены."
            )
            continue

        if command_text == "/clearstate":
            # Explicitly rewind the publishing history to its starting point.
            # Keep Telegram update offset so the reset command is not replayed.
            state["initialized"] = True
            state["sent_ids"] = ["16384015"]
            state["last_post_id"] = 16384015
            state["last_published_id"] = "16384015"
            state["last_publication_at"] = None
            state["last_notified_error"] = None
            state["publication_stats"] = []
            state["publication_errors"] = []
            state["stats_reset_at"] = datetime.now(timezone.utc).isoformat()
            # Epoch marker makes GitHub's state merge respect this deliberate reset.
            state["history_reset_epoch"] = time.time_ns()
            save_state(state)
            send_message(
                chat_id,
                "🤖 ValoArtsBot\n━━━━━━━━━━━━\n"
                "🧹 История публикаций сброшена.\n"
                "📌 Последний обработанный арт: 16384015\n"
                "📚 В истории остался только этот ID.\n"
                "▶️ Следующий запуск продолжит автопубликацию после него."
            )
            # Stop this run: it may already have fetched posts using the old cursor.
            stop_auto_publish = True
            continue

        if command_text == "/currentpost":
            last_id = state.get("last_published_id")
            if not last_id:
                # For existing states, use the latest successful record if available.
                records = state.get("publication_stats", [])
                if records:
                    last_id = records[-1].get("id")
            if last_id:
                send_message(chat_id, f"🖼 ID последнего опубликованного арта: {last_id}")
            else:
                send_message(chat_id, "ℹ️ Пока нет данных о последнем опубликованном арте.")
            continue

        if command_text in ("/stats", "/statistics"):
            send_message(chat_id, build_stats(state))
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
                "/shutdown — Остановить текущий запуск; следующий запуск по расписанию продолжит работу\n"
                "/start — Не имеет функции управления ботом\n"
                "/restart — Начать рабочий цикл заново без сброса истории\n"
                "/stats — Подробная статистика публикаций\n"
                "/currentpost — ID последнего опубликованного арта\n"
                "/clearstats — Очистить накопленную статистику (без сброса истории ID)\n"
                "/clearstate — Сбросить историю публикаций до ID 16384015\n"
                "/add<ИмяАгента><КоличествоПостов> — Опубликовать арты выбранного агента (макс. 50)\n"
                "Пример: /addjett10 — 10 артов с Jett\n"
                "/add12345678 — Опубликовать пост по ID, только если это VALORANT\n\n"
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
                f"⏱ Интервал между публикациями: {current_delay} сек."
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

        if custom_add or custom_id:
            # Specific requests are fetched separately, not from the latest
            # 1000 generic VALORANT results. Never modify the auto watermark.
            try:
                if custom_id:
                    requested_id = custom_id.group(1)
                    candidates = get_posts(post_id=requested_id)
                    candidates = [p for p in candidates if str(p["id"]) == requested_id]
                    if not candidates:
                        send_message(chat_id, "❌ Пост не найден или он не относится к VALORANT (rating:explicit).")
                        continue
                else:
                    agent = custom_add.group(1).lower()
                    count = int(custom_add.group(2))
                    if count > 50:
                        send_message(chat_id, "❌ За одну команду можно запросить максимум 50 постов.")
                        continue
                    # Query by canonical Rule34 agent tag; Hush may also use
                    # a plain tag while the canonical tag is being adopted.
                    candidates = get_posts(search_tags=f"valorant rating:explicit {agent}_(valorant)")
                    if agent == "hush" and len(candidates) < count:
                        extra = get_posts(search_tags="valorant rating:explicit hush")
                        existing = {str(p["id"]) for p in candidates}
                        candidates.extend(p for p in extra if str(p["id"]) not in existing)
                    candidates = [
                        p for p in candidates
                        if agent in {_hashtag(name) for name in _get_characters(str(p.get("tags", "")).split())}
                    ]
                    candidates.sort(key=lambda p: int(p["id"]), reverse=True)

                sent_ids = {str(i) for i in state.get("sent_ids", [])}
                candidates = [p for p in candidates if str(p["id"]) not in sent_ids]
                candidates = [p for p in candidates if Path(urlparse(p["file_url"]).path).suffix.lower()
                              in {".jpg", ".jpeg", ".png", ".gif", ".mp4"}]
                if not candidates:
                    send_message(chat_id, "⚠️ Добавление не начато: подходящих неопубликованных постов не найдено.")
                    continue
                limit = 1 if custom_id else count
                successes = 0
                failures = 0
                notify_start(chat_id, "добавление артов", limit)
                for item in candidates:
                    if successes >= limit:
                        break
                    try:
                        outcome = publish(item, state, source="manual")
                        if outcome is True:
                            successes += 1
                        elif outcome is False:
                            failures += 1
                    except Exception as error:
                        failures += 1
                        notify_admin_error(state, f"Ручная публикация поста ID {item['id']}", error)
                    if successes < limit:
                        should_stop = sleep_with_command_polling(state, max(1, int(state.get("post_delay", 5))))
                        if should_stop and (state.get("shutdown_once") or state.get("restart_requested") or state.get("auto_paused")):
                            break
                notify_finish(chat_id, "добавление артов", limit, successes, failures)
            except Exception as error:
                notify_admin_error(state, f"Команда {command_text}", error)
                send_message(chat_id, f"❌ Не удалось выполнить команду: {str(error)[:250]}")
            finally:
                save_state(state)
            continue

        try:
            posts = get_posts()
            sent_ids = set(str(item) for item in state.get("sent_ids", []))

            # Ручные /add-команды не зависят от last_post_id:
            # выбирают старые посты из доступной выборки Rule34, которых
            # ещё нет в sent_ids. Автоматическая публикация использует watermark
            # отдельно, ниже в main().
            if command_text in ("/add1", "/add10", "/add50", "/addv1", "/addv10"):
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
                        "⚠️ Добавление не начато: подходящих неопубликованных постов не найдено в выборке Rule34."
                    )
                    continue

                label = "видео" if videos_only else "медиа"
                notify_start(chat_id, "добавление артов" if not videos_only else "добавление видео", len(candidates))
                successes = []
                failures = []

                for add_post in candidates:
                    sent_ids = set(str(item) for item in state.get("sent_ids", []))
                    add_id = str(add_post["id"])
                    if add_id in sent_ids:
                        continue
                    try:
                        published = publish(add_post, state, source="manual")
                        if published is True:
                            successes.append(add_id)
                        elif published is None:
                            print(f"Add post {add_id} skipped: already published")
                        else:
                            failures.append(f"{add_id} (формат пропущен)")
                    except Exception as error:
                        notify_admin_error(state, f"Ручная публикация поста ID {add_id}", error)
                        failures.append(f"{add_id} (ошибка отправки)")
                    should_stop = sleep_with_command_polling(
                        state, max(1, int(state.get("post_delay", 5)))
                    )
                    if should_stop and (
                        state.get("shutdown_once", False)
                        or state.get("restart_requested", False)
                        or state.get("auto_paused", False)
                    ):
                        break

                summary = (
                    f"Готово. Опубликовано: {len(successes)} из {len(candidates)}; "
                    f"ошибок/пропусков: {len(failures)}."
                )
                if successes:
                    summary += "\nID публикаций: " + ", ".join(successes)
                if failures:
                    summary += "\nНе удалось: " + ", ".join(failures)
                notify_finish(chat_id, "добавление артов" if not videos_only else "добавление видео", len(candidates), len(successes), len(failures),
                              ("ID публикаций: " + ", ".join(successes)) if successes else "")
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
                        "⚠️ Тест не начат: подходящих изображений, GIF или видео не найдено."
                    )
                    continue

                successes = []
                failures = []

                notify_start(chat_id, "тестовую публикацию", len(candidates))

                for test_post in candidates:
                    test_id = str(test_post["id"])
                    try:
                        published = publish(test_post, state, source="manual", allow_duplicate=True)
                        if published is True:
                            successes.append(test_id)
                        elif published is None:
                            print(f"Test post {test_id} skipped: already published")
                            failures.append(f"{test_id} (уже опубликован)")
                        else:
                            failures.append(f"{test_id} (формат пропущен)")
                    except Exception as error:
                        notify_admin_error(state, f"Тестовая публикация поста ID {test_id}", error)
                        failures.append(f"{test_id} (ошибка отправки)")
                    should_stop = sleep_with_command_polling(
                        state, max(1, int(state.get("post_delay", 5)))
                    )
                    if should_stop and (
                        state.get("shutdown_once", False)
                        or state.get("restart_requested", False)
                        or state.get("auto_paused", False)
                    ):
                        break

                summary = (
                    f"Тест 10 постов завершён. Успешно: {len(successes)}, "
                    f"ошибок/пропусков: {len(failures)}."
                )
                if successes:
                    summary += "\nОпубликованные ID: " + ", ".join(successes)
                if failures:
                    summary += "\nНе удалось: " + ", ".join(failures)
                notify_finish(chat_id, "тестовую публикацию", len(candidates), len(successes), len(failures),
                              ("ID публикаций: " + ", ".join(successes)) if successes else "")
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
                    f"⚠️ Тест не начат: файл типа «{label}» не найден."
                )
                continue

            test_post = candidates[0]
            test_id = str(test_post["id"])
            notify_start(chat_id, "тестовую публикацию", 1)
            published = publish(test_post, state, source="manual", allow_duplicate=True)

            if published is True:
                send_message(
                    chat_id,
                    f"✅ Закончил тестовую публикацию\n📦 Запрошено: 1\n📤 Опубликовано: 1\n⚠️ Ошибок/пропусков: 0\nID: {test_id}"
                )
            elif published is None:
                send_message(
                    chat_id,
                    f"⚠️ Закончил тестовую публикацию\n📦 Запрошено: 1\n📤 Опубликовано: 0\n⚠️ Пропущено: 1\nID {test_id} уже опубликован."
                )
            else:
                send_message(
                    chat_id,
                    f"❌ Закончил тестовую публикацию\n📦 Запрошено: 1\n📤 Опубликовано: 0\n⚠️ Ошибок/пропусков: 1\nТип: {label}"
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

    # A /shutdown only stops one workflow run. On the next scheduled run,
    # clear the one-shot flag and resume automatic publishing automatically.
    if state.get("shutdown_once", False):
        state["shutdown_once"] = False
        state["bot_enabled"] = True
        state["auto_paused"] = False
        save_state(state)
        print("One-shot shutdown cleared; resuming on this scheduled run.")

    # Commands are handled before any scheduled publishing.
    stop_requested = process_test_commands(state)
    if state.get("shutdown_once", False):
        print("/shutdown requested; ending this workflow run.")
        return
    if state.get("restart_requested", False):
        state["restart_requested"] = False
        save_state(state)
        stop_requested = False
        print("Restart requested; starting a fresh work cycle.")
    if not state.get("bot_enabled", True):
        state["auto_paused"] = True
        save_state(state)
    if state.get("auto_paused", False):
        wait_while_paused(state, idle_timeout=60)
        if state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return
    elif stop_requested:
        print("A control command requested stopping scheduled publishing this run.")
        return

    posts = (get_posts_after(int(state["last_post_id"]))
             if state.get("last_post_id") is not None else get_posts())
    # The API request can take time; handle commands that arrived while it ran.
    stop_requested = process_test_commands(state)
    if state.get("shutdown_once", False):
        print("/shutdown requested during API lookup; ending this workflow run.")
        return
    if state.get("restart_requested", False):
        state["restart_requested"] = False
        save_state(state)
        print("Restart requested during API lookup; fetching fresh data.")
        posts = (get_posts_after(int(state["last_post_id"]))
                 if state.get("last_post_id") is not None else get_posts())
        stop_requested = False
    if not state.get("bot_enabled", True):
        state["auto_paused"] = True
        save_state(state)
    if state.get("auto_paused", False):
        wait_while_paused(state, idle_timeout=60)
        if state.get("auto_paused", False) or not state.get("bot_enabled", True):
            return
    elif stop_requested:
        print("A control command requested stopping publishing during API request.")
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
        if run_deadline_reached():
            print("Reached safe run deadline; stopping before the next post to save state.json.")
            save_state(state)
            break
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
            if is_permanent_media_error(error):
                # Permanent bad media: do not block the entire publication queue.
                # Save a separate skip record, not a fake successful publication.
                skipped = state.setdefault("skipped_invalid_ids", [])
                if post_id not in skipped:
                    skipped.append(post_id)
                state["last_post_id"] = int(post_id)
                state["initialized"] = True
                save_state(state)
                print(f"SKIP permanently invalid Telegram media: {post_id}: {error}")
                notify_admin_error(state, f"Автопубликация поста ID {post_id}", error)
                # Move directly to the next art. No sleep on invalid media.
                continue
            # Transient failures (timeouts, rate limits, 5xx) are retried next run.
            notify_admin_error(state, f"Автопубликация поста ID {post_id}", error)
            break

        # Обновляем watermark и для опубликованного поста, и для пропущенного
        # неподдерживаемого/слишком большого файла, чтобы не зациклиться на нём.
        state["last_post_id"] = post_id
        # Only successful sends belong in sent_ids; skipped media must not
        # masquerade as successfully published artwork.
        if published is True:
            sent_ids.add(post_id)
        state["sent_ids"] = sorted(sent_ids, key=lambda value: int(value))
        state["initialized"] = True
        save_state(state)

        should_stop = sleep_with_command_polling(
            state, max(1, int(state.get("post_delay", 5)))
        )
        if should_stop or state.get("auto_paused", False) or not state.get("bot_enabled", True):
            print("Stopping automatic publishing because of an administrator command.")
            break

    if state.get("shutdown_once", False):
        print("/shutdown requested during publishing; ending this workflow run.")
        return

    if state.get("restart_requested", False):
        state["restart_requested"] = False
        save_state(state)
        if run_deadline_reached():
            print("Restart deferred to the next scheduled workflow run.")
            return
        print("Restart requested during publishing; starting a fresh work cycle.")
        return main()

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
