import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, UnidentifiedImageError

NASA_API = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"
TG_API = "https://api.telegram.org"
STATE_FILE = Path("data/apod_state.json")
TIMEOUT = 60
MAX_FILE_SIZE = 49_000_000
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NASA-APOD-Telegram-Bot/1.0"})


def get_apod():
    today = datetime.now(ZoneInfo("Asia/Tashkent")).date().isoformat()
    response = SESSION.get(
        NASA_API,
        params={"api_key": os.getenv("NASA_API_KEY", "DEMO_KEY"), "date": today},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    item = response.json()

    for key in ("date", "title", "explanation", "url", "media_type"):
        if not item.get(key):
            raise RuntimeError(f"NASA APOD javobida {key!r} topilmadi.")

    print(f"APOD date: {item['date']}")
    print(f"APOD title: {item['title']}")
    print(f"APOD media_type: {item['media_type']}")
    print(f"APOD url: {item['url']}")
    print(f"APOD hdurl: {item.get('hdurl', '(yo‘q)')}")
    return item


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"State faylini o‘qib bo‘lmadi: {exc}") from exc


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def make_caption(item):
    text = f"🌌 {item['title']}\n\n{item['explanation']}"
    return text if len(text) <= 1024 else text[:1021].rstrip() + "…"


def telegram(method, *, file_field=None, file_obj=None, **data):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN yoki TELEGRAM_CHAT_ID Secrets’da yo‘q.")

    files = {file_field: file_obj} if file_field and file_obj else None
    response = SESSION.post(
        f"{TG_API}/bot{token}/{method}",
        data={"chat_id": chat_id, **data},
        files=files,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} xatosi: {result}")
    return result["result"]


def download_image(url, destination):
    if not url or not url.startswith(("https://", "http://")):
        raise RuntimeError(f"Noto‘g‘ri rasm URL: {url!r}")

    size = 0
    with SESSION.get(url, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        print(f"Downloaded URL: {response.url}")
        print(f"Content-Type: {response.headers.get('Content-Type', '(unknown)')}")
        with open(destination, "wb") as output:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise RuntimeError("Rasm 49 MB limitidan katta.")
                output.write(chunk)

    if size == 0:
        raise RuntimeError("Yuklangan fayl bo‘sh.")

    try:
        with Image.open(destination) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        sample = Path(destination).read_bytes()[:120]
        raise RuntimeError(
            f"NASA URL’dan haqiqiy rasm kelmadi. Fayl boshi: {sample!r}"
        ) from exc


def send_image(item, state):
    # NASA APOD rasmida hdurl odatda yuqori sifatli nusxa; bo‘lmasa url.
    source_url = item.get("hdurl") or item["url"]

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        downloaded = temp_dir / "downloaded_original"
        preview = temp_dir / "nasa_apod_preview.jpg"
        original_jpg = temp_dir / "nasa_apod_original.jpg"

        download_image(source_url, downloaded)

        with Image.open(downloaded) as image:
            image.load()
            image.convert("RGB").save(preview, format="JPEG", quality=92, optimize=True)
            if image.format == "JPEG":
                original_jpg.write_bytes(downloaded.read_bytes())
            else:
                image.convert("RGB").save(
                    original_jpg,
                    format="JPEG",
                    quality=95,
                    subsampling=0,
                    optimize=True,
                )

        if not state.get("photo_sent"):
            with preview.open("rb") as photo:
                telegram(
                    "sendPhoto",
                    file_field="photo",
                    file_obj=photo,
                    caption=make_caption(item),
                )
            state["photo_sent"] = True
            save_state(state)
            print("Telegram: rasm va izoh yuborildi.")

        if not state.get("document_sent"):
            if original_jpg.stat().st_size > MAX_FILE_SIZE:
                raise RuntimeError("JPG Telegram hajm limitidan katta.")
            with original_jpg.open("rb") as document:
                telegram(
                    "sendDocument",
                    file_field="document",
                    file_obj=document,
                    filename="nasa_apod_original.jpg",
                    caption=f"📁 Original JPG — {item['title']}",
                )
            state["document_sent"] = True
            save_state(state)
            print("Telegram: alohida JPG fayli yuborildi.")


def main():
    item = get_apod()
    if item["media_type"] != "image":
        raise RuntimeError(
            f"Bugungi APOD turi {item['media_type']!r}; bu versiya faqat rasmlarni yuboradi."
        )

    apod_date = item["date"]
    state = load_state()
    if state.get("date") != apod_date:
        state = {
            "date": apod_date,
            "photo_sent": False,
            "document_sent": False,
            "complete": False,
        }
        save_state(state)

    if state.get("complete"):
        print("Bu APOD allaqachon to‘liq yuborilgan; takrorlanmaydi.")
        return

    send_image(item, state)
    if not state.get("photo_sent") or not state.get("document_sent"):
        raise RuntimeError("Rasm yoki JPG fayli yuborilishi tugamadi.")

    state["complete"] = True
    save_state(state)
    print("NASA APOD muvaffaqiyatli yuborildi.")


if __name__ == "__main__":
    main()
