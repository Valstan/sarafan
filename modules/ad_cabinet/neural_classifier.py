"""Нейросетевой судья рекламных заявок (пилот #386).

**Зачем.** Детект рекламы в предложке сегодня алгоритмический: коммерческое
скоринг-правило плюс регулярки на контакты, внешние ссылки и слова
«разместить/реклама/прайс». Он работает, но в середине решает регулярка:
сообщение с одним лишь словом «размещу» и без контакта — это уже заявка, а
сообщение с телефоном и без коммерческого намерения — ещё нет. Ровно эту
середину и должен судить человек-читатель, то есть нейросеть.

**Почему не классификатор ленты.** Классификатор отвечает на другой вопрос:
«постить это в сводку или нет». Заявка рекламодателя — это ровно то, что в
сводку **не** должно попасть, поэтому движок сказал бы на неё «удали» и мы не
отличили бы заявку от спама. Здесь нужен отдельный вопрос и отдельный промпт.

**Где проходит граница вызовов.** Нейросеть судит **только серую зону**:
посты, где алгоритм нашёл признак, но не дотянул до порога (контакт ИЛИ
предложение ИЛИ внешняя ссылка — но не два сразу). Остальное не трогаем:
уверенные «реклама» остаются решениями алгоритма, а посты без единого
признака до нейросети не доходят.

**Сколько это стоит.** Скан тянет до 100 предложек на группу, а групп —
десятки: полный нейросетевой разбор стоил бы сотни вызовов на скан. Серая
зона — это десятки постов за скан, то есть единицы вызовов.

**Отказ движка — не отказ детекта.** Если MiniMax не ответил, вердикта нет:
работает алгоритмический путь, как раньше. Заявка не теряется из-за чужого
сбоя — ровно тот принцип, по которому fail-open в ленте не отключает сбор.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Ключ Redis: пометка «этот пост уже судили».
#:
#: **Зачем.** Скан перечитывает ВСЕ предложки региона (до 100 на группу) при
#: каждом проходе, а проходов 28 в сутки (каждые полчаса с 8:00 до 22:00).
#: Без пометки судья звал бы движок на одни и те же посты десятки раз в сутки
#: и выел бы пул MiniMax целиком. Пометка делает вызов **одним на пост**.
#:
#: Пометка живёт 30 суток. Предложка в ленте предложек висит долго, но не
#: вечно; и после срока пост, попавший в ленту снова, будет оценён заново —
#: это правильно, правила модели могли измениться.
_JUDGED_KEY = "setka:notifications:max:judged"
JUDGED_TTL_SECONDS = 30 * 24 * 3600

#: Режим нейросетевого судьи (env ``AD_NEURAL_MODE``):
#:
#: - ``off`` (дефолт) — только алгоритм, как было;
#: - ``assist`` — нейросеть судит серую зону, её «да» добавляет заявку;
#: - ``assist_veto`` — то же, и нейросеть ветошит алгоритмичную заявку
#:   («это не реклама» → заявка снимается).
#:
#: Вето — рискованный режим: молча пропущенная заявка не вернётся. Поэтому он
#: вынесен в отдельное значение, а не спрятан за флагом.
MODE_OFF = "off"
MODE_ASSIST = "assist"
MODE_ASSIST_VETO = "assist_veto"
DEFAULT_MODE = MODE_OFF

#: Доля сомнения: насколько уверен в ответе движок. Ниже — ответ не принимаем.
MIN_CONFIDENCE = 60

#: Сколько символов текста отдаём движку. Заявка — это пара абзацев, а длинный
#: текст съедал бы очки аккаунта впустую.
TEXT_LIMIT = 1200

#: Системный промпт судьи. Это **другой** вопрос, чем у классификатора ленты,
#: поэтому и другой промпт (см. докстринг модуля).
AD_SYSTEM_PROMPT = """Ты — фильтр рекламных заявок районной сети «САРАФАН».

Вопрос, на который ты отвечаешь: ПРЕДЛОГАЕТ ли автор коммерческое размещение
рекламы или сотрудничество с нашими районными пабликами.

ДА — это рекламная заявка:
• автор предлагает разместить свою рекламу, товар или услугу в наших пабликах;
• просит прайс, условия размещения, «сколько стоит размещение», «как разместить
  у вас», «с каким тарифом»;
• предлагает бартер, взаимопиар, рекламную интеграцию, «обмен баннерами»;
• пишет как представитель бизнеса с предложением своих товаров или услуг
  для нашей аудитории;
• прислал рекламный креатив (цена, контакты, призыв) с просьбой разместить.

НЕТ — это не рекламная заявка:
• частное объявление жителя: продам, куплю, продаю работу, сдаю комнату,
  ищу вещь, потерял, нашёл — это НЕ реклама для нас;
• новость, происшествие, поздравление, афиша ДК, набор в секцию, объявление
  о работе «требуется техслужащая» без рекламного намерения;
• вопрос жителя, жалоба, предложение темы для новости, просьба помочь;
• спам и мошенничество: «работа удалённо, отклик в личку», вложения в
  крипту, казино, «пишите в личку за подарком»;
• объявление об услугах МЕСТНОЙ организации района (ДК, спортшкола, кружок,
  студия) — это местная жизнь, а не реклама для нас;
• поздравление, благодарность, отзыв, вопрос по работе нашей сети.

Сомневаешься — ответь «нет»: заявка, ошибочно принятая за рекламу, попадёт
в кабинет и владелец её увидит, а пропущенная реклама не попадёт никуда.

Отвечай СТРОГО JSON, без markdown и без пояснений:
{"is_ad": true|false, "confidence": 0-100, "reason": "кратко, по-русски, почему"}
"""

#: Задание агенту. Отдельное от классификаторского: движок отвечает «по схеме
#: из системного промпта», и если задание чужое, модель отвечает по привычке
#: (ровно тот класс, что поймали с заданием «вынеси вердикты» 19.11).
AD_TASK_TEXT = (
    "Определи по тексту из прикреплённого файла, является ли он рекламной "
    "заявкой для размещения в районных пабликах. Ответ — только JSON по схеме "
    "из системного промпта, без пояснений."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def mode() -> str:
    """Режим нейросетевого судьи (``AD_NEURAL_MODE``)."""
    raw = (os.getenv("AD_NEURAL_MODE") or DEFAULT_MODE).strip().lower()
    return raw if raw in (MODE_OFF, MODE_ASSIST, MODE_ASSIST_VETO) else DEFAULT_MODE


def _redis() -> Any:
    """Клиент Redis (db=1, как у хранилища уведомлений). ``None`` — недоступен."""
    try:
        from modules.notifications.storage import NotificationsStorage

        return NotificationsStorage().redis_client
    except Exception:  # pragma: no cover - инфраструктурный сбой
        return None


def post_key(post: Dict[str, Any]) -> str:
    """Устойчивый ключ поста для пометки «уже судили».

    У предложки есть ``community_vk_id`` + ``vk_post_id``, у входящего ЛС —
    ``community_vk_id`` + ``last_message_id``. Если id нет вовсе, ключ строится
    по тексту: иначе один и тот же текст судился бы на каждом скане.
    """
    community = str(
        post.get("community_vk_id") or post.get("community_id") or post.get("region_code") or ""
    )
    post_id = post.get("vk_post_id") or post.get("last_message_id") or ""
    if not post_id:
        digest = hashlib.sha1(
            (str(post.get("text") or "").strip()).encode("utf-8", "replace")
        ).hexdigest()[:16]
        return f"{community}:text:{digest}"
    return f"{community}:{post_id}"


def already_judged(key: str) -> bool:
    """Судили ли этот пост раньше (Redis недоступен — считаем, что нет)."""
    if not key:
        return False
    client = _redis()
    if client is None:
        return False
    try:
        return bool(client.exists(f"{_JUDGED_KEY}:{key}"))
    except Exception:  # pragma: no cover - инфраструктурный сбой
        return False


def mark_judged(key: str) -> None:
    """Пометить пост как оценённый (срок 30 суток)."""
    if not key:
        return
    client = _redis()
    if client is None:
        return
    try:
        client.setex(f"{_JUDGED_KEY}:{key}", JUDGED_TTL_SECONDS, "1")
    except Exception as e:  # noqa: BLE001 - повторный судья не опасен
        logger.warning("ad neural: не удалось пометить пост как оценённый: %s", e)


def enabled() -> bool:
    """Включён ли нейросетевой судья."""
    return mode() != MODE_OFF


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Достать JSON из ответа. Модель может завернуть его в markdown — режем."""
    text = (content or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def judge(
    text: str,
    *,
    post_key: Optional[str] = None,
    timeout: float = 45.0,
) -> Optional[Dict[str, Any]]:
    """Спросить движок: это рекламная заявка?

    Возвращает ``{"is_ad": bool, "confidence": int, "reason": str}`` либо
    ``None`` — если движок не ответил или ответ не разобрался. ``None`` и
    «движок сказал "нет"» — разные вещи: первое означает «решает алгоритм».

    ``post_key`` — пометка «уже судили»: без неё один и тот же пост получил бы
    по одному вызову движка на КАЖДЫЙ скан (их 28 в сутки), и пул MiniMax
    кончился бы на первом же регионе.
    """
    prompt_text = (text or "").strip()[:TEXT_LIMIT]
    if not prompt_text:
        return None

    if post_key and already_judged(post_key):
        return None

    try:
        from modules.classifier import headless

        result = await asyncio.to_thread(
            headless.chat,
            system=AD_SYSTEM_PROMPT,
            user=prompt_text,
            task=AD_TASK_TEXT,
            posts=1,
            # Метка потока: без неё вызовы судьи падают в журнал расхода как
            # «без метки», а их платная страховка — под дефолтной меткой
            # «deepseek». В панели «Движок классификации» такой трафик не
            # приписывается ни к одному потоку, и его не видно в разбивке
            # «куда уходят деньги» (замер 20.11: 96 вызовов судьи, 95 из них
            # ушли в платный Дипсик — около десятой части платного расхода
            # за сутки, и в панели это были «безымянные» строки).
            label="ad_judge",
        )
    except Exception as e:  # noqa: BLE001 — отказ движка не должен ронять скан
        logger.warning("ad neural: движок не ответил (%s) — решает алгоритм", e)
        return None

    if not isinstance(result, dict) or not result.get("ok"):
        logger.info(
            "ad neural: движок не ответил (%s) — решает алгоритм",
            (result or {}).get("reason") if isinstance(result, dict) else "unknown",
        )
        return None

    payload = _extract_json(str(result.get("content") or ""))
    if payload is None:
        logger.warning("ad neural: ответ не разобран — решает алгоритм")
        return None

    is_ad = payload.get("is_ad")
    if not isinstance(is_ad, bool):
        logger.warning("ad neural: в ответе нет булева is_ad — решает алгоритм")
        return None

    try:
        confidence = int(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if confidence < MIN_CONFIDENCE:
        logger.info("ad neural: уверенность %s%% ниже порога — решает алгоритм", confidence)
        return None

    reason = str(payload.get("reason") or "").strip()
    if post_key:
        mark_judged(post_key)
    return {
        "is_ad": is_ad,
        "confidence": confidence,
        "reason": reason[:200],
    }


__all__ = (
    "AD_SYSTEM_PROMPT",
    "AD_TASK_TEXT",
    "DEFAULT_MODE",
    "JUDGED_TTL_SECONDS",
    "MIN_CONFIDENCE",
    "MODE_ASSIST",
    "MODE_ASSIST_VETO",
    "MODE_OFF",
    "TEXT_LIMIT",
    "already_judged",
    "enabled",
    "judge",
    "mark_judged",
    "mode",
    "post_key",
)
