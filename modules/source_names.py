"""Официальные названия сообществ-доноров для подписи «Источник: …».

Зачем этот модуль. В ``communities.name`` лежат названия, которые владелец
правил руками: частью они сокращены, частью обрезаны на полуслове
(«СДК села Рожки Малмыжского района Кировской обла»). Решение владельца 20.10:
в публикациях писать название так, как сообщество называет себя в VK, а ручные
сокращения не использовать. Чистить базу при этом не нужно — данные остаются,
меняется только то, откуда берётся подпись.

Как берём. ``groups.getById`` по тем сообществам, чьи посты реально идут в
эту волну (десятки id, один вызов на волну), с кэшем в процессе воркера на
несколько часов. Любой отказ VK — не повод не публиковать: остаётся прежнее
имя из базы, а волна идёт дальше.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Название сообщества меняется нечасто; 6 часов — компромисс между свежестью
# и тем, чтобы не звать VK на каждой волне.
CACHE_TTL_SECONDS = 6 * 3600

# Буквы, из которых состоит пригодная подпись источника: кириллица и латиница.
# Татарские/казахские имена на латинице тоже проходят.
_PRINTABLE_LETTER = re.compile(r"[A-Za-zА-Яа-яЁёӘәҖҗҢңӨөҮүҺһІі]")

# CJK-диапазоны. ``groups.getById`` умеет вернуть вместо названия строку в
# чужой кодировке — живая группа 234960216 отдала `习薪摆废赘蒲` (проверено
# 21.10.2026). Печатать такое в постах нельзя.
_CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _usable_name(name: str) -> bool:
    """Годится ли официальное имя в подпись «Источник: …».

    Причина проверки — не косметика: имя приходит из VK, а VK для части
    сообществ отдаёт мусор (бан, переименование в чужую кодировку). Без заслона
    подпись в публикации выглядела бы как набор иероглифов, и заметить это можно
    было бы только глазами в ленте. Негодное имя не заменяет прежнее: остаётся
    то, что лежит в базе.
    """
    if _CJK.search(name):
        return False
    return bool(_PRINTABLE_LETTER.search(name))


_CACHE: Dict[int, "tuple[float, str]"] = {}
_LOCK = threading.Lock()


def reset_cache() -> None:
    """Сбросить кэш (тесты и ручная диагностика)."""
    with _LOCK:
        _CACHE.clear()


def _post_owner_id(post: Dict[str, Any]) -> Optional[int]:
    try:
        owner_id = int(post.get("owner_id", post.get("from_id", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if not owner_id:
        return None
    return owner_id


def _load_cached(group_id: int, now: float) -> Optional[str]:
    with _LOCK:
        cached = _CACHE.get(group_id)
    if not cached:
        return None
    saved_at, name = cached
    if now - saved_at > CACHE_TTL_SECONDS:
        return None
    return name


def _store(group_id: int, name: str, now: float) -> None:
    with _LOCK:
        _CACHE[group_id] = (now, name)


def fetch_official_names(
    vk_client: Any,
    group_ids: Iterable[int],
    *,
    now: Optional[float] = None,
) -> Dict[int, str]:
    """Положительные id сообществ → официальные названия (с кэшем).

    Ошибки VK и отсутствующие в ответе сообщества просто не попадают в
    результат: вызывающий оставляет прежнее имя.
    """
    now = time.time() if now is None else now
    wanted = {abs(int(gid)) for gid in group_ids if gid}
    if not wanted:
        return {}

    out: Dict[int, str] = {}
    missing = []
    for group_id in wanted:
        name = _load_cached(group_id, now)
        if name:
            out[group_id] = name
        else:
            missing.append(group_id)
    if not missing:
        return out

    getter = getattr(vk_client, "get_groups_by_ids", None)
    if getter is None:
        return out
    try:
        items = getter(missing) or []
    except Exception:  # pragma: no cover - сеть/VK, публикацию не валит
        logger.debug("groups.getById для подписи источника не удался", exc_info=True)
        return out

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            group_id = abs(int(item.get("id")))
        except (TypeError, ValueError):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name or len(name) < 2:
            continue
        if not _usable_name(name):
            logger.info(
                "groups.getById: имя %s не годится в подпись (%r) — остаётся имя из базы",
                group_id,
                name[:80],
            )
            continue
        out[group_id] = name
        _store(group_id, name, now)
    return out


def enrich_group_names(
    vk_client: Any,
    group_names: Dict[str, str],
    posts: Iterable[Dict[str, Any]],
) -> Dict[str, str]:
    """Перекрыть ``group_names`` официальными названиями для постов волны.

    Ключи — ``str(abs(owner_id))``, как в существующем словаре
    ``tasks.parsing_scheduler_tasks``. Посты личных страниц (положительный
    owner_id) пропускаются: ``groups.getById`` их не знает, а имя из базы —
    единственное, что есть.
    """
    candidates = []
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        owner_id = _post_owner_id(post)
        if owner_id is not None and owner_id < 0:
            candidates.append(owner_id)
    if not candidates:
        return dict(group_names or {})

    official = fetch_official_names(vk_client, candidates)
    if not official:
        return dict(group_names or {})

    merged = dict(group_names or {})
    for group_id, name in official.items():
        merged[str(group_id)] = name
    return merged
