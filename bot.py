
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
MAX_IMAGE_BYTES = 10 * 1024 * 1024

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
        # Некоторые ответы могут содержать один объект публикации.
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



def send_photo(image_data, filename, caption):
    response = SESSION.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data={
            "chat_id": CHANNEL_ID,
            "caption": caption,
        },
        files={
            "photo": (filename, image_data),
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
    image_url = post["file_url"]

    extension = Path(urlparse(image_url).path).suffix.lower()

    # Публикуем только обычные изображения.
    if extension not in (".jpg", ".jpeg", ".png"):
        print(f"SKIP unsupported format: {post_id}")
        return False

    response = SESSION.get(image_url, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    if not content_type.startswith("image/"):
        print(f"SKIP non-image file: {post_id}")
        return False

    image_data = response.content

    if len(image_data) > MAX_IMAGE_BYTES:
        print(f"SKIP oversized file: {post_id}")
        return False

    caption = (
        caption = '<a href="https://t.me/valorantnsfv">R34 Valorant</a>'
    )

    send_photo(
        image_data,
        f"valorant_{post_id}{extension}",
        caption,
    )

    print(f"PUBLISHED: {post_id}")
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

        # При ошибке публикации исключение завершит запуск.
        # Успешно отправленные посты сохраняются сразу.
        published = publish(post)

        # Неподдерживаемые форматы тоже помечаем обработанными,
        # чтобы не проверять один и тот же файл бесконечно.
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
