
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from PIL import Image

NASA_API = "https://science.nasa.gov/wp-json/wp/v2/apod-basic/"
TG_API = "https://api.telegram.org"
STATE = Path("data/apod_state.json")
MAX_TG_FILE = 49_000_000
TIMEOUT = 60


def get_apod():
    today = datetime.now(ZoneInfo("Asia/Tashkent")).date().isoformat()
    response = requests.get(
        NASA_API,
        params={
            "date": today,
            "api_key": os.getenv("NASA_API_KEY", "DEMO_KEY"),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, list):
        if not data:
            raise RuntimeError("NASA bugungi APOD ma'lumotini bermadi.")
        data = data[0]

    if not isinstance(data, dict) or not data.get("date"):
        raise RuntimeError("NASA javobi kutilgan formatda emas.")

    return data


def read_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raise RuntimeError("State fayli buzilgan. Uni tekshirish kerak.")
    return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(STATE)


def caption(item):
    title = item.get("title") or "NASA APOD"
    explanation = html.unescape(item.get("explanation") or "")
    explanation = re.sub(r"<[^>]+>", "", explanation).strip()
    text = f"🌌 {title}\n\n{explanation}"
    return text[:1021] + "…" if len(text) > 1024 else text


def tg(method, *, file_field=None, file_obj=None, **data):
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
        raise RuntimeError(f"Telegram {method} xatosi: {result}")

    return result["result"]


def download(url, path):
    if not url or not url.startswith(("https://", "http://")):
        raise RuntimeError("Yaroqli media URL topilmadi.")

    with requests.get(url, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        size = 0

        with open(path, "wb") as output:
            for chunk in response.iter_content(1024 * 256):
                if not chunk:
                    continue
                size += len(chunk)

                if size > MAX_TG_FILE:
                    raise RuntimeError(
                        "Fayl Telegram limitidan katta. "
                        "Originalni yuborib bo'lmadi."
                    )

                output.write(chunk)

    if Path(path).stat().st_size == 0:
        raise RuntimeError("Yuklangan fayl bo'sh.")


def send_photo(item, state):
    original_url = item.get("hdurl") or item.get("url")
    if not original_url:
        raise RuntimeError("NASA original rasm havolasini bermadi.")

    with tempfile.TemporaryDirectory() as folder:
        original = Path(folder) / "original_image"
        preview = Path(folder) / "preview.jpg"

        download(original_url, original)

        # Telegram uchun optimallashtirilgan ko'rinadigan rasm.
        with Image.open(original) as img:
            img = img.convert("RGB")
            img.thumbnail((2560, 2560))
            img.save(preview, "JPEG", quality=90, optimize=True)

        # Birinchi bosqich: rasm + sarlavha + izoh.
        if not state.get("photo_sent"):
            with preview.open("rb") as photo:
                tg(
                    "sendPhoto",
                    file_field="photo",
                    file_obj=photo,
                    caption=caption(item),
                )

            state["photo_sent"] = True
            save_state(state)

        # Ikkinchi bosqich: yuqori sifatli original fayl.
        if not state.get("document_sent"):
            with original.open("rb") as document:
                tg(
                    "sendDocument",
                    file_field="document",
                    file_obj=document,
                    caption=f"📁 Original yuqori sifatli rasm\n"
                            f"{item.get('title', 'NASA APOD')}",
                )

            state["document_sent"] = True
            save_state(state)


def video_candidates(item):
    urls = [item.get("url"), item.get("permalink")]

    # Ayrim APOD sahifalarida video iframe ichida joylashadi.
    basic_html = item.get("basic_html") or ""
    for match in re.findall(
        r'<iframe[^>]+src=["\']([^"\']+)',
        basic_html,
        flags=re.I,
    ):
        urls.append(html.unescape(match))

    result = []
    for url in urls:
        if url and url.startswith(("https://", "http://")) and url not in result:
            result.append(url)

    return result


def download_video(item, folder):
    # 49 MB dan kichik, bevosita ko'rish mumkin bo'lgan MP4 izlaymiz.
    for url in video_candidates(item):
        output = str(Path(folder) / "video.%(ext)s")

        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "--format",
                    "best[ext=mp4][filesize<49000000]/"
                    "best[ext=mp4][filesize_approx<49000000]/"
                    "best[filesize<49000000]/best",
                    "--merge-output-format", "mp4",
                    "--output", output,
                    url,
                ],
                check=True,
                timeout=600,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue

        files = [
            p for p in Path(folder).glob("video.*")
            if p.is_file() and p.suffix.lower() in
            {".mp4", ".mkv", ".webm", ".mov"}
        ]

        if not files:
            continue

        path = max(files, key=lambda p: p.stat().st_size)

        if path.stat().st_size > MAX_TG_FILE:
            path.unlink(missing_ok=True)
            continue

        # Telegram video sifatida yuborish uchun MP4 tanlanadi.
        if path.suffix.lower() == ".mp4":
            return path

    raise RuntimeError(
        "Telegram uchun mos, 49 MB dan kichik MP4 topilmadi. "
        "Manba cheklangan yoki video formati mos emas."
    )


def send_video(item, state):
    if state.get("video_sent"):
        return

    with tempfile.TemporaryDirectory() as folder:
        video = download_video(item, folder)

        with video.open("rb") as file:
            tg(
                "sendVideo",
                file_field="video",
                file_obj=file,
                caption=caption(item),
                supports_streaming="true",
            )

    state["video_sent"] = True
    save_state(state)


def main():
    item = get_apod()
    apod_date = item["date"]
    media_type = item.get("media_type")

    state = read_state()

    # Har bir APOD sanasi uchun alohida bosqichlar.
    if state.get("date") != apod_date:
        state = {
            "date": apod_date,
            "photo_sent": False,
            "document_sent": False,
            "video_sent": False,
        }
        save_state(state)

    if media_type == "image":
        send_photo(item, state)

        if not state.get("document_sent"):
            raise RuntimeError("Original rasm hali yuborilmadi.")

    elif media_type == "video":
        send_video(item, state)

        if not state.get("video_sent"):
            raise RuntimeError("Video hali yuborilmadi.")

    else:
        raise RuntimeError(
            f"APOD media turi '{media_type}'. "
            "Bu tur avtomatik media yuborish uchun qo'llab-quvvatlanmaydi."
        )

    state["complete"] = True
    save_state(state)
    print(f"{apod_date}: APOD yuborish yakunlandi.")


if __name__ == "__main__":
    main()
