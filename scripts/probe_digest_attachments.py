"""Проба: сколько вчерашних сборников реально ушли с фото (read-only).

Читает посты из журнала публикаций через VK API и печатает вложения. Ничего не
пишет и не постит: только чтение.

Запуск на боксе:

    sudo -n bash -c 'set -a; . /etc/setka/setka.env; set +a; cd /home/valstan/SETKA \\
        && PYTHONPATH=/home/valstan/SETKA ./venv/bin/python /tmp/probe_digest_attachments.py'
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import text

from database.connection import AsyncSessionLocal


async def _digest_posts(since: str, until: str, limit: int) -> List[Dict[str, Any]]:
    sql = text(
        """
        SELECT DISTINCT ON (region_code) region_code, published_url, published_at
        FROM published_posts
        WHERE published_at >= :since AND published_at < :until
          AND kind = 'digest' AND published_url IS NOT NULL
        ORDER BY region_code, published_at
        LIMIT :limit
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                sql,
                {
                    "since": datetime.fromisoformat(since),
                    "until": datetime.fromisoformat(until),
                    "limit": limit,
                },
            )
        ).all()
    return [{"region": r[0], "url": r[1], "at": str(r[2])} for r in rows]


async def main() -> None:
    import vk_api

    from config.runtime import get_photo_upload_token_names
    from modules.vk_token_router import TokenPolicy

    since = "2026-11-09 18:00"
    until = "2026-11-09 19:30"
    limit = 60

    posts = await _digest_posts(since, until, limit)
    print(f"постов сборников в окне: {len(posts)}")

    names = get_photo_upload_token_names()
    token = None
    async with AsyncSessionLocal() as session:
        policy = TokenPolicy(session)
        for name in names:
            candidate = await policy.token_for_account(name)
            if candidate is not None and getattr(candidate, "token", None):
                token = candidate.token
                print(f"читаем токеном аккаунта: {name}")
                break
    if not token:
        print("NO_TOKEN")
        return

    api = vk_api.VkApi(token=token).get_api()
    with_photo = 0
    for row in posts:
        pid = row["url"].rsplit("wall", 1)[-1]
        try:
            items = api.wall.getById(posts=pid, fields="attachments")
        except Exception as e:  # noqa: BLE001 — читаем, что дают
            print(f"{row['region']:<18} {pid:<22} ERROR {str(e)[:90]}")
            continue
        items = items if isinstance(items, list) else (items or {}).get("items", [])
        if not items:
            print(f"{row['region']:<18} {pid:<22} НЕТ ПОСТА")
            continue
        atts = items[0].get("attachments") or []
        photos = [
            {
                "id": (a.get("photo") or {}).get("id"),
                "owner_id": (a.get("photo") or {}).get("owner_id"),
                "album_id": (a.get("photo") or {}).get("album_id"),
            }
            for a in atts
            if a.get("type") == "photo"
        ]
        if photos:
            with_photo += 1
        dumped = json.dumps(photos, ensure_ascii=False)
        print(f"{row['region']:<18} {pid:<22} вложений={len(atts)} фото={dumped}")

    print(f"=== ИТОГ: постов с фото {with_photo} из {len(posts)} ===")


if __name__ == "__main__":
    asyncio.run(main())
