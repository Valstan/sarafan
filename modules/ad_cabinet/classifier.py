"""Классификатор рекламы для рекламного кабинета (предложка).

Переиспользует чистый алгоритм ``AdvertisementFilter`` из пайплайна сводок
(``modules/filters/ads_filter.py``) и добавляет предложка-специфичные сигналы.

Фильтр сводки отвечает на вопрос «надо ли выкинуть пост из сводки»; здесь
вопрос обратный — «это коммерческая заявка, на которую нужно ответить оффером».
Поэтому ``passed=False`` фильтра == «это реклама» для нас (R9).

⚠️ Контекст передаём **без** ``theme='reklama'`` — иначе ``AdvertisementFilter``
закоротит детект на ``passed=True`` (R10).

Интерфейс намеренно минимальный — ``classify()`` с тем же контрактом потом
заменится обученной моделью (TF-IDF / маленький трансформер, фаза 4) без правок
вызывающего кода.

**Нейросетевой судья (19.11, пилот #386).** Алгоритмическая часть осталась как
была, но в «серую зону» (признак есть, порога не хватает) теперь вмешивается
MiniMax — он и решает, заявка это или обычное объявление. Движок не ответил —
решает алгоритм, заявка не теряется. Подробности и граница вызовов —
``modules/ad_cabinet/neural_classifier.py``.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from modules.filters.ads_filter import AdvertisementFilter
from modules.ad_cabinet import neural_classifier as neural

# Контакты — главный признак «человек хочет разместиться/продать».
_CONTACT_PATTERNS = [
    (r"\bt\.me/\w+", "ссылка на Telegram"),
    (r"\bwa\.me/\d+", "ссылка на WhatsApp"),
    (r"\bwhats\s?app\b", "WhatsApp"),
    (r"\bviber\b", "Viber"),
    (r"@[a-zA-Z][a-zA-Z0-9_.]{3,}", "@-хэндл"),
    (
        r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}",
        "номер телефона",
    ),
]

# Внешняя ссылка (не на vk.com) — частый признак рекламы стороннего ресурса.
_EXTERNAL_LINK = re.compile(r"https?://(?!(?:www\.)?vk\.com)", re.IGNORECASE)

# Слова-маркеры предложений о размещении/сотрудничестве.
_OFFER_WORDS = [
    ("размещу", "размещу"),
    ("размещен", "размещение рекламы"),
    ("размести", "размещение"),
    ("реклам", "реклама"),
    ("сотруднич", "сотрудничество"),
    ("прайс", "прайс"),
    ("расценк", "расценки"),
    ("бартер", "бартер"),
    ("взаимопиар", "взаимопиар"),
    ("коммерческ", "коммерческое предложение"),
    ("предлагаю услуг", "предложение услуг"),
]

# Порог ниже, чем у сводки (там SCORE_THRESHOLD=4): в предложке коммерческое
# намерение явнее, и цена ложного срабатывания мала (оператор подтверждает).
SCORE_THRESHOLD = 3


async def classify(post: dict, context: Optional[dict] = None) -> Tuple[bool, int, List[str]]:
    """Классифицировать предложенный пост.

    Args:
        post: dict поста VK (``text``, ``marked_as_ads``, ``attachments`` …).
        context: контекст фильтра (``theme`` будет удалён принудительно).

    Returns:
        ``(is_ad, score, reasons)`` — флаг рекламы, итоговый скор, причины.
    """
    ctx = dict(context or {})
    ctx.pop("theme", None)  # R10: не давать фильтру закоротиться на reklama-теме.

    text = post.get("text") or ""
    text_lower = text.lower()
    reasons: List[str] = []

    # 1. Базовый алгоритм сводки (инверсия: rejected = реклама).
    flt = AdvertisementFilter(name="ad_cabinet_filter")
    res = await flt.apply(post, ctx)
    score = int((res.metadata or {}).get("score", 0))
    base_is_ad = not res.passed
    if base_is_ad:
        reasons.append(res.reason or "детектор рекламы")

    # 2. Предложка-специфичные сигналы (накапливают score).
    for pattern, label in _CONTACT_PATTERNS:
        if re.search(pattern, text):
            score += 2
            reasons.append(f"контакт: {label}")
            break  # одного контакт-сигнала достаточно
    if _EXTERNAL_LINK.search(text):
        score += 1
        reasons.append("внешняя ссылка")
    for stem, label in _OFFER_WORDS:
        if stem in text_lower:
            score += 2
            reasons.append(f"слово «{label}»")
            break

    is_ad = base_is_ad or score >= SCORE_THRESHOLD

    # ── Нейросетевой судья (пилот #386) ────────────────────────────────────
    # Вмешивается только в «серую зону»: алгоритм нашёл признак, но не
    # дотянул до порога (контакт ИЛИ предложение ИЛИ внешняя ссылка — но не
    # два сразу). Именно эту середину регулярка решает хуже всего.
    #
    # Почему не на всех постах: скан тянет до 100 предложек на группу, а
    # групп десятки — полный разбор стоил бы сотни вызовов движка на скан.
    # Почему не классификатором ленты: там другой вопрос («постить в сводку
    # или нет»), и заявка рекламодателя получила бы «удали» — как и спам.
    neural_verdict = None
    if neural.enabled():
        grey_zone = (not base_is_ad) and 0 < score < SCORE_THRESHOLD
        veto_zone = base_is_ad and neural.mode() == neural.MODE_ASSIST_VETO
        if grey_zone or veto_zone:
            neural_verdict = await neural.judge(text)

    if neural_verdict:
        reason = neural_verdict.get("reason") or "рекламная заявка"
        if neural_verdict["is_ad"]:
            if not is_ad:
                score = max(score, SCORE_THRESHOLD)
                reasons.append(f"нейросеть: {reason}")
            is_ad = True
        elif base_is_ad and neural.mode() == neural.MODE_ASSIST_VETO:
            is_ad = False
            reasons.append(f"нейросеть сняла: {reason}")

    # Если пометили рекламой только по накопленному score (базовый фильтр
    # сводки ПРОПУСТИЛ пост — его порог 4 выше нашего 3 — и предложка-сигналы
    # не сработали), причин в карточке не было бы. Прокидываем score базового
    # фильтра как причину, чтобы оператор видел «почему». (PENDING: пустые
    # reasons_json при score из унаследованного AdvertisementFilter.)
    if is_ad and not reasons:
        reasons.append(f"коммерческие признаки (score {score})")

    # Уникализируем причины, сохраняя порядок.
    seen: set = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return is_ad, score, reasons
