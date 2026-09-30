#!/usr/bin/env python3
"""Ручная доставка контент-конвейера: вердикты глазами, пока лежит DeepSeek.

Зачем он: до DeepSeek-402 ручной прогон жил отдельным скриптом в ``/tmp`` на
боксе. Он был потерян вместе с ``/tmp``, а вместе с ним — и весь разбор:
почему первый lip партии остался в журнале со статусом ``selected`` при
ответе приёмника 201. Рецепт, который живёт только в ``/tmp``, не является
рецептом: следующий прогон начинается с нуля.

Этот скрипт — **не копия конвейера**, а один из режимов штатного прогона:
подготовленные вердикты подставляются в тот же ``runner.run_site``, проходят
те же гейты (``classify.parse_verdict`` — сторож на пустой заголовок и на
раздувшийся текст) и пишутся в тот же журнал. Отличие одно и оно видно в
журнале: отказ человека помечен ``manual_*``, отказ модели — ``llm_*``.

Три режима, ровно один обязателен:

``--emit FILE``
    Отбор и выгрузка кандидатов целиком (текст, медиа, дата) в JSON. Ничего
    не пишет в журнал, наружу не отправляет, модель не зовёт. Из этого файла
    потом заполняется поле ``verdicts``.

``--check FILE``
    Разбор файла вердиктов против текущего отбора: что уедет, что останется без
    вердикта, какие вердикты не проходят гейты, какие ``lip`` в файле не
    берутся сайтом. Ничего не пишет и не отправляет. Ноль изменений — не пустой
    отчёт, а ненулевой код возврата.

``--deliver FILE``
    Доставка. Пишет журнал и отправляет приёмнику, затем **перечитывает журнал
    и сверяет построчно** — расхождение печатается и даёт код возврата 2.

Почему сверка обязательна (а не «повыводить и посмотреть»): отчёт скрипта и
журнал — разные источники, и именно они расходились. Скрипт рапортовал
``delivered/201 с remote_id``, а строка оставалась ``selected`` — причина так и
не найдена, лечилось одиночным повтором. Пока причина не найдена, единственная
защита — сверять после каждого прогона и не давать прогону уйти с exit 0,
разойдясь с журналом (G158: «скрипт отработал, ничего не сделал»).

Запуск на боксе под env приложения (``DATABASE_URL`` живёт в env-файле, ключи
доставки сайтов — в комнате КАРМАНа и подтягиваются bootstrap'ом):

    cd /home/valstan/SETKA
    sudo -n bash -c 'set -a
        . /etc/setka/secrets-token.env; . /etc/setka/setka.env; set +a
        venv/bin/python scripts/conveyor_manual_deliver.py
        --site kultura --emit /tmp/kultura.json'

    # прочитать /tmp/kultura.json, заполнить verdicts, затем:
    sudo -n bash -c 'set -a
        . /etc/setka/secrets-token.env; . /etc/setka/setka.env; set +a
        venv/bin/python scripts/conveyor_manual_deliver.py
        --site kultura --check /tmp/kultura.json'
    # и только после зелёной сверки:
    sudo -n bash -c 'set -a
        . /etc/setka/secrets-token.env; . /etc/setka/setka.env; set +a
        venv/bin/python scripts/conveyor_manual_deliver.py
        --site kultura --deliver /tmp/kultura.json'

⚠️ **env читается внутри `sudo -n bash -c`, а не снаружи.** Файлы
`/etc/setka/*.env` — `600 root:root`, поэтому `sudo venv/bin/python` без
`set -a` внутри того же шелла видит пустое окружение и падает на
``DATABASE_URL is not set`` (проверено на живом прогоне 30.09). Отсюда же
вторая ошибка того же класса: `cd ~/SETKA` под sudo даёт `/root`, поэтому
каталог в команде — абсолютный.

Канон режима: **доставка требует явного ``--deliver``** (#264 — разовый скрипт
наследует режим окружения, а не намерение автора). Скрипт без флага отправляет
на сайт «что-нибудь» по остаточному принципу; отказать — дешевле.

Коды возврата: 0 — всё сошлось; 1 — не тот режим/сайт/файл; 2 — прогон состоялся,
но разошёлся с журналом или с планом.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Статус журнала, который обязан получить lip после прогона. Не совпадение с
# bucket делает расхождением: «доставлено по отчёту, в журнале selected» —
# ровно тот случай, который стоило поймать.
_EXPECTED_STATUS = {
    "delivered": "delivered",
    "rejected": "rejected",
    "held": "held",
    "failed": "failed",
}


def load_verdicts(path: str) -> Dict[str, Dict[str, Any]]:
    """Прочитать карту ``lip → вердикт`` из файла режима.

    Понимает два вида содержимого: ``{"verdicts": {...}}`` (файл, выгруженный
    ``--emit`` — вердикты дописываются рядом с кандидатами) и «голую» карту
    ``{"<lip>": {...}}`` (вердикты написаны с нуля). Смешивать нельзя: если в
    файле есть ``candidates``, но нет ``verdicts``, это не пустой план, а
    недописанный — и молчать об этом нельзя.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("файл вердиктов должен быть JSON-объектом")
    if "verdicts" in raw or "candidates" in raw:
        verdicts = raw.get("verdicts")
        if verdicts is None:
            raise ValueError(
                "в файле есть кандидаты, но нет поля verdicts — заполните его "
                '(формат: {"lip": {"action": "accept|reject", ...}})'
            )
    else:
        verdicts = raw
    if not isinstance(verdicts, dict):
        raise ValueError("поле verdicts должно быть объектом lip → вердикт")
    out: Dict[str, Dict[str, Any]] = {}
    for lip, verdict in verdicts.items():
        if not isinstance(verdict, dict):
            raise ValueError(f"вердикт для {lip} должен быть объектом, получено {type(verdict)}")
        out[str(lip)] = verdict
    return out


def _candidates_path(file_name: str, lips: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Найти тексты кандидатов в файле режима ``--emit`` (если он там есть)."""
    raw = json.loads(Path(file_name).read_text(encoding="utf-8"))
    items = (raw or {}).get("candidates") or []
    return {str(c.get("lip")): c for c in items if isinstance(c, dict) and c.get("lip")}


async def plan(
    session,
    site: Dict[str, Any],
    verdicts: Dict[str, Dict[str, Any]],
    *,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Разобрать вердикты против текущего отбора. Ничего не пишет и не шлёт.

    Гейты здесь — те же, что на живой доставке: ``parse_verdict`` и
    ``check_invariant`` на собранном теле. План, который прошёл бы на
    доставке и не прошёл бы здесь, означал бы, что план врёт.
    """
    from modules.conveyor import classify as classify_mod
    from modules.conveyor import delivery as delivery_mod
    from modules.conveyor import runner as runner_mod
    from modules.conveyor import source as source_mod

    stats = await runner_mod.run_site(session, site, days=days, limit=limit, dry_run=True)
    preview = stats.get("preview") or []
    posts = await source_mod.fetch_audit_snapshots(session, lips=[p["lip"] for p in preview])
    texts = _candidates_path(file_name, [p["lip"] for p in preview]) if file_name else {}

    sections = site.get("sections") or ()
    publish = bool(site.get("publish_key"))
    rows: List[Dict[str, Any]] = []
    for item in preview:
        lip = str(item.get("lip") or "")
        verdict = verdicts.get(lip)
        if verdict is None:
            rows.append({"lip": lip, "verdict": "нет вердикта", "detail": "останется в отборе"})
            continue
        # Снапшот из аудита — тот же словарь, который пойдёт в доставку. Собирать
        # свой сокращённый пост здесь нельзя: гейт тогда проверяет не то тело,
        # которое уедет, и план врёт (пойман тестом на отсутствующем `url`).
        post = posts.get(lip) or texts.get(lip)
        if not post:
            rows.append({"lip": lip, "verdict": "нечем проверять", "detail": "поста нет в аудите"})
            continue
        parsed, refusal = classify_mod.parse_verdict(verdict, post, sections=sections)
        if parsed is None:
            rows.append(
                {"lip": lip, "verdict": "отказ", "detail": runner_mod._manual_reason(refusal)}
            )
            continue
        body = delivery_mod.build_payload(
            post,
            parsed,
            date_iso=delivery_mod.iso_date(post.get("published_at")),
            publish=publish,
        )
        bad = delivery_mod.check_invariant(body)
        if bad:
            rows.append({"lip": lip, "verdict": "задержан", "detail": bad})
            continue
        rows.append(
            {
                "lip": lip,
                "verdict": "уедет",
                "detail": f"{parsed.get('section') or '—'} · {parsed.get('title') or ''}"[:80],
            }
        )

    selected = {str(p.get("lip") or "") for p in preview}
    unknown = sorted(set(verdicts) - selected)
    return {"site": stats.get("site"), "rows": rows, "unknown_lips": unknown, "stats": stats}


async def reconcile(
    session,
    site_key: str,
    results: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Перечитать журнал и найти строки, разошедшиеся с отчётом прогона.

    Пустой список — согласие двух источников. Непустой — реальная поломка
    (обычно «доставлено по отчёту, в журнале ``selected``»), и она обязана
    быть видна в коде возврата, а не в глазном сравнении двух распечаток.
    """
    from sqlalchemy import select

    from database.models_extended import ConveyorDelivery

    lips = [str(r.get("lip") or "") for r in results if r.get("lip")]
    if not lips:
        return []
    rows = (
        (
            await session.execute(
                select(ConveyorDelivery)
                .where(ConveyorDelivery.site == site_key)
                .where(ConveyorDelivery.lip.in_(lips))
            )
        )
        .scalars()
        .all()
    )
    actual = {r.lip: r for r in rows}

    bad: List[Dict[str, Any]] = []
    for res in results:
        lip = str(res.get("lip") or "")
        if not lip:
            continue
        row = actual.get(lip)
        want = _EXPECTED_STATUS.get(str(res.get("bucket") or ""))
        if want is None:
            continue
        got = getattr(row, "status", None)
        if got != want:
            bad.append(
                {
                    "lip": lip,
                    "reported": res.get("bucket"),
                    "journal": got or "нет строки",
                    "http_status": res.get("http_status"),
                    "remote_id": res.get("remote_id"),
                }
            )
    return bad


def render_plan(report: Dict[str, Any]) -> str:
    lines = [f"site={report.get('site')} кандидатов={len(report.get('rows') or [])}"]
    for row in report.get("rows") or []:
        lines.append(f"  - {row['lip']}: {row['verdict']} — {row['detail']}")
    for lip in report.get("unknown_lips") or []:
        lines.append(f"  ! вердикт есть, а отбор взял не {lip} — вердикт не применится")
    return "\n".join(lines)


def render_delivery(stats: Dict[str, Any], mismatches: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "site={site} selected={selected} delivered={delivered} rejected={rejected} "
        "held={held} failed={failed} без вердикта={skipped}".format(
            site=stats.get("site"),
            selected=stats.get("selected"),
            delivered=stats.get("delivered"),
            rejected=stats.get("rejected"),
            held=stats.get("held"),
            failed=stats.get("failed"),
            skipped=stats.get("skipped"),
        )
    ]
    for res in stats.get("results") or []:
        detail = res.get("reason") or res.get("remote_id") or ""
        lines.append(f"  - {res.get('lip')}: {res.get('bucket')} {detail}")
    for lip in stats.get("without_verdict") or []:
        lines.append(f"  ~ {lip}: вердикта не было — остаётся в отборе на следующий раз")
    if mismatches:
        lines.append("РАСХОЖДЕНИЕ С ЖУРНАЛОМ (доставить одиночкой):")
        for m in mismatches:
            lines.append(
                f"  ! {m['lip']}: отчёт={m['reported']} журнал={m['journal']} "
                f"http={m['http_status']} remote_id={m['remote_id']}"
            )
    return "\n".join(lines)


def _iso(value: Any) -> Optional[str]:
    """Дата в ISO-строке.

    Отдельная функция, потому что ``published_at`` из БД — это ``datetime``, а
    ``json.dumps`` такой объект не берёт: файл просто не писался. На тестах это
    не всплывало — там дата была ``None``, а на живых данных она всегда есть.
    """
    if value is None or isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _emit_candidate(post: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(post)
    out["published_at"] = _iso(post.get("published_at"))
    return out


def _emit_payload(site_key: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "site": site_key,
        "note": (
            'Заполни поле verdicts: {"<lip>": {"action": "accept", "section": ..., '
            '"title": ..., "text": ...}} или {"action": "reject", "reason": ...}. '
            "Формат ровно тот, что отдаёт модель, — гейты общие."
        ),
        "candidates": [_emit_candidate(p) for p in stats.get("preview") or []],
        "duplicates": stats.get("preview_duplicates") or [],
        "verdicts": {},
    }


def _write_file(path: str, payload: Dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


async def _amain(args, site: Dict[str, Any], mode: str, file_name: str) -> int:
    """Вся работа режима. Отделено от ``main``, который только разбирает аргументы.

    Разделение не для красоты: ``main`` зовёт ``asyncio.run``, а вызвать его из
    уже идущей петли нельзя — значит, ветки с БД иначе нечем проверить, кроме
    как запуском скрипта руками. Именно так и вышло: ветка ``--emit``
    осталась непокрытой и держала баг, который виден только при запуске целиком.
    """
    from database.connection import AsyncSessionLocal
    from modules.conveyor import runner as runner_mod

    site_key = str(site.get("key") or args.site)

    async def with_session(coro_factory):
        async with AsyncSessionLocal() as sess:
            return await coro_factory(sess)

    if mode == "emit":

        async def go(sess):
            return await runner_mod.run_site(
                sess, site, days=args.days, limit=args.limit, dry_run=True, dry_full=True
            )

        try:
            stats = await with_session(go)
        except Exception as e:  # noqa: BLE001 — CLI обязан сказать, а не упасть молча
            print(f"отбор не выполнился: {e}", file=sys.stderr)
            return 1
        payload = _emit_payload(site_key, stats)
        _write_file(file_name, payload)
        print(
            f"site={site_key} кандидатов={len(payload['candidates'])} "
            f"дублей={len(payload['duplicates'])} → {file_name}"
        )
        return 0

    try:
        verdicts = load_verdicts(file_name)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"файл вердиктов не прочитан: {e}", file=sys.stderr)
        return 1

    if mode == "check":

        async def go_plan(sess):
            return await plan(
                sess, site, verdicts, days=args.days, limit=args.limit, file_name=file_name
            )

        try:
            report = await with_session(go_plan)
        except Exception as e:  # noqa: BLE001
            print(f"разбор не выполнился: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, ensure_ascii=False, default=str))
        else:
            print(render_plan(report))
        rows = report.get("rows") or []
        not_ready = [r for r in rows if r["verdict"] != "уедет"] + [
            {"lip": lip} for lip in (report.get("unknown_lips") or [])
        ]
        if not rows and not verdicts:
            print("отбор пуст — вердикты не по чему писать", file=sys.stderr)
            return 1
        return 1 if not_ready else 0

    async def go_deliver(sess):
        stats = await runner_mod.run_site(
            sess,
            site,
            days=args.days,
            limit=args.limit,
            verdicts=verdicts,
            collect_results=True,
        )
        mismatches = await reconcile(sess, site_key, stats.get("results") or [])
        return stats, mismatches

    try:
        stats, mismatches = await with_session(go_deliver)
    except Exception as e:  # noqa: BLE001
        print(f"доставка не выполнена: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps({"stats": stats, "mismatches": mismatches}, ensure_ascii=False, default=str)
        )
    else:
        print(render_delivery(stats, mismatches))
    if mismatches:
        print("журнал разошёлся с отчётом — см. расхождение выше", file=sys.stderr)
        return 2
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", required=True, help="ключ сайта из SITES (напр. kultura)")
    parser.add_argument("--emit", metavar="FILE", help="выгрузить кандидатов в FILE")
    parser.add_argument(
        "--check", metavar="FILE", help="разобрать вердикты из FILE, ничего не шлёт"
    )
    parser.add_argument("--deliver", metavar="FILE", help="доставить по вердиктам из FILE")
    parser.add_argument("--days", type=int, default=None, help="окно свежести, сутки")
    parser.add_argument("--limit", type=int, default=None, help="потолок постов")
    parser.add_argument("--json", action="store_true", help="вывести сводку JSON")
    args = parser.parse_args(argv)

    chosen = [(m, getattr(args, m)) for m in ("emit", "check", "deliver") if getattr(args, m)]
    if len(chosen) != 1:
        print(
            "нужен ровно один режим: --emit FILE | --check FILE | --deliver FILE",
            file=sys.stderr,
        )
        return 1
    mode, file_name = chosen[0]

    from config.content_conveyor import get_site

    site = get_site(args.site)
    if site is None:
        print(f"нет сайта с ключом {args.site!r}", file=sys.stderr)
        return 1

    return asyncio.run(_amain(args, site, mode, file_name))


if __name__ == "__main__":
    raise SystemExit(main())
