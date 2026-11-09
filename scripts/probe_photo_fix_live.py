"""Приёмка починки P214 на проде: загрузка через задеплоенную функцию.

Гоняет ``modules.publisher.photo_processing.upload_wall_photo`` — тот самый код,
что теперь на боксе — по полигону и печатает, что вернул каждый вызов:

* ``photo<owner>_<id>`` — фото залито (и, значит, троттлинг пройден);
* ``None`` — фото потеряно.

Ничего не публикует: фото остаются в альбоме владельца (полигон — рабочая
площадка, посты на ней не нужны).

Запуск на боксе:

    sudo -n bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA \\
        && PYTHONPATH=/home/valstan/SETKA ./venv/bin/python \\
             scripts/probe_photo_fix_live.py --count 12'
"""

from __future__ import annotations

import argparse
import asyncio
import io
import time

from PIL import Image


def _make_image(seed: int, size=(900, 1200)) -> bytes:
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


async def run(count: int, gid: int) -> None:
    from database.connection import AsyncSessionLocal
    from modules.publisher.photo_processing import upload_wall_photo
    from modules.publisher.vk_publisher_extended import VKPublisher

    async with AsyncSessionLocal() as session:
        publisher = await VKPublisher.create_with_policy(
            session, target_group_id=gid, test_polygon_mode=False
        )
        started = time.time()
        ok = 0
        for i in range(1, count + 1):
            att = await upload_wall_photo(publisher, gid, _make_image(i))
            elapsed = round(time.time() - started, 1)
            if att:
                ok += 1
            print(
                f"[{i}/{count}] {elapsed:>6} c  {'OK  ' if att else 'НЕТ '}{att or ''}",
                flush=True,
            )
    print(f"=== ИТОГ: загружено {ok} из {count} ===")


def main() -> None:
    p = argparse.ArgumentParser(description="Приёмка починки заливки фото на проде")
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--group", type=int, default=137760500)
    args = p.parse_args()
    asyncio.run(run(args.count, args.group))


if __name__ == "__main__":
    main()
