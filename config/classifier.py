"""Конфиг HITL-классификатора контента (ADR-0003).

Этап B (решение владельца 2026-07-05): классификацию делает облачная рутина
через HTTP-интерфейс (``/api/classifier``), а не Claude API. Ключ рутины —
``CLASSIFIER_INGEST_KEY`` (X-API-Key на ingest-эндпоинтах, как VK-шлюз).

Env vars:
  CLASSIFIER_INGEST_KEY       # секрет для облачной рутины (X-API-Key)
  CLASSIFIER_DISABLED=0       # аварийный kill-switch (1/true/yes/on → выкл.)
  CLASSIFIER_REGION_CODES     # allowlist кодов регионов для shadow (CSV);
                              # пусто = все регионы. Обкатка — один район.
  CLASSIFIER_PENDING_MAX=40   # потолок постов в одном /pending-батче
  CLASSIFIER_SOURCE_DAYS=3    # окно свежести источника (сутки): /pending видит
                              # только посты, собранные за последние N дней
  CLASSIFIER_PREPUBLISH_ENABLED=0     # классифицировать кандидатов ВНУТРИ волны,
                                      # до первой публикации (см.
                                      # modules/classifier/prepublish.py); дефолт выкл
  CLASSIFIER_SELECTION_ENABLED=0      # отбор В сводку по меткам нейросети
                                      # (звено 5, шаг 2): в волне остаются только
                                      # publish-посты, при молчании фильтра —
                                      # политика деградации владельца (см.
                                      # modules/classifier/selection.py); дефолт выкл
  CLASSIFIER_DISTILL_ENABLED=0        # автоматическая чеканка правил еженедельным
                                      # beat-слотом classifier-distill-weekly;
                                      # дефолт выкл — правила чеканит ручная
                                      # дистилляция из чата (памятка /distill),
                                      # две ветки по одному сырью плодят дубли
  CLASSIFIER_RULE_STALE_DAYS=90       # aging: approved-правило старше порога без
                                      # подачи в постулаты → подсветка в панели
  CLASSIFIER_RULES_SNAPSHOT_PATH      # файл снапшота выученных правил (beat);
                                      # дефолт logs/classifier_learned_rules_snapshot.md
  METRICS_FETCH_WORKERS=3     # сколько батчей wall.getById тянуть параллельно
                              # (круг обновления метрик, 1..4; 1 = выключить)
  RATING_VIEWS_ALPHA=0.25     # показатель степени при просмотрах в рейтинге
                              # отбора; 0.5 = нынешняя post_popularity, 0 = охват
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Файл-корректировщик («классификационные постулаты», ADR-0003 §E) — в репо,
# версия = git. Подаётся в промпт классификатора (рутины/API).
POSTULATES_PATH = Path(__file__).resolve().parent / "classification_postulates.md"


def get_ingest_key() -> str:
    """Секрет облачной рутины (env ``CLASSIFIER_INGEST_KEY``). Пусто = ingest выключен."""
    return (os.getenv("CLASSIFIER_INGEST_KEY") or "").strip()


def classifier_disabled() -> bool:
    """Kill-switch классификатора (env ``CLASSIFIER_DISABLED``). Дефолт — включён."""
    return os.getenv("CLASSIFIER_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def get_region_allowlist() -> List[str]:
    """Коды регионов для shadow (env ``CLASSIFIER_REGION_CODES``, CSV).

    Пусто → пустой список → «все регионы» (интерпретируется вызывающим кодом
    как отсутствие фильтра). Обкатка — один район (решение владельца).
    """
    raw = os.getenv("CLASSIFIER_REGION_CODES", "") or ""
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def get_pending_max() -> int:
    """Потолок постов в одном ``/pending``-батче (env ``CLASSIFIER_PENDING_MAX``)."""
    try:
        return max(1, min(200, int(os.getenv("CLASSIFIER_PENDING_MAX", "40"))))
    except ValueError:
        return 40


def get_source_days() -> int:
    """Окно свежести источника ``/pending`` в сутках (env ``CLASSIFIER_SOURCE_DAYS``).

    Классификатор видит только посты, собранные за последние N дней — чтобы в
    ленту не попадало старьё, которое в сводку уже не пойдёт. Считается по
    ``collected_at`` аудита сбора (прокси даты публикации: механически-старые
    посты фильтр отсекает ещё при сборе, поэтому собранное — свежее).
    Границы 1..30.

    **Дефолт 1, а не 3 (решение владельца 2026-08-19: «жить только свежим»).**
    Трое суток окна означали, что после любой остановки движка фоновая таска
    неделями доедала посты, которым в сводку уже нельзя по правилу возраста
    (тогда 72 часа, с 2026-09-22 — сутки, что довод только усиливает):
    после аварии 16-18.08 в окне копилось под 7000 постов при пропускной
    способности 1600 в сутки, и backlog на панели переставал что-либо значить.
    Свежее теперь размечается прямо в волне (``prepublish``), так что широкое
    окно кормит уже только мусорную корзину.
    """
    try:
        return max(1, min(30, int(os.getenv("CLASSIFIER_SOURCE_DAYS", "1"))))
    except ValueError:
        return 1


def get_rule_stale_days() -> int:
    """Порог aging выученного правила в сутках (env ``CLASSIFIER_RULE_STALE_DAYS``).

    approved-правило, не подававшееся в эффективные постулаты дольше порога
    (по ``last_effective_at``), подсвечивается в панели как кандидат на вывод
    (retire, ADR-0005 §Aging / pool #033). Границы 7..365, дефолт 90.
    """
    try:
        return max(7, min(365, int(os.getenv("CLASSIFIER_RULE_STALE_DAYS", "90"))))
    except ValueError:
        return 90


def get_rules_snapshot_path() -> Path:
    """Путь файла-снапшота выученных правил (env ``CLASSIFIER_RULES_SNAPSHOT_PATH``).

    Пишется beat-джобой ``snapshot_learned_rules`` ежедневно. Путь должен быть
    **untracked** (дефолт под ``logs/``): tracked-файл, перезаписанный на проде,
    сломал бы ``git pull`` грязным деревом (PR-only, ADR-0002). Захват снапшота
    в git-историю — шагом dev-сессии (см. docs/ops/hitl-classifier-routine.md).
    """
    raw = (os.getenv("CLASSIFIER_RULES_SNAPSHOT_PATH") or "").strip()
    return Path(raw) if raw else Path("logs") / "classifier_learned_rules_snapshot.md"


def headless_enabled() -> bool:
    """Включена ли headless-классификация на DeepSeek (env ``CLASSIFIER_HEADLESS_ENABLED``).

    **Выключено по умолчанию, и это не осторожность ради осторожности.** Пока
    работает облачная рутина, оба источника пишут в одну таблицу, а
    ``record_verdicts`` идемпотентен по ``lip`` — кто успел, того и вердикт.
    Включать headless можно только ВМЕСТО рутины, иначе прогоны воруют друг у
    друга посты, счёт идёт за оба, а сверка «кто как решил» становится
    невозможной: на одном посте вердикт ровно один.

    Порядок перехода: сверка на общих постах (``scripts/classifier_headless_compare.py``,
    ничего не пишет) → выключить рутину → поднять этот флаг → через неделю
    удалить рутину.
    """
    return os.getenv("CLASSIFIER_HEADLESS_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def distill_enabled() -> bool:
    """Чеканит ли правила автоматическая ветка (env ``CLASSIFIER_DISTILL_ENABLED``).

    **Выключено по умолчанию: правила чеканит ручная дистилляция из чата**
    (памятка ``/distill``), а еженедельный beat-слот ``classifier-distill-weekly``
    молчит, пока флаг не поднят.

    До 2026-09-04 у слота не было своего флага — он гейтился
    ``CLASSIFIER_HEADLESS_ENABLED``. Тот включает КЛАССИФИКАЦИЮ ПОТОКА и на проде
    стоит в ``1``, поэтому «пока дистилляция делается вручную» из докстринга задачи
    не наступало никогда: обе ветки читали одни и те же правки оператора и писали
    в одну панель. Результат — 8 черновиков от 31.08, каждый пересказывал уже
    утверждённое правило. Дедуп по ``norm_key`` их не ловит и не должен: ключ —
    нормализованный ТЕКСТ правила, то есть защита от дословного повтора; учить
    отсев пересказов смысловому сравнению дороже, чем просто не плодить их
    (решение владельца 2026-09-04 — дедуп остаётся алгоритмическим).

    Разводить ветки флагом, а не «договорённостью», обязательно: обе пишут
    ``proposed`` в ``classification_rules``, и разобраться, чей черновик перед
    тобой, оператор может только по полю ``source``.
    """
    return os.getenv("CLASSIFIER_DISTILL_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def get_headless_chunk_size() -> int:
    """Постов в одном вызове модели (env ``CLASSIFIER_HEADLESS_CHUNK``). Границы 1..40."""
    try:
        return max(1, min(40, int(os.getenv("CLASSIFIER_HEADLESS_CHUNK", "10"))))
    except ValueError:
        return 10


def get_metrics_fetch_workers() -> int:
    """Сколько батчей ``wall.getById`` тянуть параллельно (env ``METRICS_FETCH_WORKERS``).

    Границы 1..4. Больше и не нужно: частоту для ВК задаёт не наша сеть, а общий
    per-token тормоз (``enforce_token_rate_limit``, 0.4 с на вызов ≈ 2.5 запроса
    в секунду) — он выравнивает поток запросов при любом числе потоков, а потоки
    убирают простой на ожидании ответа. Замер на проде 22.10: круг обновления
    метрик — **151 батч**, в один поток 265 с, в три потока 87 с, разобранных
    метрик столько же (15 003). ``1`` — выключить (поведение до 22.10).
    """
    try:
        return max(1, min(4, int(os.getenv("METRICS_FETCH_WORKERS", "3"))))
    except ValueError:
        return 3


def read_postulates() -> str:
    """Текст файла-корректировщика (для промпта рутины/API). Нет файла → ''."""
    try:
        return POSTULATES_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


# Показатель степени при просмотрах в рейтинге отбора (звено 5, шаг 1).
# score = engagement / (views + 1) ** alpha
#   alpha = 0.5 — нынешняя post_popularity (рейтинг вовлечённости: маленькая
#                 группа с высоким откликом обгоняет районный хит)
#   alpha = 0.25 — дефолт: охват весомее, но не решает в одиночку
#   alpha = 0   — чистый абсолютный охват
# Наружу вынесен сознательно: это редакционная настройка, её подбирают на
# данных через витрину /api/classifier-review/rating/top, а не в коде.
RATING_VIEWS_ALPHA_DEFAULT = 0.25


# Границы разумного для показателя степени. 0 — чистый охват, 1 — просмотры в
# знаменателе в полную силу (сильнее нынешней 0.5). Всё, что вне, формулу не
# настраивает, а ломает: alpha < 0 переворачивает смысл (охват начинает
# УМНОЖАТЬСЯ и районный хит забивает всё), alpha > 1 схлопывает верхушку в
# посты с двумя просмотрами.
RATING_VIEWS_ALPHA_MIN = 0.0
RATING_VIEWS_ALPHA_MAX = 1.0


def get_rating_views_alpha() -> float:
    """Показатель степени при просмотрах (env ``RATING_VIEWS_ALPHA``).

    Нечитаемое значение — это дефолт, а не исключение: опечатка в env не
    должна ронять отбор посреди волны публикации.

    Тем же дефолтом читается и значение вне диапазона — включая ``nan`` и
    ``inf``, которые ``float()`` принимает молча. ``nan`` особенно тих: все
    сравнения с ним ложны, сортировка витрины перестала бы упорядочивать
    что-либо, и выглядело бы это как «рейтинг почему-то странный», а не как
    ошибка в env.
    """
    raw = (os.getenv("RATING_VIEWS_ALPHA") or "").strip()
    if not raw:
        return RATING_VIEWS_ALPHA_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return RATING_VIEWS_ALPHA_DEFAULT
    # not (min <= value <= max) вместо or-цепочки: так же ловится nan, для
    # которого любое сравнение даёт False.
    if not (RATING_VIEWS_ALPHA_MIN <= value <= RATING_VIEWS_ALPHA_MAX):
        logger.warning(
            "RATING_VIEWS_ALPHA=%r вне диапазона %s..%s — берём дефолт %s",
            raw,
            RATING_VIEWS_ALPHA_MIN,
            RATING_VIEWS_ALPHA_MAX,
            RATING_VIEWS_ALPHA_DEFAULT,
        )
        return RATING_VIEWS_ALPHA_DEFAULT
    return value


# ─────────────────────────── Квоты тем (заказ владельца 2026-08-30) ───────────


def theme_quota_enabled() -> bool:
    """Применять ли потолки долей тем (env ``CLASSIFIER_THEME_QUOTA_ENABLED``).

    Дефолт — выкл: журнал публикаций должен сутки поработать вхолостую, иначе
    первая же волна посчитает доли по пустому знаменателю. Запрет темы
    (``share_percent = 0``) от этого гейта НЕ зависит — он работает всегда, иначе
    выключение квот молча вернуло бы в ленту то, что владелец запретил.
    """
    return os.getenv("CLASSIFIER_THEME_QUOTA_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def get_theme_quota_window_hours() -> int:
    """Окно, за которое считаются доли (env ``CLASSIFIER_THEME_QUOTA_WINDOW_HOURS``).

    Дефолт 24 — решение владельца: форма ленты держится каждый день. Окно
    **скользящее**, а не календарные сутки: календарный день даёт обрыв в
    полночь, и утренняя волна стартовала бы с пустым счётчиком, не ограниченная
    ничем, а вечерние душились бы. Границы 1..720 (месяц).
    """
    try:
        return max(1, min(720, int(os.getenv("CLASSIFIER_THEME_QUOTA_WINDOW_HOURS", "24"))))
    except ValueError:
        return 24


def get_theme_quota_min_posts() -> int:
    """Сколько постов вернуть, если квота вычистила волну целиком.

    Env ``CLASSIFIER_THEME_QUOTA_MIN_POSTS``, дефолт 1: молчащая районная лента
    хуже небольшого перебора по доле. Ноль отключает правило. Границы 0..5.
    """
    try:
        return max(0, min(5, int(os.getenv("CLASSIFIER_THEME_QUOTA_MIN_POSTS", "1"))))
    except ValueError:
        return 1
