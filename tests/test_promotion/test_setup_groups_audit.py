"""Отбор целей ремонта — по живому ВК, а не по строке журнала.

Заказ владельца 2026-09-06: «дооформить группы, в которых нет обложек и
аватаров; в которых есть — не трогать». Ответить на это ни один из двух
имевшихся источников не мог:

* **dry-run печатает режим**, а режим — конъюнкция (``full`` = нет аватара И нет
  обложки И описание короткое). Сообщество с обложкой и без аватара попадало в
  ``spot`` — «авторское оформление, не трогаем». Условие не покрывает область
  вопроса (#284);
* **``promo_group_setup.status``** отвечает на «чем кончился прогон», а не «что
  сейчас в ВК», и расходился с явью дважды 01.09 (у ``nagorsk`` аватар стоял при
  строке ``error``; ``orichi`` в списке не было вовсе).

Поэтому отбор смотрит в ВК. Гвоздь здесь — последний тест: регион со строкой
``ok`` в журнале и пропавшим аватаром обязан попасть в ремонт, иначе гейт снова
зелёный и снова не про то.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

from modules.promotion.group_setup_vk import SetupResult


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "setup_groups_script_audit",
        os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "setup_groups.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_script()


def _target(code: str, region_id: int):
    return {"code": code, "region_id": region_id, "vk_group_id": -(1000 + region_id)}


def _snap(*, avatar: bool, cover: bool):
    return SetupResult(ok=True, payload={"has_photo": avatar, "has_cover": cover})


_KIND_DEFAULT = object()


async def _select(mod, targets, snapshots):
    """Прогнать отбор с заданными снимками (код региона → SetupResult)."""
    by_gid = {abs(t["vk_group_id"]): snapshots[t["code"]] for t in targets}
    seen: dict = {}

    async def _targets(_codes, kinds=_KIND_DEFAULT):
        seen["kinds"] = kinds
        return targets

    async def _tokens():
        # У каждого сообщества есть community-ключ: снимок не стоит user-бюджета.
        return "user-token", {abs(t["vk_group_id"]): "comm" for t in targets}

    with (
        patch.object(mod, "load_targets", _targets),
        patch.object(mod, "load_tokens", _tokens),
        patch("vk_api.VkApi", MagicMock()),
        patch(
            "modules.promotion.group_setup_vk.get_current",
            side_effect=lambda api, gid: by_gid[abs(gid)],
        ),
    ):
        picked = await mod.select_repair_targets_by_snapshot(None)
    _select.seen = seen
    return [t["code"] for t in picked]


class TestSelectBySnapshot:
    @pytest.mark.asyncio
    async def test_missing_avatar_is_picked(self, mod):
        targets = [_target("uni", 1)]
        assert await _select(mod, targets, {"uni": _snap(avatar=False, cover=True)}) == ["uni"]

    @pytest.mark.asyncio
    async def test_missing_cover_is_picked(self, mod):
        targets = [_target("svecha", 2)]
        assert await _select(mod, targets, {"svecha": _snap(avatar=True, cover=False)}) == [
            "svecha"
        ]

    @pytest.mark.asyncio
    async def test_fully_dressed_is_left_alone(self, mod):
        """«В которых есть — не трогать»: оформленное сообщество в ремонт не идёт."""
        targets = [_target("mi", 3)]
        assert await _select(mod, targets, {"mi": _snap(avatar=True, cover=True)}) == []

    @pytest.mark.asyncio
    async def test_unreadable_snapshot_is_picked_not_skipped(self, mod):
        """«Не смогли посмотреть» — не «всё на месте».

        Тихий пропуск сделал бы аудит одинаково молчаливым на «чисто» и на «не
        измерили»; regions с нечитаемым снимком обязаны дойти до repair_region,
        который вернёт честное «снимок не взялся».
        """
        targets = [_target("zuevka", 4)]
        broken = SetupResult(ok=False, vk_error_code=15, detail="Access denied")
        assert await _select(mod, targets, {"zuevka": broken}) == ["zuevka"]

    @pytest.mark.asyncio
    async def test_journal_status_does_not_decide(self, mod):
        """Гвоздь: строка журнала не участвует в отборе вообще.

        Регион с уцелевшим ``ok`` и пропавшим аватаром при старом отборе по
        ``status='error'`` не попадал в ремонт никогда.
        """
        targets = [_target("nagorsk", 5), _target("orichi", 6)]
        snapshots = {
            "nagorsk": _snap(avatar=True, cover=True),  # журнал говорил error — не трогаем
            "orichi": _snap(avatar=False, cover=True),  # журнала не было — чиним
        }
        assert await _select(mod, targets, snapshots) == ["orichi"]

    @pytest.mark.asyncio
    async def test_oblast_with_missing_avatar_is_picked(self, mod):
        """Гвоздь: область — тоже группа проекта, ремонт её не обходит.

        Найдено 2026-11-20 на живом прогоне: ``--audit`` показывал «Чинить: 5»
        (в списке были ``kirov_obl`` и ``tatarstan_obl``), а ``--repair`` брал
        только ``kind='raion'`` — области в ремонт не попадали никогда. Гейт
        зелёный, работа не сделана.
        """
        targets = [_target("tatarstan_obl", 7), _target("kirov_obl", 8)]
        snapshots = {
            "tatarstan_obl": _snap(avatar=False, cover=False),
            "kirov_obl": _snap(avatar=True, cover=False),
        }
        assert await _select(mod, targets, snapshots) == ["tatarstan_obl", "kirov_obl"]

    @pytest.mark.asyncio
    async def test_repair_asks_for_all_kinds(self, mod):
        """Отбор ремонта обязан просить и районы, и области.

        Без этой строки тест выше проходил бы и на старом коде: подсунуть
        области в подменённый ``load_targets`` можно было и раньше — вопрос в
        том, какие ``kinds`` скрипт реально запрашивает у БД.
        """
        await _select(mod, [_target("uni", 1)], {"uni": _snap(avatar=False, cover=True)})
        assert _select.seen["kinds"] == ("raion", "oblast")
