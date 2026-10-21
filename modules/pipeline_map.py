"""Карта конвейера: что и когда запускается, из чего состоит волна, куда идёт.

Заказ владельца 21.10.2026: «создать карту на странице сайта, чтобы я видел, что
и когда работает, из чего состоят волны и когда и куда они идут — хоть как-то
сгруппированно».

**Источник расписания — сам ``app.conf.beat_schedule``, а не копия в шаблоне.**
Копия расходится с расписанием молча, и карта начинает врать ровно тогда, когда
нужна: смотришь на неё, чтобы понять, «почему в 9 утра очередь», и видишь
вчерашнюю правду. Этот класс отказа в проекте уже случался с памятками
(ADR-0001, P192): две копии одного знания расходятся, и расходятся
незаметно.

**Источник цифр выхода — ``parsing_stats``.** Сколько волн реально прошло и
сколько постов они дали за период. Из этого считается «сколько стоит одна
волна» и «сколько волн на один пост» — то, по чему видно, где лишние движения.

Модуль намеренно не ходит в БД и не поднимает Celery: он превращает уже
собранные данные в структуру для страницы и поэтому проверяется тестами без
базы. Сбор данных — ``web/api/pipeline.py``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Задача-веер: одна запись расписания = один запуск на тему, внутри — по задаче
# на регион. Именно её слоты владелец и называет «волнами».
WAVE_TASK = "run_all_regions_theme"


def _cron_values(value: Any) -> Optional[List[int]]:
    """Значения поля crontab как список: у celery это множество, но не всегда."""
    if value is None:
        return None
    if isinstance(value, (set, frozenset, list, tuple)):
        return sorted(int(v) for v in value)
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return None


def _expand_cron(schedule: Any) -> Optional[Dict[str, int]]:
    """``crontab`` → минуты/часы/множитель повторяемости; иначе ``None``."""
    minute = _cron_values(getattr(schedule, "minute", None))
    hour = _cron_values(getattr(schedule, "hour", None))
    if minute is None or hour is None:
        return None

    def _field(name: str, size: int) -> Optional[List[int]]:
        values = _cron_values(getattr(schedule, name, None))
        if values is None:
            return None
        return values

    dow = _field("day_of_week", 7)
    dom = _field("day_of_month", 31)
    month = _field("month_of_year", 12)

    factor = 1.0
    if dow is not None and set(dow) != set(range(7)):
        factor *= len(set(dow)) / 7
    if dom is not None and set(dom) != set(range(1, 32)):
        factor *= len(set(dom)) / 31
    if month is not None and set(month) != set(range(1, 13)):
        factor *= len(set(month)) / 12

    return {
        "minute": minute[0] if minute else 0,
        "hour": hour[0] if hour else 0,
        "minutes": minute,
        "hours": hour,
        "factor": factor,
    }


def _entry_theme(entry: Dict[str, Any]) -> str:
    """Тема волны из ``args``/``kwargs`` записи beat."""
    args = entry.get("args") or ()
    if args:
        return str(args[0])
    kwargs = entry.get("kwargs") or {}
    return str(kwargs.get("theme") or "")


def _entry_expires(entry: Dict[str, Any]) -> Optional[int]:
    options = entry.get("options") or {}
    raw = options.get("expires")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _entry_strict(entry: Dict[str, Any]) -> bool:
    """``strict`` волны: у областных тем он ``True``, и регионов там единицы.

    Промах на этом флаге стоит дорого: проживая 21.10, карта посчитала волны
    областных тем по 75 регионам вместо 2 и показала 2 925 волн в сутки вместо
    реальных ~1 600. Ошибка была в сторону «хуже, чем есть», но это ровно тот
    класс, ради которого карта и строится: цифра выглядит правдоподобно.
    """
    kwargs = entry.get("kwargs") or {}
    if "strict" in kwargs:
        return bool(kwargs.get("strict"))
    args = entry.get("args") or ()
    if len(args) > 1:
        return bool(args[1])
    return False


def region_key(theme: str, strict: bool) -> str:
    """Ключ для числа регионов: тема считается отдельно по strict и loose.

    Один регион может попасть в волну темы и строгим, и мягким гейтом, но
    числа у них разные — поэтому и ключи разные.
    """
    return f"{theme}|{'strict' if strict else 'loose'}"


def wave_slots(schedule: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Слоты волн: одна строка = один запуск веера в сутки (или реже).

    Возвращает по строке на каждый час расписания — так их можно группировать
    по часам и считать нагрузку часа, не разворачивая минуты в декорации.
    """
    rows: List[Dict[str, Any]] = []
    for name, entry in (schedule or {}).items():
        if not isinstance(entry, dict):
            continue
        task = str(entry.get("task") or "")
        if not task.endswith(WAVE_TASK):
            continue
        cron = _expand_cron(entry.get("schedule"))
        if not cron:
            # Интервальные веера в расписании не встречаются; если появятся —
            # честнее не показывать их как «время на часах», чем соврать.
            continue
        for hour in cron["hours"]:
            for minute in cron["minutes"]:
                rows.append(
                    {
                        "name": name,
                        "task": task,
                        "theme": _entry_theme(entry),
                        "strict": _entry_strict(entry),
                        "hour": hour,
                        "minute": minute,
                        "time": f"{hour:02d}:{minute:02d}",
                        "expires": _entry_expires(entry),
                        "per_week": round(cron["factor"], 3),
                    }
                )
    rows.sort(key=lambda r: (r["hour"], r["minute"], r["theme"]))
    return rows


def group_by_hour(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Сгруппировать слоты по часам — так карта читается сверху вниз, как день."""
    buckets: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[int(row["hour"])].append(row)

    out: List[Dict[str, Any]] = []
    for hour in sorted(buckets):
        slots = sorted(buckets[hour], key=lambda r: (r["minute"], r["theme"]))
        themes = sorted({s["theme"] for s in slots if s["theme"]})
        out.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00–{hour:02d}:59",
                "slots": slots,
                "themes": themes,
                "waves": sum(1 for s in slots if s.get("regions")),
                "regions_total": sum(int(s.get("regions") or 0) for s in slots),
            }
        )
    return out


def attach_regions(
    rows: Iterable[Dict[str, Any]], region_counts: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Проставить каждому слоту число регионов, которые он реально заденет.

    ``region_counts`` — по ключу ``region_key(theme, strict)``.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        key = region_key(str(item.get("theme") or ""), bool(item.get("strict")))
        item["regions"] = int(region_counts.get(key, 0))
        out.append(item)
    return out


def theme_table(
    *,
    wave_rows: Sequence[Dict[str, Any]],
    stats: Sequence[Dict[str, Any]],
    region_counts: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Темы: сколько волн в сутки, сколько районов, что это дало за период.

    ``stats`` — уже агрегированные строки ``parsing_stats`` по темам:
    ``{theme, runs, published, avg_seconds, max_run_date}``.

    Колонка «волн на пост» — главное число карты: во сколько волн обходится
    один опубликованный пост. Единица означает «каждая волна что-то принесла»,
    десять — «девять из десяти были разведкой».
    """
    by_theme_stats = {str(row.get("theme") or ""): row for row in stats}
    slots_per_day: Dict[str, float] = defaultdict(float)
    waves_per_day: Dict[str, float] = defaultdict(float)
    regions_of_theme: Dict[str, int] = defaultdict(int)
    for row in wave_rows:
        theme = str(row.get("theme") or "")
        per_week = float(row.get("per_week") or 1.0)
        regions = int(row.get("regions") or 0)
        slots_per_day[theme] += per_week
        waves_per_day[theme] += per_week * regions
        regions_of_theme[theme] = max(regions_of_theme[theme], regions)

    themes = sorted(
        set(slots_per_day) | set(by_theme_stats),
        key=lambda t: (-slots_per_day.get(t, 0), t),
    )

    out: List[Dict[str, Any]] = []
    for theme in themes:
        if not theme:
            continue
        st = by_theme_stats.get(theme) or {}
        runs = int(st.get("runs") or 0)
        published = int(st.get("published") or 0)
        regions = int(regions_of_theme.get(theme, 0))
        per_day = slots_per_day.get(theme, 0.0)
        out.append(
            {
                "theme": theme,
                "slots_per_day": round(per_day, 2),
                "regions": regions,
                "waves_per_day": round(waves_per_day.get(theme, 0.0), 1),
                "runs": runs,
                "published": published,
                "avg_seconds": round(float(st.get("avg_seconds") or 0), 1),
                "waves_per_post": round(runs / published, 1) if published else None,
                "yield_pct": round(published / runs * 100, 1) if runs else None,
                "last_run": st.get("max_run_date"),
            }
        )
    return out


def totals(
    *,
    wave_rows: Sequence[Dict[str, Any]],
    themes: Sequence[Dict[str, Any]],
    busy_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Сводка «сколько стоит день» — для шапки карты."""
    slots_per_day = round(sum(float(r.get("per_week") or 1.0) for r in wave_rows), 1)
    waves = round(sum(float(t.get("waves_per_day") or 0) for t in themes), 1)
    return {
        "slots_per_day": slots_per_day,
        "waves_per_day": waves,
        "themes": len([t for t in themes if t.get("slots_per_day")]),
        "busy_hours": round(busy_hours, 1) if busy_hours is not None else None,
        "share_of_day": round(busy_hours / 24 * 100, 1) if busy_hours is not None else None,
    }


__all__ = [
    "WAVE_TASK",
    "attach_regions",
    "group_by_hour",
    "theme_table",
    "totals",
    "wave_slots",
]
