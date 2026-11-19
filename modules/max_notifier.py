"""Карточка рекламной заявки в MAX — то, что видит владелец и на что жмёт.

**Зачем.** Заявка (реклама, пойманная в предложке сообщества или во входящем
ЛС) сегодня приходит в Telegram **счётчиком**: «новых заявок в предложке: 3» и
ссылкой на кабинет. Дальше — открыть кабинет, найти заявку, отрендерить ответ,
отправить. Это четыре шага и минута на каждого рекламодателя.

MAX даёт формат, где шаг остаётся один: **карточка в чат «САРАФАН» с фото,
текстом, ссылкой на исходный пост и кнопкой ответа.** Нажатие — это callback,
на который отвечает бот: он берёт подготовленный текст и отправляет автору.

**Что в карточке и почему:**

- **Фото** поста (``photo_urls_json`` заявки) — рекламодатель продаёт картинку,
  и без неё владелец не понимает, о чём речь;
- **Текст** — снимок поста из заявки, обрезанный: в MAX длинное сообщение
  выглядит как простыня, а суть заявки — в первых строках;
- **Ссылка на исходный пост** — чтобы открыть и посмотреть целиком, а не
  верить пересказу;
- **Кнопка «Наши услуги по рекламе»** — ссылка на нашу страницу услуг: ответ
  должен содержать её, и кнопка отвечает на вопрос «а что вы вообще
  предлагаете» без лишнего текста;
- **Кнопка «Ответить автору»** — callback с id заявки: дальше срабатывает
  отправка ответа автору (см. ``web/api/max_webhook.py``).

**Прямая правка бота в проде.** В MAX есть «канал в MAX» и чат сообщества,
администратором которого становится бот, — но автор заявки живёт в VK, и его
MAX-идентификатора у нас нет. Поэтому «от имени сообщества» означает
**сообщество в VK** (оно ответит само, если автор разрешил), а личный
аккаунт владельца — запасной путь, и то он упирается в ограничение VK:
у user-токена нет scope ``messages``, поэтому личный аккаунт может только
открыть диалог, но не написать в него сам (см. P091, ``modules/notifications/
vk_actions.py``). Это ограничение VK, а не MAX, и обойти его нельзя — зато
можно не обещать того, что не работает.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from modules import max_http

logger = logging.getLogger(__name__)

#: Наша страница услуг по рекламе. Её владелец просил вставить в ответ
#: рекламодателю — и кнопкой, и текстом шаблона.
SERVICES_URL = "https://сарафан.вмалмыже.рф/regions/links"

#: Стандартный текст ответа. Короткий: что делаем и куда смотреть прайс.
#: Владелец просил «не очень много текста, но объяснить какие у нас услуги по
#: размещению рекламы и ссылку на страницу услуг».
DEFAULT_REPLY_TEMPLATE = (
    "Здравствуйте! Спасибо за обращение в {community_name}.\n"
    "Мы размещаем рекламу в районных пабликах «САРАФАН» и на страницах "
    "сервисов Малмыжа. Есть форматы «предложка» и «рекламная подборка» — "
    "подберём под ваш бюджет.\n"
    "Услуги и прайс: {services_url}"
)

#: Сколько символов текста поста показываем в карточке. Длинный текст в MAX
#: выглядит как простыня, а суть заявки — в первых строках.
SNIPPET_LIMIT = 400

#: Префикс payload'а кнопки ответа. Он же — маркер «это наше действие», а не
#: чужой callback.
CALLBACK_PREFIX = "ad:"


def services_url() -> str:
    """Страница услуг по рекламе (env- overridable — домен может смениться)."""
    return (os.getenv("AD_SERVICES_URL") or SERVICES_URL).strip()


def callback_payload(ad_request_id: int) -> str:
    """Payload кнопки ответа для заявки."""
    return f"{CALLBACK_PREFIX}{int(ad_request_id)}"


def ad_request_id_from_payload(payload: str) -> Optional[int]:
    """Разобрать id заявки из payload'а кнопки (``None``, если не наш)."""
    text = (payload or "").strip()
    if not text.startswith(CALLBACK_PREFIX):
        return None
    raw = text[len(CALLBACK_PREFIX) :].strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _snippet(text: Optional[str], limit: int = SNIPPET_LIMIT) -> str:
    """Обрезать текст поста до читаемого куска."""
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def post_url(community_vk_id: Any, vk_post_id: Any) -> Optional[str]:
    """Ссылка на исходный пост ВК (``None``, если поста нет — входящее ЛС)."""
    if not community_vk_id or not vk_post_id:
        return None
    try:
        return f"https://vk.com/wall{abs(int(community_vk_id))}_{int(vk_post_id)}"
    except (TypeError, ValueError):
        return None


def _first_photo(photo_urls: Any) -> Optional[str]:
    """Прямая CDN-ссылка первой картинки поста."""
    if isinstance(photo_urls, str):
        photo_urls = [photo_urls]
    if not isinstance(photo_urls, (list, tuple)):
        return None
    for item in photo_urls:
        if isinstance(item, str) and item.strip().startswith(("http://", "https://")):
            return item.strip()
    return None


def build_card(
    *,
    ad_request_id: int,
    community_name: Optional[str],
    author_name: Optional[str],
    text_snapshot: Optional[str],
    photo_url: Optional[str] = None,
    source_url: Optional[str] = None,
    origin_label: str = "предложке",
) -> Dict[str, Any]:
    """Собрать карточку заявки: текст + вложения (фото и клавиатура).

    Возвращает ``{"text": ..., "attachments": [...]}`` — ровно то, что ждёт
    ``POST /messages`` в MAX.
    """
    who = (author_name or "").strip() or "Автор"
    where = (community_name or "").strip() or "сообщество"
    lines = [f"📢 Рекламная заявка ({origin_label})", f"👤 {who} — {where}"]
    snippet = _snippet(text_snapshot)
    if snippet:
        lines.append("")
        lines.append(snippet)
    if source_url:
        lines.append("")
        lines.append(f"🔗 Открыть: {source_url}")
    text = "\n".join(lines)

    attachments: List[Dict[str, Any]] = []
    if photo_url:
        attachments.append({"type": "image", "payload": {"url": photo_url}})
    attachments.append(
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "link",
                            "text": "Наши услуги по рекламе",
                            "url": services_url(),
                        }
                    ],
                    [
                        {
                            "type": "callback",
                            "text": "Ответить автору",
                            "payload": callback_payload(ad_request_id),
                        }
                    ],
                ]
            },
        }
    )
    return {"text": text, "attachments": attachments}


def card_for_request(request: Any, *, cabinet_url: str = "") -> Dict[str, Any]:
    """Карточка по строке заявки (``AdRequest``).

    ``cabinet_url`` — ссылка на кабинет заявок; в неё уводим, когда исходного
    поста нет (входящее ЛС: показать нечего, только открыть переписку).
    """
    origin = getattr(request, "origin", "suggested")
    origin_label = "предложке" if origin == "suggested" else "входящему ЛС"
    link = (
        post_url(getattr(request, "community_vk_id", None), getattr(request, "vk_post_id", None))
        or cabinet_url
        or None
    )
    photo = _first_photo(getattr(request, "photo_urls_json", None))
    return build_card(
        ad_request_id=int(request.id),
        community_name=getattr(request, "community_name", None),
        author_name=getattr(request, "author_name", None),
        text_snapshot=getattr(request, "text_snapshot", None),
        photo_url=photo,
        source_url=link,
        origin_label=origin_label,
    )


def send_card(card: Dict[str, Any], *, chat_id: Any = None) -> Dict[str, Any]:
    """Отправить карточку в чат MAX.

    Пустой ``chat_id``/токен — канал не настроен: возвращаем ``{"ok": False}``
    без исключения, чтобы вызывающий мог спокойно продолжить (уведомление —
    не повод уронить скан заявок).
    """
    target = chat_id if chat_id is not None else (os.getenv("MAX_CHAT_ID") or "").strip()
    if not target:
        logger.info("MAX: чат не задан (MAX_CHAT_ID) — карточка не отправлена")
        return {"ok": False, "reason": "no_chat_id"}
    if not max_http.token():
        logger.info("MAX: токен не задан (MAX_TOKEN) — карточка не отправлена")
        return {"ok": False, "reason": "no_token"}
    try:
        max_http.send_message(target, card["text"], attachments=card.get("attachments"))
    except Exception as e:  # noqa: BLE001 — уведомление не должно ронять скан
        logger.warning("MAX: отправка карточки не удалась: %s", e)
        return {"ok": False, "reason": str(e)}
    return {"ok": True}


__all__ = (
    "CALLBACK_PREFIX",
    "DEFAULT_REPLY_TEMPLATE",
    "SERVICES_URL",
    "SNIPPET_LIMIT",
    "ad_request_id_from_payload",
    "build_card",
    "callback_payload",
    "card_for_request",
    "post_url",
    "send_card",
    "services_url",
)
