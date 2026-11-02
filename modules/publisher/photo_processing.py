"""Подготовка фото к вложению в вечернюю сводку «Короткой строкой» (P214).

Спецификация — решения владельца от 18.10 ([P203](../../docs/pending/
P203-pekarnya-pechyonye-posty-s-obrabotkoy-foto.md)), перенесённые из отменённой
«Пекарни» в медиа вечерней сводки:

* **Водяной знак** названия сообщества-источника: верхний вариант, полупрозрачный
  текст с тенью, «еле видно, но видно» (владелец просил прозрачность даже сильнее,
  чем в демо). На чужих фото ставим — прямое решение владельца.
* **Цифровой шум** — незаметный для глаза шум. Его задача не «улучшить картинку»,
  а сделать её **не копией**: та же фотография, перезалитая другими байтами, —
  это и есть цифровой шум. Поэтому он случайный по построению: детерминированный
  шум превращался бы в обычный фильтр, а не в метку.
* **Ресайз** в нативный для телефона размер, **без обрезки** (владелец прямо
  запретил кроп: «риск отрезать полголовы»).
* EXIF вычищается, ориентация из EXIF применяется (`exif_transpose`).

Всё это меняет вид чужой фотографии, поэтому параметры зафиксированы
константами и не «подбираются на глаз»: любая правка здесь — отдельное
решение владельца, а не настройка под вкус.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

#: Потолок по стороне: телефон показывает кадр целиком, обрезать нельзя.
MAX_W = 1080
MAX_H = 1350

#: Прозрачность водяного знака. Владелец: «еле видно, но видно» — поэтому
#: заметно слабее обычного «видно».
WATERMARK_ALPHA = 120

#: Амплитуда цифрового шума (σ в единицах уровня яркости 0..255).
NOISE_SIGMA = 1.5

#: Качество JPEG. Ниже 85 видны артефакты на телефоне, выше — трафик без нужды.
JPEG_QUALITY = 88

#: Шрифт водяного знака — тот же, что у фирменной графики (DejaVu, свободная
#: лицензия). Системные шрифты не годятся: на Windows-деве DejaVu нет вовсе,
#: на Linux путь зависит от пакета, а результат обязан быть одинаковым.
_FONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets",
    "fonts",
    "DejaVuSans-Bold.ttf",
)

#: Отступ водяного знака от края, в процентах от ширины.
_WATERMARK_MARGIN_RATIO = 0.03
#: Кегль водяного знака — доля ширины (но не меньше 16 и не больше 72).
_WATERMARK_SIZE_RATIO = 0.045
_WATERMARK_MIN_SIZE = 16
_WATERMARK_MAX_SIZE = 72
#: Сдвиг тени под текстом, в пикселях.
_WATERMARK_SHADOW_OFFSET = 2

_DOWNLOAD_TIMEOUT = 20
_UPLOAD_TIMEOUT = 30


def _load_font(size: int) -> Any:
    """Шрифт водяного знака; при отсутствии файла — встроенный (мельче, но работает)."""
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:  # noqa: BLE001 — файл шрифта может отсутствовать в сборке
        return ImageFont.load_default()


def resize_for_phone(img: Image.Image) -> Image.Image:
    """Уменьшить до телефона, **не обрезая** ни одного пикселя.

    Пропорции сохраняются: ``thumbnail`` только уменьшает. Обрезка запрещена
    решением владельца (P203) — «риск отрезать полголовы».
    """
    img = img.copy()
    img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
    return img


def add_watermark(img: Image.Image, text: str) -> Image.Image:
    """Накрыть водяным знаком с тенью: полупрозрачный текст, «еле видно»."""
    label = (text or "").strip()
    if not label:
        return img

    base = img.convert("RGBA")
    width, height = base.size
    size = int(width * _WATERMARK_SIZE_RATIO)
    size = max(_WATERMARK_MIN_SIZE, min(_WATERMARK_MAX_SIZE, size))
    font = _load_font(size)
    margin = max(6, int(width * _WATERMARK_MARGIN_RATIO))

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Тень — тёмная, чуть смещённая: без неё белый текст на светлом фоне
    # не читается, а слишком тёмная тень перетягивает внимание.
    draw.text(
        (margin + _WATERMARK_SHADOW_OFFSET, margin + _WATERMARK_SHADOW_OFFSET),
        label,
        font=font,
        fill=(0, 0, 0, WATERMARK_ALPHA // 2),
    )
    draw.text((margin, margin), label, font=font, fill=(255, 255, 255, WATERMARK_ALPHA))

    return Image.alpha_composite(base, overlay).convert("RGB")


def add_noise(img: Image.Image) -> Image.Image:
    """Наложить цифровой шум: картинка перестаёт быть копией исходника."""
    if NOISE_SIGMA <= 0:
        return img
    rgb = img.convert("RGB")
    # effect_noise даёт гауссов шум вокруг 128 — то есть «ноль» шума это 128.
    # Из `add` с offset=-128 получаем именно ±σ вокруг нуля, а не сдвиг яркости.
    noise = Image.effect_noise(rgb.size, NOISE_SIGMA).convert("RGB")
    return ImageChops.add(rgb, noise, scale=1.0, offset=-128)


def process_photo(raw: bytes, watermark: str = "") -> bytes:
    """Фото → готовое к вложению JPEG: ресайз, водяной знак, шум, без EXIF.

    Бросает исключение, если это не изображение: вызывающий сам решит, что
    значит «фото не прочиталось» (в сборнике — пропустить фото, а не уронить
    рубрику).
    """
    with Image.open(io.BytesIO(raw)) as opened:
        img = ImageOps.exif_transpose(opened)
        img = img.convert("RGB")
        img = resize_for_phone(img)
        img = add_watermark(img, watermark)
        img = add_noise(img)
        out = io.BytesIO()
        # save без exif= → EXIF не переносится (PIL и так не пишет его сам,
        # но параметр оставлен явно: поведение не должно зависеть от версии).
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()


def download_photo(url: str, *, timeout: int = _DOWNLOAD_TIMEOUT) -> Optional[bytes]:
    """Скачать фото по публичной ссылке ВК. ``None`` — не скачалось."""
    import requests

    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as e:  # noqa: BLE001 — фото не обязано быть
        logger.warning("photo: не скачалось %s: %s", url[:120], e)
        return None


async def upload_wall_photo(
    publisher: Any, group_id: int, image_bytes: bytes, *, filename: str = "digest.jpg"
) -> Optional[str]:
    """Залить фото в группу и вернуть строку вложения ``photo<owner>_<id>``.

    Именно wall-upload: фото, приложенное к посту сообщества, должно принадлежать
    **этому** сообществу. Токен — тот, которым идёт публикация (community-токен
    группы), иначе VK отклонит вложение.

    Вызовы идут через ``publisher._invoke`` — это тот же шов, которым публикуется
    сам пост: rate-limit токена, community-токен вместо publish-токена и единая
    обработка отказа ВК. Свой клиент здесь означал бы второй путь в ВК мимо
    ограничений — ровно то, чего в проекте избегают.

    ``publisher`` — duck-typed: нужен ``_client_for_group(group_id)`` и
    ``_invoke(client, method, params)``.
    """
    import asyncio

    import requests

    gid = abs(int(group_id))
    try:
        client, _is_community = publisher._client_for_group(gid)
        server = await publisher._invoke(
            client, "photos.getWallUploadServer", {"group_id": gid}
        )
        upload_url = (server or {}).get("upload_url")
        if not upload_url:
            logger.warning("photo: upload-сервер не вернул url")
            return None

        def _post() -> dict:
            resp = requests.post(
                upload_url,
                files={"photo": (filename, image_bytes, "image/jpeg")},
                timeout=_UPLOAD_TIMEOUT,
            )
            return resp.json()

        up = await asyncio.to_thread(_post)
        if not (up or {}).get("photo") or not (up or {}).get("hash"):
            logger.warning("photo: upload вернул пусто: %s", str(up)[:150])
            return None

        saved = await publisher._invoke(
            client,
            "photos.saveWallPhoto",
            {
                "photo": up["photo"],
                "server": up["server"],
                "hash": up["hash"],
                "group_id": gid,
            },
        )
        photo = _first_photo_item(saved)
        if photo is None:
            logger.warning("photo: saveWallPhoto вернул пусто")
            return None
        return f"photo{photo['owner_id']}_{photo['id']}"
    except Exception as e:  # noqa: BLE001 — фото не обязано быть
        logger.warning("photo: загрузка не удалась: %s", e)
        return None


def _first_photo_item(saved: Any) -> Optional[dict]:
    """Достать фото из ответа ВК: у ``photos.saveWallPhoto`` это ``items[0]``."""
    if isinstance(saved, list):
        items = saved
    elif isinstance(saved, dict):
        items = saved.get("items") or []
    else:
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") is not None:
            return item
    return None
