import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API_URL = "https://api.rule34.xxx/index.php"
POST_URL = "https://rule34.xxx/index.php?page=post&s=view&id={}"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
RULE34_API_KEY = os.environ["RULE34_API_KEY"]
RULE34_USER_ID = os.environ["RULE34_USER_ID"]

STATE_FILE = Path("state.json")
MAX_FILE_BYTES = 50 * 1024 * 1024

TAGS = (
    "valorant rating:explicit "
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ValoArtsBot/1.0"
})


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

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "Rule34 API вернул не JSON. "
            f"Content-Type: {response.headers.get('Content-Type')}; "
            f"ответ начинается с: {response.text[:200]!r}"
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
                "Неизвестная структура JSON от Rule34. "
                f"Поля ответа: {list(data.keys())}"
            )

    if data is None:
        data = []

    if not isinstance(data, list):
        raise RuntimeError(
            "Неожиданный тип ответа Rule34: "
            f"{type(data).__name__}; значение: {str(data)[:200]}"
        )

    results = []

    blocked_tags = {
    }

    for post in data:
        if not isinstance(post, dict):
            continue

        rating = str(post.get("rating", "")).lower()
        tags = set(str(post.get("tags", "")).lower().split())

        if rating not in ("e", "explicit"):
            continue

        if "valorant" not in tags:
            continue

        if tags.intersection(blocked_tags):
            continue

        if not post.get("id") or not post.get("file_url"):
            continue

        results.append(post)

    return sorted(results, key=lambda p: int(p["id"]))


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

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )


def publish(post):
    post_id = str(post["id"])
    media_url = post["file_url"]

    extension = Path(urlparse(media_url).path).suffix.lower()

    response = SESSION.get(media_url, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    file_data = response.content

    if len(file_data) > MAX_FILE_BYTES:
        print(f"SKIP oversized file: {post_id}")
        return False

    # Определяем тип файла по расширению и Content-Type.
    if extension in (".jpg", ".jpeg", ".png"):
        if not content_type.startswith("image/"):
            print(f"SKIP invalid image: {post_id}")
            return False

        method = "sendPhoto"
        field_name = "photo"

    elif extension == ".gif" or content_type == "image/gif":
        method = "sendAnimation"
        field_name = "animation"

        if not (
            extension == ".gif"
            or file_data[:6] in (b"GIF87a", b"GIF89a")
        ):
            print(f"SKIP invalid GIF: {post_id}")
            return False

        extension = ".gif"

    elif extension == ".mp4" or content_type == "video/mp4":
        method = "sendVideo"
        field_name = "video"
        extension = ".mp4"

        if not (
            extension == ".mp4"
            or file_data[4:8] == b"ftyp"
        ):
            print(f"SKIP invalid MP4: {post_id}")
            return False

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


def main():
    state = load_state()
    sent_ids = set(
        str(item) for item in state.get("sent_ids", [])
    )

    posts = get_posts()

    if not state.get("initialized", False):
        # Первый запуск: последние 100 результатов
        # из доступной выборки API.
        posts = posts[-100:]
        print(f"Initial batch: {len(posts)} posts")
    else:
        posts = [
            post for post in posts
            if str(post["id"]) not in sent_ids
        ]
        print(f"New posts: {len(posts)}")

    for post in posts:
        post_id = str(post["id"])

        if post_id in sent_ids:
            continue

        published = publish(post)

        # Сохраняем ID, чтобы не обрабатывать
        # публикацию повторно при следующем запуске.
        sent_ids.add(post_id)
        state["sent_ids"] = sorted(
            sent_ids, key=lambda value: int(value)
        )
        state["initialized"] = True
        save_state(state)

        time.sleep(1)

    state["initialized"] = True
    state["sent_ids"] = sorted(
        sent_ids, key=lambda value: int(value)
    )
    save_state(state)

    print("Run completed successfully.")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
