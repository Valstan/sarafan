"""Загрузка картинок на стену VK-сообщества (планировщик рекламного кабинета).

Зеркало :mod:`modules.ad_cabinet.vk_photo_upload`, но для **стены** (wall), а не
для ЛС: ``wall.post`` принимает только VK-attachment'ы (``photo<owner>_<id>``),
поэтому офферные/рекламные картинки надо один раз залить через
wall-upload-server и получить attachment-строку.

⚠️ Грузить нужно **user-токеном владельца** целевой группы: VK запрещает
``photos.getWallUploadServer``/``saveWallPhoto`` под community-auth — отдаёт
``[27] method is unavailable with group auth`` (probe 2026-06-19: 16/16 пабликов
сети заливаются user-токеном, 0/16 community-токеном).

И это не «какой угодно админ», а именно **владелец**: фото, залитое
администратором, community-токен в записи **не принимает** — пост уходит без
вложения, и молча (замер 09.11, ``docs/ops/photo-upload-path.md``). Фото
владельца прикрепляется и переходит в собственность группы
(``owner_id = -group_id``), поэтому ``access_key`` в строку вложения **не
добавляем**: с ним фото осталось бы личным и копилось бы в альбоме владельца.

``group_id`` всё равно передаём в ``saveWallPhoto``, а attachment-строку нельзя
переиспользовать на чужой стене (owner-mismatch → VK дропнет вложение): грузим
по каждой целевой группе отдельно. По одной картинке за вызов upload-сервера;
стена VK принимает максимум 10 вложений.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

MAX_WALL_PHOTOS = 10
_UPLOAD_TIMEOUT = 30


def upload_wall_photo(
    api,
    image_bytes: bytes,
    *,
    group_id: int,
    filename: str = "post.jpg",
) -> Optional[str]:
    """Залить одну картинку в wall-контекст сообщества.

    ``group_id`` — **положительный** id группы (VK ждёт positive для
    ``getWallUploadServer`` / ``saveWallPhoto``). Returns ``"photo<owner>_<id>"``
    (с ``_access_key`` если есть) или ``None``.
    """
    gid = abs(int(group_id))
    try:
        server = api.photos.getWallUploadServer(group_id=gid)
        upload_url = server["upload_url"]

        # Сырой multipart POST (vk_api.method() это не умеет). Поле строго "photo".
        up = requests.post(
            upload_url,
            files={"photo": (filename, image_bytes, "image/jpeg")},
            timeout=_UPLOAD_TIMEOUT,
        ).json()
        if not up.get("photo") or up.get("photo") in ("[]", ""):
            logger.warning("wall upload returned empty photo: %s", up)
            return None

        saved = api.photos.saveWallPhoto(
            group_id=gid,
            photo=up["photo"],
            server=up["server"],
            hash=up["hash"],
        )
        if not saved:
            return None
        p = saved[0]
        # Без access_key: фото переходит в собственность группы. С ним фото
        # осталось бы личным (owner_id = пользователь) и копилось бы в альбоме
        # владельца — см. докстринг модуля.
        return f"photo{p['owner_id']}_{p['id']}"
    except Exception as e:
        logger.warning("upload_wall_photo failed: %s", e)
        return None


def upload_wall_images(api, images: List[bytes], *, group_id: int) -> List[str]:
    """Залить до 10 картинок на стену, вернуть список attachment-строк.

    Частичный успех допустим: что залилось, то и вернём (в порядке входа).
    """
    parts: List[str] = []
    for i, blob in enumerate(images[:MAX_WALL_PHOTOS]):
        att = upload_wall_photo(api, blob, group_id=group_id, filename=f"post_{i}.jpg")
        if att:
            parts.append(att)
    return parts
