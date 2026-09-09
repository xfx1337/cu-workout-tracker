"""Рисуем расписание недели картинкой — как официальный постер, но со статусами.

Сетка строится по данным: колонки — дни недели, строки — время начала.
Статус каждого занятия («ты записан», «мест нет», «отменена») виден прямо на карточке.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from app.models import Training
from app.tz import MONTHS_RU, WEEKDAYS_RU, now_utc, to_local

# ---------------------------------------------------------------- оформление

BG = (17, 17, 17)
WHITE = (255, 255, 255)
INK = (20, 20, 20)          # текст на цветной карточке
GREY = (78, 78, 78)         # куда «выцветают» недоступные карточки
PILL_BG = (17, 17, 17)

WIDTH = 1240
MARGIN = 44
GAP = 16
CARD_RADIUS = 18
CARD_PAD = 18
PILL_H = 40

PALETTE = {
    "Здоровая спина": (185, 176, 255),
    "Пилатес": (255, 92, 179),
    "Памп": (208, 250, 78),
    "Функциональная тренировка": (240, 52, 70),
    "Боди скульпт": (166, 216, 245),
    "Силовая тренировка": (255, 106, 26),
    "Йога": (30, 155, 240),
    "Стрейчинг": (109, 78, 245),
    "Осанка и баланс": (255, 216, 46),
}
FALLBACK_PALETTE = [
    (208, 250, 78), (255, 92, 179), (30, 155, 240), (255, 216, 46),
    (185, 176, 255), (255, 106, 26), (166, 216, 245), (109, 78, 245),
]

# Шрифты ищем среди системных: Windows / типовой Linux-сервер / macOS.
BOLD_FONTS = ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
              "NotoSans-Bold.ttf", "seguisb.ttf", "Arial Bold.ttf"]
REGULAR_FONTS = ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                 "NotoSans-Regular.ttf", "segoeui.ttf", "Arial.ttf"]


class FontsNotFound(RuntimeError):
    pass


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    for name in (BOLD_FONTS if bold else REGULAR_FONTS):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise FontsNotFound(
        "Не нашёл системный шрифт с кириллицей. На Linux поставь пакет "
        "fonts-dejavu-core или fonts-liberation."
    )


def _blend(color: tuple[int, int, int], target: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(color, target))  # type: ignore[return-value]


def _color_for(title: str) -> tuple[int, int, int]:
    if title in PALETTE:
        return PALETTE[title]
    return FALLBACK_PALETTE[sum(map(ord, title)) % len(FALLBACK_PALETTE)]


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, sizes: tuple[int, ...], bold: bool
) -> ImageFont.FreeTypeFont:
    """Самый крупный размер, при котором ни одно слово не приходится рвать дефисом."""
    words = text.split() or [text]
    for size in sizes:
        font = _font(size, bold)
        if all(draw.textlength(word, font=font) <= max_w for word in words):
            return font
    return _font(sizes[-1], bold)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Перенос по словам; слишком длинное слово рвём по символам."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        line = words[0]
        for word in words[1:]:
            probe = f"{line} {word}"
            if draw.textlength(probe, font=font) <= max_w:
                line = probe
            else:
                lines.append(line)
                line = word
        lines.append(line)

    out: list[str] = []
    for line in lines:
        while draw.textlength(line, font=font) > max_w and len(line) > 1:
            cut = len(line) - 1
            while cut > 1 and draw.textlength(line[:cut] + "-", font=font) > max_w:
                cut -= 1
            out.append(line[:cut] + "-")
            line = line[cut:]
        out.append(line)
    return out


# ---------------------------------------------------------------- данные ячейки

@dataclass(slots=True)
class Cell:
    training: Training
    taken: int
    status: str          # free | booked | full | cancelled | past

    @property
    def color(self) -> tuple[int, int, int]:
        base = _color_for(self.training.title)
        if self.status == "full":
            return _blend(base, GREY, 0.62)
        if self.status == "cancelled":
            return _blend(base, GREY, 0.78)
        if self.status == "past":
            return _blend(base, GREY, 0.84)
        return base

    @property
    def pill_text(self) -> str:
        if self.status == "booked":
            return "ТЫ ЗАПИСАН"
        if self.status == "full":
            return "МЕСТ НЕТ"
        if self.status == "cancelled":
            return "ОТМЕНЕНА"
        if self.status == "past":
            return "ПРОШЛА"
        return f"СВОБОДНО {max(self.training.capacity - self.taken, 0)}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def status_of(training: Training, taken: int, booked_ids: set[int], now: datetime) -> str:
    if training.is_cancelled:
        return "cancelled"
    if training.starts_at + timedelta(minutes=training.duration_min) <= now:
        return "past"
    if training.id in booked_ids:
        return "booked"
    if taken >= training.capacity:
        return "full"
    return "free"


def week_title(week_start: date, week_end: date) -> str:
    if week_start.month == week_end.month:
        return f"{week_start.day} – {week_end.day} {MONTHS_RU[week_end.month - 1]}"
    return (
        f"{week_start.day} {MONTHS_RU[week_start.month - 1]} – "
        f"{week_end.day} {MONTHS_RU[week_end.month - 1]}"
    )


# ---------------------------------------------------------------- рендер

def render_week(
    trainings: list[Training],
    taken_map: dict[int, int],
    booked_ids: set[int],
    week_start: date,
) -> bytes:
    now = now_utc()
    cells: dict[tuple[int, str], list[Cell]] = {}
    times: set[str] = set()
    weekdays: set[int] = set()

    for training in trainings:
        local = to_local(training.starts_at)
        key = (local.weekday(), f"{local:%H:%M}")
        cell = Cell(training, taken_map.get(training.id, 0), status_of(training, taken_map.get(training.id, 0), booked_ids, now))
        cells.setdefault(key, []).append(cell)
        times.add(key[1])
        weekdays.add(key[0])

    # колонки: минимум пн–пт, выходные добавляем только если там что-то есть
    columns = sorted(set(range(5)) | weekdays)
    rows = sorted(times)

    col_w = (WIDTH - 2 * MARGIN - GAP * (len(columns) - 1)) // len(columns)
    inner_w = col_w - 2 * CARD_PAD

    f_title = _font(54, True)
    f_sub = _font(28, False)
    f_day = _font(30, True)
    f_time = _font(25, True)
    f_pill = _font(20, True)

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    all_titles = " ".join({t.title for t in trainings}) or "-"
    all_people = " ".join({t.instructor for t in trainings if t.instructor}) or "-"
    f_name = _fit_font(probe, all_titles, inner_w, (27, 25, 23, 21, 19), True)
    f_who = _fit_font(probe, all_people, inner_w, (21, 19, 17), False)

    # --- считаем высоты ---
    lh_time, lh_name, lh_who = f_time.size + 7, f_name.size + 7, f_who.size + 5

    def card_height(cell: Cell) -> int:
        h = CARD_PAD
        h += len(_wrap(probe, _time_label(cell.training), f_time, inner_w)) * lh_time
        h += 12
        h += len(_wrap(probe, cell.training.title, f_name, inner_w)) * lh_name
        if cell.training.instructor:
            h += 4
            h += len(_wrap(probe, cell.training.instructor, f_who, inner_w)) * lh_who
        h += 16 + PILL_H + CARD_PAD
        return h

    row_heights = []
    for time_key in rows:
        height = 0
        for weekday in columns:
            group = cells.get((weekday, time_key), [])
            if group:
                height = max(height, sum(card_height(c) for c in group) + GAP * (len(group) - 1))
        row_heights.append(height)

    header_h = MARGIN + 62 + 40 + 34 + 52 + 18
    total_h = header_h + sum(row_heights) + GAP * (len(rows) - 1) + MARGIN + 46

    img = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(img)

    # --- шапка ---
    draw.text((MARGIN, MARGIN), "РАСПИСАНИЕ ГРУППОВЫХ ТРЕНИРОВОК", font=f_title, fill=WHITE)
    week_end = week_start + timedelta(days=columns[-1])
    draw.text((MARGIN, MARGIN + 66), week_title(week_start, week_end), font=f_sub, fill=(150, 150, 150))

    # --- плашки дней недели ---
    day_y = MARGIN + 62 + 40 + 12
    for i, weekday in enumerate(columns):
        x = MARGIN + i * (col_w + GAP)
        draw.rounded_rectangle([x, day_y, x + col_w, day_y + 52], radius=12, outline=(72, 72, 72), width=2)
        label = WEEKDAYS_RU[weekday].upper()
        tw = draw.textlength(label, font=f_day)
        draw.text((x + (col_w - tw) / 2, day_y + 10), label, font=f_day, fill=WHITE)

    # --- карточки ---
    y = header_h
    for row_index, time_key in enumerate(rows):
        for col_index, weekday in enumerate(columns):
            group = cells.get((weekday, time_key), [])
            x = MARGIN + col_index * (col_w + GAP)
            card_y = y
            for cell in group:
                h = card_height(cell)
                _draw_card(draw, cell, x, card_y, col_w, h, inner_w,
                           f_time=f_time, f_name=f_name, f_who=f_who, f_pill=f_pill,
                           lh=(lh_time, lh_name, lh_who))
                card_y += h + GAP
        y += row_heights[row_index] + GAP

    # --- легенда ---
    legend = "Нажми день недели под картинкой, чтобы выбрать тренировку"
    draw.text((MARGIN, total_h - MARGIN - 12), legend, font=f_sub, fill=(120, 120, 120))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _time_label(training: Training) -> str:
    start = to_local(training.starts_at)
    end = start + timedelta(minutes=training.duration_min)
    return f"{start:%H:%M} – {end:%H:%M}"


def _draw_card(
    draw: ImageDraw.ImageDraw,
    cell: Cell,
    x: int,
    y: int,
    w: int,
    h: int,
    inner_w: int,
    *,
    f_time, f_name, f_who, f_pill, lh,
) -> None:
    lh_time, lh_name, lh_who = lh
    color = cell.color
    draw.rounded_rectangle([x, y, x + w, y + h], radius=CARD_RADIUS, fill=color)
    if cell.status == "booked":
        draw.rounded_rectangle([x, y, x + w, y + h], radius=CARD_RADIUS, outline=WHITE, width=5)

    tx = x + CARD_PAD
    ty = y + CARD_PAD

    for line in _wrap(draw, _time_label(cell.training), f_time, inner_w):
        draw.text((tx, ty), line, font=f_time, fill=INK)
        ty += lh_time
    ty += 12

    for line in _wrap(draw, cell.training.title, f_name, inner_w):
        draw.text((tx, ty), line, font=f_name, fill=INK)
        ty += lh_name

    if cell.training.instructor:
        ty += 4
        who_color = _blend(color, INK, 0.72)
        for line in _wrap(draw, cell.training.instructor, f_who, inner_w):
            draw.text((tx, ty), line, font=f_who, fill=who_color)
            ty += lh_who

    # плашка со статусом внизу карточки
    pill_y = y + h - CARD_PAD - PILL_H
    text = cell.pill_text
    tw = draw.textlength(text, font=f_pill)
    pill_w = min(int(tw) + 32, inner_w)
    draw.rounded_rectangle([tx, pill_y, tx + pill_w, pill_y + PILL_H], radius=PILL_H // 2, fill=PILL_BG)
    pill_ink = WHITE if cell.status in ("full", "cancelled", "past") else color
    draw.text((tx + (pill_w - tw) / 2, pill_y + 9), text, font=f_pill, fill=pill_ink)
