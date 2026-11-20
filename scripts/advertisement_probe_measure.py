#!/usr/bin/env python3
"""Замер «фактической цены» снятия отсечки ``advertisement`` (P091, проба на одном районе).

**Что это.** Отсечка ``advertisement`` режет посты ДО того, как движок их увидит.
Сколько из них дошло бы до ленты — вопрос контрфактический: их уже нет в потоке.
Поэтому цена меряется **фактом**: сняли отсечку в одном районе → смотрим, сколько
постов/сутки добавилось в ленту этого района, и вычитаем общий тренд по остальным
районам (иначе сеть и так движется — 02.11 был fail-open).

Метод — тот же, что в замере 19.11 (P091): по **разным** ``lip``, окно до пробы
против окна пробы, исключение новых регионов, вечерний сборник (``kind='digest'``)
не в счёт.

⚠️ **Время в базе — разное в разных таблицах** (P209), и это ломает счёт, если
не знать:

- ``collected_post_audit.collected_at`` и ``published_posts.published_at`` —
  **локальное время бокса (MSK)**: пишется ``DEFAULT now()``;
- ``content_classifications.created_at`` — **UTC**: в модели явный
  ``datetime.utcnow``.

Окна здесь считаются по локальному MSK: граница окна — просто ``now()`` **без
сдвига**, а ``--since`` — полночь по МСК. Первая версия скрипта сдвигала границу
на −3 часа «как будто в базе UTC» — из-за этого окно 19.11 вышло неполным
(обрезался хвост суток) и дневные числа поехали на 3 часа.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/advertisement_probe_measure.py \
            --region kotelnich --since 2026-11-20 --days 7'

Аргументы:
  --region   код пробного района (обязательно);
  --since    дата начала пробы, МСК (YYYY-MM-DD);
  --days     длина базового окна до пробы (по умолчанию 7);
  --control  коды контрольных районов через запятую (по умолчанию — все прочие
             районы, у которых есть собранные посты в базовом окне).

Только чтение.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from typing import Dict, List

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from modules.prod_env import load_prod_env  # noqa: E402

#: Отсечка, которую снимаем на пробе.
DROP_REASON = "advertisement"


async def _db_now_local(session) -> datetime:
    """Сейчас в локальном времени БД (MSK) — как пишутся колонки аудита."""
    return await session.scalar(text("SELECT (now() AT TIME ZONE current_setting('TimeZone'))"))


def _msk_midnight(date_str: str) -> datetime:
    """Дата (МСК) → полночь этой даты в локальном времени базы."""
    return datetime.strptime(date_str, "%Y-%m-%d")


async def _collect(session, region: str, start: datetime, end: datetime) -> Dict[str, int]:
    """Сбор и отсечки по одному району за окно."""
    q = text(
        """
        SELECT
          count(*) FILTER (WHERE a.decision = 'kept')                   AS kept,
          count(*) FILTER (WHERE a.drop_reason = :reason)              AS dropped,
          count(*) FILTER (WHERE a.drop_reason = :reason
                             AND c.lip IS NOT NULL)                     AS dropped_with_verdict,
          count(*) FILTER (WHERE a.drop_reason = :reason
                             AND c.verdict->>'action' = 'publish')      AS dropped_publish
        FROM collected_post_audit a
        LEFT JOIN content_classifications c ON c.lip = a.lip
        WHERE a.region_code = :region
          AND a.collected_at >= :start AND a.collected_at < :end
        """
    )
    row = (
        (
            await session.execute(
                q, {"region": region, "start": start, "end": end, "reason": DROP_REASON}
            )
        )
        .mappings()
        .one()
    )
    return {k: int(v or 0) for k, v in dict(row).items()}


async def _published(session, region: str, start: datetime, end: datetime) -> int:
    """Сколько постов реально вышло в ленту района за окно (без сборников)."""
    q = text(
        """
        SELECT count(*)
        FROM published_posts
        WHERE region_code = :region
          AND published_at >= :start AND published_at < :end
          AND coalesce(kind, 'post') <> 'digest'
        """
    )
    return int(
        (await session.execute(q, {"region": region, "start": start, "end": end})).scalar() or 0
    )


async def _published_by_region(
    session, regions: List[str], start: datetime, end: datetime
) -> Dict[str, int]:
    """Публикации по списку районов за окно."""
    if not regions:
        return {}
    q = text(
        """
        SELECT region_code, count(*) AS cnt
        FROM published_posts
        WHERE region_code = ANY(:regions)
          AND published_at >= :start AND published_at < :end
          AND coalesce(kind, 'post') <> 'digest'
        GROUP BY 1
        """
    )
    rows = (
        (await session.execute(q, {"regions": regions, "start": start, "end": end}))
        .mappings()
        .all()
    )
    return {str(r["region_code"]): int(r["cnt"] or 0) for r in rows}


async def _audited_regions(session, start: datetime, end: datetime) -> List[str]:
    """Районы, у которых в базовом окне есть собранные посты (кандидаты в контроль)."""
    q = text(
        """
        SELECT region_code, count(*) AS cnt
        FROM collected_post_audit
        WHERE collected_at >= :start AND collected_at < :end
        GROUP BY 1
        ORDER BY cnt DESC
        """
    )
    rows = (await session.execute(q, {"start": start, "end": end})).mappings().all()
    return [str(r["region_code"]) for r in rows if int(r["cnt"] or 0) > 0]


def _per_day(count: int, days: float) -> float:
    return round(count / days, 2) if days > 0 else 0.0


async def main() -> int:
    parser = argparse.ArgumentParser(description="замер цены снятия отсечки advertisement")
    parser.add_argument("--region", required=True, help="код пробного района")
    parser.add_argument("--since", required=True, help="дата начала пробы (МСК, YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=7, help="длина базового окна до пробы")
    parser.add_argument("--control", default="", help="коды контрольных районов через запятую")
    parser.add_argument(
        "--prod",
        action="store_true",
        help="подхватить env-файлы прод-бокса (нужен root): DATABASE_URL и прочее",
    )
    args = parser.parse_args()

    if args.prod:
        load_prod_env()

    # Импорт соединения — ПОСЛЕ загрузки env: модуль читает DATABASE_URL при
    # импорте и без env падает с «DATABASE_URL is not set».
    from database.connection import AsyncSessionLocal  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        now_local = await _db_now_local(session)
        probe_start = _msk_midnight(args.since)
        base_start = probe_start - timedelta(days=args.days)
        probe_days = max((now_local - probe_start).total_seconds() / 86400.0, 0.5)
        base_days = float(args.days)

        control = [c.strip() for c in args.control.split(",") if c.strip()]
        if not control:
            audited = await _audited_regions(session, base_start, probe_start)
            control = [r for r in audited if r != args.region]
        if not control:
            print("нет контрольных районов: не с чем сравнивать")
            return 2

        probe_base = await _collect(session, args.region, base_start, probe_start)
        probe_probe = await _collect(session, args.region, probe_start, now_local)
        pub_region_base = await _published(session, args.region, base_start, probe_start)
        pub_region_probe = await _published(session, args.region, probe_start, now_local)

        pub_control_base = await _published_by_region(session, control, base_start, probe_start)
        pub_control_probe = await _published_by_region(session, control, probe_start, now_local)

        # Публикации контроля: сумма и среднее по району (на каждый район одинаково
        # не приходится — у них разный поток, поэтому сравниваем средние).
        ctl_base_total = sum(pub_control_base.values())
        ctl_probe_total = sum(pub_control_probe.values())
        ctl_regions = len(control)
        ctl_base_per = _per_day(ctl_base_total / ctl_regions if ctl_regions else 0, base_days)
        ctl_probe_per = _per_day(ctl_probe_total / ctl_regions if ctl_regions else 0, probe_days)

        reg_base_per = _per_day(pub_region_base, base_days)
        reg_probe_per = _per_day(pub_region_probe, probe_days)
        did = (reg_probe_per - reg_base_per) - (ctl_probe_per - ctl_base_per)

    print("=== Проба снятия отсечки '%s' ===" % DROP_REASON)
    print(
        "район: %s | проба с %s | база %d суток | окно пробы %.1f суток"
        % (args.region, args.since, args.days, probe_days)
    )
    print("контроль: %d районов" % ctl_regions)
    print("время: локальное бокса (MSK) — так пишутся collected_at/published_at")

    print("\n-- сбор по пробному району --")
    for label, win in (("база", probe_base), ("проба", probe_probe)):
        print(
            "  %-6s собрано %5d | отброшено как advertisement %5d | из них с вердиктом %5d"
            " | вердикт publish %5d (%.0f%%)"
            % (
                label,
                win["kept"],
                win["dropped"],
                win["dropped_with_verdict"],
                win["dropped_publish"],
                (100.0 * win["dropped_publish"] / win["dropped"]) if win["dropped"] else 0.0,
            )
        )

    print("\n-- публикации в ленту --")
    print(
        "  пробный район: база %.2f/сутки → проба %.2f/сутки (%.2f)"
        % (reg_base_per, reg_probe_per, reg_probe_per - reg_base_per)
    )
    print(
        "  контроль (среднее по району): база %.2f/сутки → проба %.2f/сутки (%.2f)"
        % (ctl_base_per, ctl_probe_per, ctl_probe_per - ctl_base_per)
    )

    print("\n=== ЦЕНА ПРОБЫ ===")
    print("  %+.2f поста/сутки в пробном районе относительно контроля" % did)
    if did > 0:
        print(
            "  (за %d суток пробы это примерно %+.0f постов в ленту района)"
            % (round(probe_days), did * probe_days)
        )
    else:
        print("  (разницы нет или она отрицательная — значит, снятие отсечки ленту не раздуло)")
    print("\n  Метод: по разным lip, окно до пробы против окна пробы, сборники исключены.")
    print("  Прод-доступ: только чтение; ничего не менял.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
