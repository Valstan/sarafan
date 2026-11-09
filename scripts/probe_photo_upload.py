"""Проба: заливка фото на стену сообщества — почему ВК отдаёт пустой ``photo`` (P214).

Симптом (замер 09.11, вечер, сборники): 75 попыток заливки, 72 отказа. ВК отдаёт
``server`` + ``hash``, но ``photo`` пустой — код это считает отказом и роняет
фото. Плюс HTTP 405 от ``pu.vk.com`` (nginx) после нескольких загрузок подряд.

Проба отвечает на вопросы измерением, а не «на глаз»:

1. **дедуп** — та же картинка второй раз: ВК отдаёт пустой ``photo``? (нет)
2. **темп** — на какой по счёту загрузке ``photo`` пустеет?
3. **картинка** — боевой конвейер (скачать → ресайз → водяной знак → шум) или
   синтетика: влияет ли на пустой ``photo``?
4. **группа** — одна и та же группа или разные (как в бою: сборник каждого
   района — своя группа)?
5. **спасение** — если ``photo`` пустой, принимает ли ВК
   ``photos.saveWallPhoto`` с одними ``server`` + ``hash``?

Проба **не публикует постов**: ``photos.saveWallPhoto`` вызывается только там,
где это явно включено (``--save-mode``). Загрузка без сохранения не оставляет
следа на стене.

Запуск на боксе (токен владельца живёт в БД, токен-разрешение — тем же
путём, что в бою, см. ``modules.vk_token_router.TokenPolicy``):

    sudo -n bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA \\
        && PYTHONPATH=/home/valstan/SETKA ./venv/bin/python scripts/probe_photo_upload.py \\
             --count 12 --source prod --groups journal'
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image


def _make_image(seed: int, size=(900, 1200)) -> bytes:
    """Синтетическая картинка: детерминированная по ``seed``.

    Нужен настоящий JPEG: upload-сервер ВК проверяет содержимое, и пустой
    файл отличается от картинки по поведению сервера.
    """
    img = Image.new("RGB", size)
    px = img.load()
    rnd = seed * 7919
    for y in range(0, size[1], 40):
        for x in range(0, size[0], 40):
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
            px[x, y] = ((rnd >> 16) & 255, (rnd >> 8) & 255, rnd & 255)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


async def _owner_token() -> tuple[Optional[str], Optional[str]]:
    """Токен аккаунта-владельца — тот же путь, что в бою.

    ``config.runtime.get_photo_upload_token_names()`` → политика токенов
    (``TokenPolicy.token_for_account``): БД — источник истины, env не
    воскрешает выключенный аккаунт.
    """
    from config.runtime import get_photo_upload_token_names
    from database.connection import AsyncSessionLocal
    from modules.vk_token_router import TokenPolicy

    names = get_photo_upload_token_names()
    async with AsyncSessionLocal() as session:
        policy = TokenPolicy(session)
        for name in names:
            candidate = await policy.token_for_account(name)
            if candidate is not None and getattr(candidate, "token", None):
                return name, candidate.token
    return (names[0] if names else None), None


async def _journal_groups(since: str, until: str, limit: int) -> List[int]:
    """Группы из журнала публикаций — те самые, куда шли сборники."""
    from sqlalchemy import text

    from database.connection import AsyncSessionLocal

    sql = text(
        """
        SELECT DISTINCT substring(published_url from 'wall(-?[0-9]+)_') AS gid
        FROM published_posts
        WHERE published_at >= :since AND published_at < :until
          AND kind = 'digest' AND published_url IS NOT NULL
        ORDER BY gid
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
    return [abs(int(r[0])) for r in rows if r and r[0]]


async def _real_photo_bytes(watermark: str, limit: int = 20) -> List[bytes]:
    """Боевой конвейер по РАЗНЫМ реальным фото: скачать → ресайз → знак → шум → JPEG.

    Список, а не одна картинка: в бою каждый файл свой, и вопрос «ломается ли
    заливка на конкретной картинке» иначе не проверяется.
    """
    from sqlalchemy import text

    from database.connection import AsyncSessionLocal
    from modules.publisher.photo_processing import download_photo, process_photo

    sql = text(
        """
        SELECT media FROM collected_post_audit
        WHERE media IS NOT NULL AND media::text LIKE '%"photo"%'
        ORDER BY id DESC LIMIT 120
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql)).all()

    out: List[bytes] = []
    seen: set = set()
    for (media,) in rows:
        if len(out) >= limit:
            break
        try:
            items = media if isinstance(media, list) else json.loads(media)
        except Exception:  # noqa: BLE001 — битые json не повод падать
            continue
        for item in items or []:
            if not isinstance(item, dict) or item.get("type") != "photo":
                continue
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            raw = download_photo(url)
            if not raw:
                continue
            try:
                out.append(process_photo(raw, watermark=watermark))
            except Exception:  # noqa: BLE001 — не то изображение
                continue
            break
    return out


def _upload(
    upload_url: str, data: bytes, filename: str = "digest.jpg"
) -> tuple[int, Dict[str, Any], str]:
    """POST картинки на upload-сервер ВК → (HTTP-код, payload, тело).

    405 приходит не-json'ом (HTML от nginx) — код ответа смотрим отдельно.
    Тело возвращаем строкой: когда json не разобрался, видно, что именно
    ответил сервер (пусто? HTML? ошибка ВК?).
    """
    import requests

    resp = requests.post(
        upload_url,
        files={"photo": (filename, data, "image/jpeg")},
        timeout=30,
    )
    body = ""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — 405 отдаётся не-json'ом
        payload = {}
        body = (getattr(resp, "text", "") or "")[:300]
    return (
        getattr(resp, "status_code", 200),
        payload if isinstance(payload, dict) else {},
        body,
    )


def _first_photo_item(saved: Any) -> Optional[dict]:
    """Первый элемент ``items[]`` ответа ``photos.saveWallPhoto``."""
    if not isinstance(saved, dict):
        return None
    items = saved.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    import vk_api

    name, token = await _owner_token()
    if not token:
        return {"error": f"нет токена аккаунта-владельца ({args.account or 'по умолчанию'})"}

    api = vk_api.VkApi(token=token).get_api()

    groups: List[int] = []
    if args.groups == "journal":
        groups = await _journal_groups(args.since, args.until, args.count)
        if not groups:
            return {"error": "журнал не дал групп за окно — проверь --since/--until"}
    else:
        # Полигон: та же группа N раз подряд — так проверяется темп.
        groups = [abs(int(args.group))] * args.count

    prod_pool: List[bytes] = []
    if args.source == "prod":
        prod_pool = await _real_photo_bytes(args.watermark)
        if not prod_pool:
            return {"error": "не нашлось реальных фото в БД для боевого конвейера"}

    rows: List[Dict[str, Any]] = []
    empty_retries_used = [0]
    started = time.time()
    for i in range(1, len(groups) + 1):
        gid = groups[i - 1]
        if args.source == "prod" and prod_pool:
            data = prod_pool[(i - 1) % len(prod_pool)]
        elif args.mode == "same":
            data = _make_image(1)
        else:
            data = _make_image(i)

        server = api.photos.getWallUploadServer(group_id=gid)
        status, payload, body = _upload(server["upload_url"], data)

        # Ответ с пустым photo — троттлинг ВК. Проверяем, лечится ли он
        # паузой: ровно это и предлагает починка в upload_wall_photo.
        retry_info: List[Dict[str, Any]] = []
        throttled = status != 200 or (bool(payload.get("hash")) and not payload.get("photo"))
        if args.empty_retry_wait and throttled and empty_retries_used[0] < args.empty_retry_max:
            for attempt in range(1, args.empty_retries + 1):
                empty_retries_used[0] += 1
                time.sleep(args.empty_retry_wait)
                server2 = api.photos.getWallUploadServer(group_id=gid)
                status2, payload2, body2 = _upload(server2["upload_url"], data)
                retry_info.append(
                    {
                        "attempt": attempt,
                        "http": status2,
                        "photo": bool(payload2.get("photo")),
                        "hash": bool(payload2.get("hash")),
                        "server": payload2.get("server"),
                    }
                )
                if payload2.get("photo"):
                    break

        row: Dict[str, Any] = {
            "n": i,
            "gid": gid,
            "elapsed_s": round(time.time() - started, 1),
            "http": status,
            "server": payload.get("server"),
            "photo": (payload.get("photo") or "")[:40] or None,
            "hash": bool(payload.get("hash")),
            "keys": sorted(payload.keys()),
        }
        if not payload:
            row["body_kind"] = "empty-or-non-json"
        if body:
            row["body"] = body[:120]
        if retry_info:
            row["empty_retry"] = retry_info

        want_save = args.save_mode == "always" or (
            args.save_mode == "empty-only" and payload.get("hash") and not payload.get("photo")
        )
        if want_save and payload.get("hash"):
            try:
                # Сохранение — **тем же путём, что в бою**: клиент приложения и
                # ``api_call``. Сырой ``vk_api`` зовёт метод иначе (без
                # ``group_id``) и кладёт фото в личный альбом — это другой путь,
                # измерять его нельзя.
                from modules.vk_monitor.vk_client import VKClient

                client = VKClient(token)
                saved = client.api_call(
                    "photos.saveWallPhoto",
                    {
                        "photo": payload.get("photo") or "",
                        "server": payload.get("server"),
                        "hash": payload.get("hash"),
                        "group_id": gid,
                    },
                )
                item = _first_photo_item(saved)
                row["save"] = {
                    "ok": item is not None,
                    "photo_id": (item or {}).get("id"),
                    "owner_id": (item or {}).get("owner_id"),
                    "album_id": (item or {}).get("album_id"),
                }
                if item is None:
                    row["save"]["raw"] = json.dumps(saved, ensure_ascii=False)[:400]
            except Exception as e:  # noqa: BLE001 — это и есть измеряемый сигнал
                row["save"] = {"ok": False, "error": str(e)[:200]}

        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if i < len(groups):
            time.sleep(args.interval)

    ok = sum(1 for r in rows if r.get("photo"))
    saved_ok = sum(1 for r in rows if (r.get("save") or {}).get("ok"))
    return {
        "groups": len(groups),
        "account": name,
        "source": args.source,
        "mode": args.mode,
        "interval_s": args.interval,
        "uploaded_with_photo": ok,
        "uploaded_empty_photo": len(rows) - ok,
        "saveWallPhoto_ok": saved_ok,
        "rows": rows,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Проба заливки фото на стену сообщества")
    p.add_argument("--group", default=os.getenv("VK_TEST_GROUP_ID", "137760500"))
    p.add_argument(
        "--groups",
        choices=["polygon", "journal"],
        default="polygon",
        help="одна группа (полигон) или группы из журнала публикаций",
    )
    p.add_argument("--since", default="2026-11-09 18:00", help="UTC-начало окна журнала")
    p.add_argument("--until", default="2026-11-09 19:30", help="UTC-конец окна журнала")
    p.add_argument(
        "--account", default="", help="имя аккаунта-владельца (по умолчанию — из конфига)"
    )
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--mode", choices=["distinct", "same"], default="distinct")
    p.add_argument("--source", choices=["synthetic", "prod"], default="synthetic")
    p.add_argument("--watermark", default="ТЕСТ", help="текст водяного знака для режима prod")
    p.add_argument(
        "--save-mode",
        choices=["never", "always", "empty-only"],
        default="never",
        help="когда звать photos.saveWallPhoto",
    )
    p.add_argument("--out", default="", help="куда сложить итоговый JSON")
    p.add_argument(
        "--empty-retry-wait",
        type=float,
        default=0.0,
        help="пауза перед ретраем пустого photo (0 — не ретраить)",
    )
    p.add_argument("--empty-retries", type=int, default=3)
    p.add_argument(
        "--empty-retry-max",
        type=int,
        default=1,
        help="сколько пустых photo ретраить за весь прогон (иначе прогон растянется)",
    )
    args = p.parse_args()

    summary = asyncio.run(run(args))
    print("=== ИТОГ ===")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2)
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
