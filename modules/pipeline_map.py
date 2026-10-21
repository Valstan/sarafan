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

ALL_MINUTES = list(range(60))
ALL_HOURS = list(range(24))
ALL_DAYS_OF_WEEK = list(range(7))
ALL_DAYS_OF_MONTH = list(range(1, 32))
ALL_MONTHS = list(range(1, 13))

# Celery нумерует дни недели как в cron: 0 — воскресенье, 1 — понедельник.
# Проверено на живой записи ``classifier-distill-weekly``: `day_of_week=1` там
# помечен комментарием «ночь понедельника».
WEEKDAY_NAMES = {0: "вс", 1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс"}


def _uniform_step(values: Sequence[int]) -> Optional[int]:
    """Шаг равномерного ряда (``*/15`` → 15), иначе ``None``."""
    if len(values) < 2:
        return None
    diffs = {int(b) - int(a) for a, b in zip(values, values[1:])}
    if len(diffs) == 1:
        return diffs.pop()
    return None


def _is_contiguous(values: Sequence[int]) -> bool:
    return len(values) > 1 and (int(values[-1]) - int(values[0])) == len(values) - 1


def _day_factor(
    dow: Optional[Sequence[int]],
    dom: Optional[Sequence[int]],
    month: Optional[Sequence[int]],
) -> float:
    """Доля дней, в которые запись реально срабатывает (1.0 — каждый день)."""
    factor = 1.0
    if dow is not None and set(dow) != set(ALL_DAYS_OF_WEEK):
        factor *= len(set(dow)) / 7
    if dom is not None and set(dom) != set(ALL_DAYS_OF_MONTH):
        factor *= len(set(dom)) / 31
    if month is not None and set(month) != set(ALL_MONTHS):
        factor *= len(set(month)) / 12
    return factor


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


def _cron_parts(schedule: Any) -> Optional[Dict[str, List[int]]]:
    """Поля ``crontab`` как списки чисел; ``None``, если это не crontab."""
    minute = _cron_values(getattr(schedule, "minute", None))
    hour = _cron_values(getattr(schedule, "hour", None))
    if minute is None or hour is None:
        return None

    def _field(name: str) -> Optional[List[int]]:
        return _cron_values(getattr(schedule, name, None))

    return {
        "minutes": minute,
        "hours": hour,
        "dow": _field("day_of_week"),
        "dom": _field("day_of_month"),
        "month": _field("month_of_year"),
    }


def _expand_cron(schedule: Any) -> Optional[Dict[str, Any]]:
    """``crontab`` → минуты/часы/множитель повторяемости; иначе ``None``."""
    parts = _cron_parts(schedule)
    if parts is None:
        return None
    minute = parts["minutes"]
    hour = parts["hours"]
    return {
        "minute": minute[0] if minute else 0,
        "hour": hour[0] if hour else 0,
        "minutes": minute,
        "hours": hour,
        "factor": _day_factor(parts["dow"], parts["dom"], parts["month"]),
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


def _each_minutes(step: int) -> str:
    return "каждую минуту" if step == 1 else f"каждые {step} мин"


def _interval_label(seconds: float) -> str:
    if seconds < 60:
        return f"каждые {round(seconds)} с"
    if seconds < 3600:
        return f"каждые {round(seconds / 60)} мин"
    if seconds < 86400:
        return f"каждые {round(seconds / 3600, 1)} ч"
    return f"каждые {round(seconds / 86400, 1)} дн"


def _cron_label(minutes: Sequence[int], hours: Sequence[int]) -> str:
    """Человеческая подпись расписания — «каждые 15 мин, 08–23», а не «*/15 8-23»."""
    minute_list = sorted({int(m) for m in minutes})
    hour_list = sorted({int(h) for h in hours})
    if not minute_list or not hour_list:
        return "—"
    step_m = _uniform_step(minute_list)
    step_h = _uniform_step(hour_list)

    if hour_list == ALL_HOURS:
        if len(minute_list) == len(ALL_MINUTES):
            return "каждую минуту"
        if step_m:
            return _each_minutes(step_m)
        if len(minute_list) == 1:
            return f"каждый час в :{minute_list[0]:02d}"
        return f"{len(minute_list)} раз в час"

    if len(minute_list) == 1 and len(hour_list) <= 4:
        return ", ".join(f"{h:02d}:{minute_list[0]:02d}" for h in hour_list)
    if len(hour_list) == 1 and len(minute_list) <= 6:
        return ", ".join(f"{hour_list[0]:02d}:{m:02d}" for m in minute_list)
    if len(minute_list) == 1 and step_h and step_h > 1:
        return f"каждые {step_h} ч в :{minute_list[0]:02d}"
    if len(minute_list) == 1:
        return f"каждый час {hour_list[0]:02d}–{hour_list[-1]:02d} " f"в :{minute_list[0]:02d}"
    if step_m and _is_contiguous(hour_list):
        return f"{_each_minutes(step_m)}, {hour_list[0]:02d}–{hour_list[-1]:02d}"
    if _is_contiguous(hour_list):
        return f"{len(minute_list)} мин в час, {hour_list[0]:02d}–{hour_list[-1]:02d}"
    return f"{len(minute_list) * len(hour_list)} зап./сутки"


def _day_prefix(
    dow: Optional[Sequence[int]],
    dom: Optional[Sequence[int]],
    month: Optional[Sequence[int]],
) -> str:
    """Ограничение по дням: «пн», «числа 1, 15», «янв», или пусто."""
    parts: List[str] = []
    if month is not None and set(month) != set(ALL_MONTHS):
        parts.append("мес. " + ",".join(str(m) for m in sorted(set(month))))
    if dom is not None and set(dom) != set(ALL_DAYS_OF_MONTH):
        parts.append("числа " + ",".join(str(d) for d in sorted(set(dom))))
    if dow is not None and set(dow) != set(ALL_DAYS_OF_WEEK):
        parts.append(",".join(WEEKDAY_NAMES.get(int(d), str(d)) for d in sorted(set(dow))))
    return " ".join(parts)


def describe_schedule(schedule: Any) -> Dict[str, Any]:
    """Расписание записи beat словами: подпись, запусков в сутки, вид.

    Подпись строится из самого ``crontab``, а не из комментария рядом с ним:
    комментарий в `beat_schedule` уже врал (см. историю `reconcile-scheduled-
    publications` — там два месяца стояло «каждые 30 мин» при часовом слоте).
    """
    parts = _cron_parts(schedule)
    if parts is None:
        total_seconds = getattr(schedule, "total_seconds", None)
        if callable(total_seconds):
            try:
                seconds = float(total_seconds())
            except Exception:  # noqa: BLE001 — чужой объект расписания
                seconds = 0.0
            if seconds > 0:
                return {
                    "kind": "interval",
                    "label": _interval_label(seconds),
                    "runs_per_day": round(86400.0 / seconds, 2),
                }
        return {"kind": None, "label": "—", "runs_per_day": 0.0}

    minutes, hours = parts["minutes"], parts["hours"]
    factor = _day_factor(parts["dow"], parts["dom"], parts["month"])
    label = _cron_label(minutes, hours)
    prefix = _day_prefix(parts["dow"], parts["dom"], parts["month"])
    if prefix:
        label = f"{prefix} · {label}"
    return {
        "kind": "cron",
        "label": label,
        "runs_per_day": round(len(set(minutes)) * len(set(hours)) * factor, 2),
    }


def periodic_slots(schedule: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Всё расписание, кроме волн: задача, подпись расписания, запусков в сутки.

    Зачем отдельно от волн. Замер P208: периодика — 31 % занятости слота, то
    есть почти треть воркера тратится на то, чего на карте не было видно
    вовсе. Владелец смотрел на волны и не понимал, почему очередь стоит.
    """
    rows: List[Dict[str, Any]] = []
    for name, entry in (schedule or {}).items():
        if not isinstance(entry, dict):
            continue
        task = str(entry.get("task") or "")
        if not task or task.endswith(WAVE_TASK):
            continue
        described = describe_schedule(entry.get("schedule"))
        rows.append(
            {
                "name": name,
                "task": task,
                "task_short": task.split(".")[-1],
                "label": described["label"],
                "kind": described["kind"],
                "runs_per_day": described["runs_per_day"],
                "expires": _entry_expires(entry),
            }
        )
    rows.sort(key=lambda r: (-float(r["runs_per_day"] or 0), str(r["name"])))
    return rows


def periodic_table(
    rows: Sequence[Dict[str, Any]], timings: Optional[Dict[str, Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Периодика + измеренная длительность → «сколько слота съедает в сутки».

    ``timings`` — сводка ``modules.task_timings``: ``{task: {avg_seconds, ...}}``.
    Записи без измерений остаются с ``minutes_per_day = None``: **прочерк
    честнее нуля**, потому что ноль читается как «ничего не занимает».
    """
    measured = timings or {}
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        timing = measured.get(str(item.get("task") or "")) or {}
        avg = timing.get("avg_seconds")
        runs = float(item.get("runs_per_day") or 0)
        item["avg_seconds"] = avg
        item["max_seconds"] = timing.get("max_seconds")
        item["last_seconds"] = timing.get("last_seconds")
        item["samples"] = int(timing.get("samples") or 0)
        item["minutes_per_day"] = round(float(avg) * runs / 60, 1) if avg else None
        out.append(item)
    out.sort(
        key=lambda r: (
            r["minutes_per_day"] is None,
            -float(r["minutes_per_day"] or 0),
            -float(r["runs_per_day"] or 0),
            str(r["name"]),
        )
    )
    return out


def periodic_totals(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Сколько слотовых минут в сутки уходит на периодику — и на чём это стоит."""
    known = [r for r in rows if r.get("minutes_per_day") is not None]
    total = round(sum(float(r["minutes_per_day"]) for r in known), 1)
    return {
        "tasks": len(rows),
        "measured": len(known),
        "minutes_per_day": total,
        "top": known[0]["name"] if known else None,
        "top_minutes": known[0]["minutes_per_day"] if known else None,
    }


__all__ = [
    "WAVE_TASK",
    "attach_regions",
    "describe_schedule",
    "group_by_hour",
    "periodic_slots",
    "periodic_table",
    "periodic_totals",
    "theme_table",
    "totals",
    "wave_slots",
]
