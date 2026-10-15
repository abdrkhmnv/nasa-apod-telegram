
import os
import json
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from PIL import Image

NASA_API = "https://api.nasa.gov/planetary/apod"
TG_API = "https://api.telegram.org"
STATE_FILE = Path("data/apod_state.json")
MAX_FILE_SIZE = 49_000_000
TIMEOUT = 60


def get_apod():
    today = datetime.now(ZoneInfo("Asia/Tashkent")).date().isoformat()
    response = requests.get(
        NASA_API,
        params={
            "api_key": os.getenv("NASA_API_KEY", "DEMO_KEY"),
            "date": today,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    item = response.json()

    if not item.get("date") or not item.get("url"):
        raise RuntimeError("NASA APOD ma'lumoti topilmadi.")

    return item


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def make_caption(item):
    title = item.get("title", "NASA APOD")
    explanation = item.get("explanation", "")
    text = f"🌌 {title}\n\n{explanation}"
    return text[:1021] + "…" if len(text) > 1024 else text


def telegram(method, *, file_field=None, file_obj=None, **data):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    files = {file_field: file_obj} if file_field and file_obj else None

    response = requests.post(
        f"{TG_API}/bot{token}/{method}",
        data={"chat_id": chat_id, **data},
        files=files,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram xatosi: {result}")

    return result["result"]


def download(url, path):
    with requests.get(url, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        size = 0

        with open(path, "wb") as output:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise RuntimeError(
                        "Fayl Telegram uchun belgilangan hajm limitidan katta."
                    )

                output.write(chunk)

    if Path(path).stat().st_size == 0:
        raise RuntimeError("Yuklangan fayl bo'sh.")


def send_photo(item, state):
    original_url = item.get("hdurl") or item["url"]

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        downloaded = folder / "downloaded_original"
        preview = folder / "preview.jpg"
        original_jpg = folder / "nasa_apod_original.jpg"

        download(original_url, downloaded)

        # Telegram ichida ko'rinadigan rasmni tayyorlash.
        with Image.open(downloaded) as image:
            image.convert("RGB").save(
                preview,
                format="JPEG",
                quality=92,
                optimize=True,
            )

            if image.format == "JPEG":
                original_jpg.write_bytes(downloaded.read_bytes())
            else:
                image.convert("RGB").save(
                    original_jpg,
                    format="JPEG",
                    quality=95,
                    subsampling=0,
                )

        # 1. Rasm + sarlavha + izoh.
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

        # 2. Alohida original JPG fayl.
        if not state.get("document_sent"):
            if original_jpg.stat().st_size > MAX_FILE_SIZE:
                raise RuntimeError("Original JPG Telegram hajm limitidan katta.")

            with original_jpg.open("rb") as document:
                telegram(
                    "sendDocument",
                    file_field="document",
                    file_obj=document,
                    caption="📁 Original JPG — yuqori sifat",
                )

            state["document_sent"] = True
            save_state(state)


def download_video(url, folder):
    output = str(Path(folder) / "apod.%(ext)s")

    subprocess.run(
        [
            "yt-dlp",
            "--no-playlist",
            "--format",
            "best[ext=mp4][filesize<49000000]/"
            "best[ext=mp4][filesize_approx<49000000]/"
            "best[filesize<49000000]",
            "--output", output,
            url,
        ],
        check=True,
        timeout=600,
    )

    candidates = [
        p for p in Path(folder).glob("apod.*")
        if p.is_file() and p.suffix.lower() == ".mp4"
    ]

    if not candidates:
        raise RuntimeError(
            "49 MB dan kichik MP4 topilmadi yoki video manbasi qo'llab-quvvatlanmaydi."
        )

    return candidates[0]


def send_video(item, state):
    if state.get("video_sent"):
        return

    with tempfile.TemporaryDirectory() as folder:
        video_path = download_video(item["url"], folder)

        with video_path.open("rb") as video:
            telegram(
                "sendVideo",
                file_field="video",
                file_obj=video,
                caption=make_caption(item),
                supports_streaming="true",
            )

    state["video_sent"] = True
    save_state(state)


def main():
    item = get_apod()
    apod_date = item["date"]
    state = load_state()

    if state.get("date") != apod_date:
        state = {
            "date": apod_date,
            "photo_sent": False,
            "document_sent": False,
            "video_sent": False,
        }
        save_state(state)

    if state.get("complete"):
        print("Bu APOD allaqachon to'liq yuborilgan.")
        return

    if item.get("media_type") == "image":
        send_photo(item, state)
        if not state.get("photo_sent") or not state.get("document_sent"):
            raise RuntimeError("Rasm yoki original fayl yuborilishi tugamadi.")

    elif item.get("media_type") == "video":
        send_video(item, state)
        if not state.get("video_sent"):
            raise RuntimeError("Video yuborilmadi.")

    else:
        raise RuntimeError("NASA noma'lum media turini qaytardi.")

    state["complete"] = True
    save_state(state)
    print("NASA APOD muvaffaqiyatli yuborildi.")


if __name__ == "__main__":
    main()
