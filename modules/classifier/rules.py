"""Петля обучения классификатора: выученные правила overlay (ADR-0005).

Коррекции оператора (лента ``/classifier``) → дистилляция (облачная рутина
чеканит черновики) → утверждение оператором в вебе → ``approved`` правила
подмешиваются в эффективные постулаты, которые рутина читает каждый прогон.
Нейросеть правила сама не применяет — только предлагает; человек в петле.

Операции:
- ``render_effective_postulates`` — база (git-файл) + утверждённые правила (для рутины);
- ``fetch_corrections_for_distill`` — коррекции оператора для рутины-дистиллятора;
- ``record_rule_proposals`` — записать черновики (дедуп против активных);
- ``list_rules`` / ``decide_rule`` / ``add_operator_rule`` — операторские операции.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select

from config.classifier import (
    COMPACT_POSTULATES_PATH,
    LOCAL_RULEBOOK_COMPACT,
    get_local_rulebook_mode,
    get_rule_stale_days,
    read_compact_postulates,
    read_postulates,
)
from database.models_extended import (
    ClassificationCorrection,
    ClassificationRule,
    ContentClassification,
)
from modules.classifier.schema import RuleProposal

logger = logging.getLogger(__name__)

RULE_STATUSES = ("proposed", "approved", "rejected", "retired")
_ACTIVE_STATUSES = ("proposed", "approved")
_MAX_RULE_LEN = 600
_LEARNED_HEADER = "## Выученные правила (утверждены оператором, дистилляция коррекций)"


def _norm(text: str) -> str:
    """Нормализовать текст правила для дедупа (схлопнуть пробелы, регистр, обрезать)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:200]


async def render_effective_postulates(session) -> str:
    """Эффективные постулаты: базовый git-файл + утверждённые выученные правила.

    Это то, что отдаётся рутине в ``/postulates`` и идёт в промпт классификации.
    Пусто выученных → возвращаем только базу (байт-в-байт, prompt-cache цел).
    Поданным правилам штампуется ``last_effective_at`` (aging, ADR-0005): правило,
    давно не попадавшее в постулаты, панель подсветит кандидатом на вывод."""
    base = read_postulates()
    # Канонический словарь тем (миграция 069): рутина выбирает theme СТРОГО из него.
    from database.models_extended import ClassifierTheme

    themes = (
        (await session.execute(select(ClassifierTheme).order_by(ClassifierTheme.position)))
        .scalars()
        .all()
    )
    if themes:
        theme_names = ", ".join(t.name for t in themes)
        # Описание темы (миграция 090) — граница, а не синоним имени. Пока
        # объяснять границу было негде, движок сочинял темы мимо словаря: в
        # Корпусе завелись «туризм», «рекреация» и даже «Клуб Малмыжских
        # Путешественников» — название сообщества, принятое за тему. Строка без
        # описания выглядит ровно как раньше, поэтому тема без него ничего не ломает.
        described = [t for t in themes if (t.description or "").strip()]
        detail = ""
        if described:
            lines = "\n".join(f"- **{t.name}** — {t.description.strip()}" for t in described)
            detail = f"\n\n{lines}\n"
        base = base.rstrip() + (
            "\n\n## Разрешённые темы (theme)\n\n"
            f"Выбирай тему СТРОГО из списка: {theme_names}. "
            "Никаких новых тем, англицизмов и вариаций написания — если ни одна не "
            "подходит идеально, бери ближайшую по смыслу; совсем мусор/пустое — «мусор»."
            f"{detail}\n"
        )
    rules = (
        (
            await session.execute(
                select(ClassificationRule).where(ClassificationRule.status == "approved")
                # Вторичный ключ обязателен. ``created_at`` — питоновский
                # ``default=datetime.utcnow`` (не server_default), поэтому пачка
                # правил, утверждённая дистилляцией одним заходом, может лечь с
                # одинаковой меткой, и порядок при ничьей не определён. Правила
                # нумеруются в промпте (1./2./3.), значит перенумерация между
                # прогонами меняет промпт, не меняя содержания — спящий дефект
                # того же семейства, что LIMIT без ORDER BY.
                # Соседняя ``render_learned_snapshot`` делает так с самого начала.
                .order_by(ClassificationRule.created_at, ClassificationRule.id)
            )
        )
        .scalars()
        .all()
    )
    if not rules:
        return base
    lines = ["", _LEARNED_HEADER, ""]
    for i, r in enumerate(rules, 1):
        scope = "" if not r.region_code else f" _(район {r.region_code})_"
        lines.append(f"{i}. {(r.rule_text or '').strip()}{scope}")
    rendered = base.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    now = datetime.utcnow()
    for r in rules:
        r.last_effective_at = now
    await session.commit()
    return rendered


async def resolve_local_postulates(session) -> str:
    """Правильник для локального контура — по флагу ``LOCAL_ENGINE_RULEBOOK``.

    ``effective`` (дефолт) — боевой путь: база из git + живые выученные правила
    из БД. ``compact`` — сжатый файл целиком (ADR-0016): нужен локальной модели,
    у которой окно контекста режется, и промпт должен быть коротким.

    Файл недоступен → откат на боевой путь, а не пустой промпт: модель без
    правил отвечает «мусор» на всё подряд, и это хуже, чем длинный промпт.
    """
    if get_local_rulebook_mode() == LOCAL_RULEBOOK_COMPACT:
        compact = read_compact_postulates()
        if compact.strip():
            return compact
        logger.warning(
            "classifier: сжатый правилник недоступен (%s) — беру действующий",
            COMPACT_POSTULATES_PATH,
        )
    return await render_effective_postulates(session)


async def render_learned_snapshot(session) -> str:
    """Детерминированный снапшот approved-правил для файла-аудита (ADR-0005, хвост Б).

    Без timestamp генерации — байты меняются только при реальном изменении слоя,
    поэтому git-дифф после захвата в репо содержательный. Источник истины — БД;
    файл пишет beat ``snapshot_learned_rules`` (untracked-путь на проде), захват
    в git-историю — шагом dev-сессии (PR-only, ADR-0002)."""
    rules = (
        (
            await session.execute(
                select(ClassificationRule)
                .where(ClassificationRule.status == "approved")
                .order_by(ClassificationRule.created_at, ClassificationRule.id)
            )
        )
        .scalars()
        .all()
    )
    lines = ["# Снапшот выученных правил классификатора (ADR-0005)", ""]
    if not rules:
        lines.append("_Пусто — утверждённых правил нет._")
        return "\n".join(lines) + "\n"
    for i, r in enumerate(rules, 1):
        scope = "" if not r.region_code else f" _(район {r.region_code})_"
        decided = r.decided_at.date().isoformat() if r.decided_at else "?"
        lines.append(
            f"{i}. [id={r.id} {r.source}, утв. {decided}] {(r.rule_text or '').strip()}{scope}"
        )
    return "\n".join(lines) + "\n"


async def fetch_corrections_for_distill(
    session, *, limit: int = 100, days: int = 30
) -> List[Dict[str, Any]]:
    """Коррекции оператора (outcome=correct) + снапшот поста — сырьё для дистилляции.

    Только несогласия (``correct``): согласия правило не рождают. Свежие первыми,
    окно ``days``. Рутина смотрит на них + текущие эффективные правила и предлагает
    ОБОБЩЁННЫЕ правила для повторяющихся паттернов (не под единичный случай)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(ClassificationCorrection, ContentClassification)
        .join(
            ContentClassification,
            ClassificationCorrection.classification_id == ContentClassification.id,
        )
        .where(ClassificationCorrection.outcome == "correct")
        .where(ClassificationCorrection.created_at >= cutoff)
        .order_by(ClassificationCorrection.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    out: List[Dict[str, Any]] = []
    for corr, cls in rows:
        out.append(
            {
                "lip": corr.lip,
                "verdict_type": corr.verdict_type,
                "ai_value": corr.ai_value,
                "operator_value": corr.operator_value,
                "post_text": (cls.post_text or "").strip()[:800],
                "post_url": cls.post_url,
                "region_code": cls.region_code,
                "created_at": corr.created_at.isoformat() if corr.created_at else None,
            }
        )
    return out


async def record_rule_proposals(
    session, proposals: Sequence[RuleProposal], *, source: str = "routine"
) -> Dict[str, int]:
    """Записать черновики правил (status=proposed). Дедуп против активных (proposed|approved).

    Идемпотентно по ``norm_key``: правило, уже висящее в предложениях или утверждённое,
    повторно не заводим. Отклонённое/выведенное — можно предложить снова (паттерн вернулся)."""
    if not proposals:
        return {"recorded": 0, "skipped_existing": 0, "skipped_invalid": 0}

    active = {
        k
        for (k,) in (
            await session.execute(
                select(ClassificationRule.norm_key).where(
                    ClassificationRule.status.in_(list(_ACTIVE_STATUSES))
                )
            )
        ).all()
        if k
    }
    seen = set(active)
    recorded = skipped_existing = skipped_invalid = 0
    for p in proposals:
        text = (p.rule_text or "").strip()
        if not text or len(text) > _MAX_RULE_LEN:
            skipped_invalid += 1
            continue
        nk = _norm(text)
        if nk in seen:
            skipped_existing += 1
            continue
        seen.add(nk)
        session.add(
            ClassificationRule(
                region_code=(p.region_code or None),
                rule_text=text,
                status="proposed",
                source=source,
                rationale=(p.rationale or None),
                evidence=(list(p.evidence) if p.evidence else None),
                model=(p.model or None),
                norm_key=nk,
            )
        )
        recorded += 1
    await session.commit()
    logger.info(
        "classifier rules: proposed recorded=%s skipped_existing=%s skipped_invalid=%s",
        recorded,
        skipped_existing,
        skipped_invalid,
    )
    return {
        "recorded": recorded,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }


async def list_rules(
    session, *, status: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    """Правила для операторской панели (свежие первыми). ``status`` пуст → все.

    ``stale`` — aging-подсветка (ADR-0005 §Aging, #033): approved-правило, не
    подававшееся в эффективные постулаты дольше ``CLASSIFIER_RULE_STALE_DAYS``
    (свежеутверждённое без штампа меряем по ``decided_at``) — кандидат на retire."""
    stmt = select(ClassificationRule).order_by(ClassificationRule.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(ClassificationRule.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    cutoff = datetime.utcnow() - timedelta(days=get_rule_stale_days())
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = r.to_dict()
        ref = r.last_effective_at or r.decided_at or r.created_at
        d["stale"] = bool(r.status == "approved" and ref and ref < cutoff)
        out.append(d)
    return out


async def decide_rule(
    session, rule_id: int, *, status: str, edited_text: Optional[str] = None
) -> Dict[str, Any]:
    """Решение оператора: approve | reject | retire (+ опц. правка текста при approve)."""
    if status not in RULE_STATUSES:
        return {"ok": False, "error": f"bad status: {status}"}
    r = await session.get(ClassificationRule, rule_id)
    if r is None:
        return {"ok": False, "error": "rule not found"}
    if edited_text is not None and edited_text.strip():
        r.rule_text = edited_text.strip()[:_MAX_RULE_LEN]
        r.norm_key = _norm(r.rule_text)
    r.status = status
    r.decided_at = datetime.utcnow()
    await session.commit()
    return {"ok": True, "id": r.id, "status": status}


async def add_operator_rule(
    session, rule_text: str, *, region_code: Optional[str] = None
) -> Dict[str, Any]:
    """Оператор пишет правило руками → сразу approved (в деле со следующего прогона)."""
    text = (rule_text or "").strip()
    if not text:
        return {"ok": False, "error": "empty rule_text"}
    r = ClassificationRule(
        region_code=region_code or None,
        rule_text=text[:_MAX_RULE_LEN],
        status="approved",
        source="operator",
        norm_key=_norm(text),
        decided_at=datetime.utcnow(),
    )
    session.add(r)
    await session.commit()
    return {"ok": True, "id": r.id, "status": "approved"}
