"""Генерация фирменной графики ИНФО-сообществ: аватар и обложка.

Ребрендинг сети (план 2026-08-29): у 25 сообществ раскатки нет ни аватара, ни
обложки, а ВК официально называет оформление положительным фактором ленты и
обучает поиск на конверсии «показ в выдаче → вступление»
(``docs/ops/vk-findability-playbook.md``). Рисуем плакатный стиль по заказу
владельца: аватар — цветной, диагональные полосы, две строки «ОПАРИНО / ИНФО»;
обложка — крупно «ОПАРИНО - ИНФО», ниже тэглайн одной строкой.

Три инженерных решения:

- **Детерминизм.** Никакого random — только sha256 от кода региона. Повторный
  прогон даёт байт-в-байт тот же файл, поэтому «изменилась ли графика» решается
  сравнением байтов, а версия шаблона живёт в ``TEMPLATE_VERSION`` (бамп при
  правке дизайна = переприменение через ``promo_group_setup.setup_version``).
- **Свой шрифт в репо** (``assets/fonts/DejaVuSans-Bold.ttf``, свободная
  лицензия Bitstream Vera). Системные шрифты не годятся: на Windows-деве DejaVu
  нет вовсе, на Linux путь зависит от пакета — а рендер обязан быть одинаковым.
- **Палитра из хэша.** Оттенок = ``sha256(code) % 360`` при фиксированных
  насыщенности и светлоте: 40+ сообществ различимы между собой, но выглядят
  одной сетью. Смещать оттенок руками не надо — цвет «прибит» к коду региона
  навсегда, и сосед в футерах/репостах всегда узнаваем.

Размеры — по замерам пробы (``docs/ops/group-setup-probe.md``) и живому ответу
ВК: аватар 1024×1024 (минимум 200, важное — в центре, ВК кропает в круг),
обложка 1920×768 (пропорция 2.5:1 — та, в которой ВК её хранит) с безопасной
зоной 1196×768 по центру: мобильный клиент показывает не всю ширину.

⚠️ Пропорцию холста не менять «на глаз». Она обязана совпадать с той, в которой
обложку хранит ВК, — иначе он ужмёт картинку в свою и отрежет бока, и никакие
crop-параметры загрузки этого не исправят. Проверить можно за секунду:
``groups.getById(group_id=<id>, fields="cover")`` печатает все варианты с их
размерами. Замер 2026-08-31: 200×80, 400×160, 911×364, 1080×432, 1920×768.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Версия шаблона: бамп при любой правке дизайна ниже. Канал setup пишет её в
# promo_group_setup.setup_version — повтор той же версии = no-op по уникуму.
#
# v2 (2026-08-31): бамп ради переприменения — кроп загрузки просил 1590×400 из
#   холста 2560×644. Правка была верной, но недостаточной: настоящая причина
#   лежала глубже, см. v3.
# v3 (2026-08-31): холст переведён с 2560×644 на 1920×768. Замер `groups.getById`
#   показал, что ВК хранит обложку в 2.5:1, а мы рисовали 3.975:1 — ВК ужимал
#   картинку в свою пропорцию и резал бока при любых crop-параметрах.
# v4 (2026-11-20): замер по всем 82 сообществам сети показал, что «свой аватар» и
#   «своя обложка» — не деталь, а то, чего нет у половины новых групп: у 12 групп
#   `has_photo=0` (ВК рисует серый плейсхолдер), а включённая обложка стоит только
#   у одной группы из 82. Значит картинка должна читаться с маленького превью в
#   ленте, а обложка — на телефоне. Дизайн переработан, концепт (оттенок из хэша
#   кода, диагональные полосы, «ИМЯ / ИНФО») сохранён:
#   * радиальный градиент вместо плоской заливки — объём без нового цвета;
#   * тёмная плашка под текстом + размытая тень — белый текст перестаёт спорить
#     с полосами и читается в превью;
#   * акцентный оттенок на строке «ИНФО» и на линии-разделителе обложки — глаз
#     цепляется за слово, а не за весь плакат;
#   * тонкое кольцо по краю аватара — круглый кроп ВК становится осмысленным.
TEMPLATE_VERSION = 4

AVATAR_SIZE = 1024

# Холст обложки — 1920×768, то есть пропорция 2.5:1.
#
# Раньше здесь стояло 2560×644 (пропорция 3.975:1) — это старый канон ВК
# 1590×400, отмасштабированный. Канон устарел: живой замер `groups.getById`
# с `fields=cover` 2026-08-31 показал, что ВК хранит обложку РОВНО в 2.5:1 —
# 200×80, 400×160, 911×364, 1080×432, 1920×768, и ни одного варианта в 3.975.
# То есть ВК ужимал нашу картинку в свою пропорцию, отрезая бока: заголовок
# обрывался примерно на 72% («КИКНУР - ИНФО» → «КИКНУР - И»).
#
# Отсюда правило: пропорция холста обязана совпадать с той, в которой ВК
# хранит обложку, иначе кроп неизбежен независимо от crop-параметров загрузки.
# 1920×768 — максимальный вариант из ответа API, берём его.
COVER_W, COVER_H = 1920, 768

# Безопасная зона: мобильный клиент показывает не всю ширину обложки, а
# центральную часть. Держим важное внутри центральных ~62% ширины — этот запас
# и есть разница между «читается на телефоне» и жалобой «обрезается».
COVER_SAFE_W, COVER_SAFE_H = 1196, 768

_FONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets",
    "fonts",
    "DejaVuSans-Bold.ttf",
)

# Палитра сети: насыщенность/светлота фиксированы, оттенок — из хэша кода.
_SAT, _LIGHT = 0.58, 0.40
_STRIPE_LIGHT = 0.30  # полосы темнее фона — читаются, но не спорят с текстом
_JPEG_QUALITY = 92

# v4: свет и глубина (см. комментарий к TEMPLATE_VERSION).
_GLOW_LIGHT = 0.50  # центр холста — светлее
_EDGE_LIGHT = 0.16  # край холста — почти в тон фону
_ACCENT_LIGHT = 0.72  # акцент: строка «ИНФО», разделитель обложки
_PLATE_ALPHA = 175  # плотность плашки под текстом
_STRIPE_ALPHA = 70  # полосы стали мягче — не спорят с плашкой


def region_hue(code: str) -> int:
    """Оттенок региона: детерминированный, 0..359."""
    digest = hashlib.sha256(code.strip().lower().encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 360


def _hls_rgb(hue: int, light: float, sat: float) -> Tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360.0, light, sat)
    return int(r * 255), int(g * 255), int(b * 255)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, max_size: int
) -> ImageFont.FreeTypeFont:
    """Максимальный кегль, при котором строка влезает в ``max_w`` (бинарный поиск)."""
    lo, hi = 8, max_size
    while lo < hi:
        mid = (lo + hi + 1) // 2
        box = draw.textbbox((0, 0), text, font=_font(mid))
        if box[2] - box[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return _font(lo)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _title_lines(name: str) -> List[str]:
    """Аватар: слова имени построчно + строка «ИНФО» всегда последней.

    «Опарино» → [«ОПАРИНО», «ИНФО»]; «Белая Холуница» → [«БЕЛАЯ», «ХОЛУНИЦА»,
    «ИНФО»] — иначе у двухсловных имён «ИНФО» терялась.
    """
    return name.upper().split() + ["ИНФО"]


def _radial_mask(size: Tuple[int, int]) -> Image.Image:
    """L-маска: 0 в центре, 255 по краям (радиальный градиент Pillow)."""
    return Image.radial_gradient("L").resize(size, Image.BICUBIC)


def _gradient_bg(size: Tuple[int, int], hue: int) -> Image.Image:
    """Фон со светом в центре и затемнением по краям — тот же оттенок, больше объёма."""
    center = Image.new("RGB", size, _hls_rgb(hue, _GLOW_LIGHT, _SAT))
    edge = Image.new("RGB", size, _hls_rgb(hue, _EDGE_LIGHT, _SAT))
    return Image.composite(edge, center, _radial_mask(size))


def _stripes_layer(
    size: Tuple[int, int],
    hue: int,
    *,
    count: int,
    width: int,
    spacing: int,
    offset: int,
    alpha: int,
    light: float,
) -> Image.Image:
    """Полосы отдельным RGBA-слоем — их можно делать мягкими (через alpha)."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    color = _hls_rgb(hue, light, _SAT) + (alpha,)
    w, h = size
    x = -h + offset
    for _ in range(count):
        draw.line([(x, h), (x + h, 0)], fill=color, width=width)
        x += spacing
    return layer


def _draw_text_shadowed(
    img: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont,
    xy: Tuple[int, int],
    fill: Tuple[int, int, int],
    shadow: Tuple[int, int, int],
    blur: int = 7,
) -> None:
    """Текст с мягкой размытой тенью — дешёвый способ поднять контраст."""
    draw = ImageDraw.Draw(img)
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    mask = Image.new("L", (w + blur * 4, h + blur * 4), 0)
    ImageDraw.Draw(mask).text((blur * 2 - box[0], blur * 2 - box[1]), text, font=font, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * 0.45))
    img.paste(shadow, (xy[0] + box[0] - blur * 2, xy[1] + box[1] - blur * 2), mask)
    draw.text(xy, text, font=font, fill=fill)


def render_avatar(code: str, name: str) -> bytes:
    """Аватар 1024×1024 JPEG: светящийся фон, «ИМЯ / ИНФО» на плашке.

    ``name`` — короткое имя района без суффиксов («Опарино», «Белая Холуница»);
    в две строки: имя (при двух словах — само по себе двумя строками) + «ИНФО».
    Всё важное — в центральном круге: ВК показывает аватар кругом.
    """
    hue = region_hue(code)
    size = (AVATAR_SIZE, AVATAR_SIZE)
    img = _gradient_bg(size, hue).convert("RGBA")
    img = Image.alpha_composite(
        img,
        _stripes_layer(
            size,
            hue,
            count=5,
            width=56,
            spacing=300,
            offset=hue % 300,
            alpha=_STRIPE_ALPHA,
            light=_STRIPE_LIGHT,
        ),
    )
    # Одна широкая акцентная диагональ — «блик», который ловит взгляд.
    img = Image.alpha_composite(
        img,
        _stripes_layer(
            size,
            hue,
            count=1,
            width=96,
            spacing=size[0] * 2,
            offset=(hue * 11) % size[0],
            alpha=95,
            light=_ACCENT_LIGHT,
        ),
    )

    lines: List[str] = _title_lines(name)
    draw = ImageDraw.Draw(img)

    # Центральный круг ~72% стороны — гарантия видимости после круглого кропа.
    max_text_w = int(AVATAR_SIZE * 0.60)
    fonts = [_fit_font(draw, line, max_text_w, 190) for line in lines]
    heights = [draw.textbbox((0, 0), t, font=f)[3] for t, f in zip(lines, fonts)]
    widths = [_text_w(draw, t, f) for t, f in zip(lines, fonts)]
    gap = 22
    total_h = sum(heights) + gap * (len(lines) - 1)
    block_w = max(widths)

    # Плашка под текстом: тёмная подложка + тонкая акцентная рамка.
    pad_x, pad_y = 48, 42
    x0 = (AVATAR_SIZE - block_w) // 2 - pad_x
    y0 = (AVATAR_SIZE - total_h) // 2 - pad_y
    plate = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        [x0, y0, x0 + block_w + pad_x * 2, y0 + total_h + pad_y * 2],
        radius=58,
        fill=_hls_rgb(hue, 0.10, _SAT) + (_PLATE_ALPHA,),
        outline=_hls_rgb(hue, _ACCENT_LIGHT, _SAT) + (110,),
        width=4,
    )
    img = Image.alpha_composite(img, plate)

    shadow = _hls_rgb(hue, 0.07, _SAT)
    y = (AVATAR_SIZE - total_h) // 2
    for text, font, h, w in zip(lines, fonts, heights, widths):
        x = (AVATAR_SIZE - w) // 2
        color = _hls_rgb(hue, _ACCENT_LIGHT, _SAT) if text == "ИНФО" else (255, 255, 255)
        _draw_text_shadowed(img, text, font, (x, y), color, shadow)
        y += h + gap

    # Тонкое кольцо по краю — круглый кроп ВК становится осмысленным.
    ring = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        [18, 18, AVATAR_SIZE - 18, AVATAR_SIZE - 18], outline=(255, 255, 255, 70), width=6
    )
    img = Image.alpha_composite(img, ring)

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()


def render_cover(code: str, name: str, tagline: str) -> bytes:
    """Обложка 1920×768 JPEG: «ИМЯ - ИНФО» крупно + тэглайн строкой ниже.

    Текст держится в безопасной зоне 1196×768 по центру: мобильный клиент
    обрезает края обложки, десктоп показывает целиком.
    """
    hue = region_hue(code)
    size = (COVER_W, COVER_H)
    img = _gradient_bg(size, hue).convert("RGBA")
    img = Image.alpha_composite(
        img,
        _stripes_layer(
            size,
            hue,
            count=8,
            width=64,
            spacing=420,
            offset=hue % 420,
            alpha=_STRIPE_ALPHA,
            light=_STRIPE_LIGHT,
        ),
    )
    img = Image.alpha_composite(
        img,
        _stripes_layer(
            size,
            hue,
            count=1,
            width=150,
            spacing=size[0] * 2,
            offset=(hue * 7) % size[0],
            alpha=80,
            light=_ACCENT_LIGHT,
        ),
    )

    draw = ImageDraw.Draw(img)
    title = f"{name.upper()} - ИНФО"
    safe_w = COVER_SAFE_W - 140  # поля внутри безопасной зоны
    title_font = _fit_font(draw, title, safe_w, 180)
    tag_font = _fit_font(draw, tagline, safe_w, 60)

    t_box = draw.textbbox((0, 0), title, font=title_font)
    g_box = draw.textbbox((0, 0), tagline, font=tag_font)
    t_h, g_h = t_box[3] - t_box[1], g_box[3] - g_box[1]
    t_w = t_box[2] - t_box[0]
    gap = 26
    rule_h = 8
    total_h = t_h + gap + rule_h + gap + g_h
    y = (COVER_H - total_h) // 2 - t_box[1]
    # Плашка на весь текстовый блок — та же опора, что и на аватаре.
    pad_x, pad_y = 72, 46
    plate = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        [
            (COVER_W - t_w) // 2 - pad_x,
            (COVER_H - total_h) // 2 - pad_y,
            (COVER_W + t_w) // 2 + pad_x,
            (COVER_H + total_h) // 2 + pad_y,
        ],
        radius=48,
        fill=_hls_rgb(hue, 0.10, _SAT) + (_PLATE_ALPHA,),
        outline=_hls_rgb(hue, _ACCENT_LIGHT, _SAT) + (100,),
        width=4,
    )
    img = Image.alpha_composite(img, plate)
    draw = ImageDraw.Draw(img)

    shadow = _hls_rgb(hue, 0.07, _SAT)
    w = _text_w(draw, title, title_font)
    x = (COVER_W - w) // 2
    _draw_text_shadowed(img, title, title_font, (x, y), (255, 255, 255), shadow)
    y += t_h + gap + 12  # разделитель не должен липнуть к выносным элементам букв

    # Акцентная линия-разделитель — взгляд идёт по ней от заголовка к тэглайну.
    rule_w = min(int(t_w * 0.55), 460)
    ImageDraw.Draw(img).rounded_rectangle(
        [
            (COVER_W - rule_w) // 2,
            y,
            (COVER_W + rule_w) // 2,
            y + rule_h,
        ],
        radius=rule_h // 2,
        fill=_hls_rgb(hue, _ACCENT_LIGHT, _SAT) + (235,),
    )
    y += rule_h + gap

    w = _text_w(draw, tagline, tag_font)
    x = (COVER_W - w) // 2
    _draw_text_shadowed(img, tagline, tag_font, (x, y), _hls_rgb(hue, _ACCENT_LIGHT, _SAT), shadow)

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()


def default_tagline(district_name: str, center_city: Optional[str] = None) -> str:
    """Тэглайн обложки: «Афиша, новости и объявления Опаринского округа»."""
    base = (district_name or "").strip() or (center_city or "").strip()
    if base:
        return f"Афиша, новости и объявления — {base}"
    return "Афиша, новости и объявления района"
