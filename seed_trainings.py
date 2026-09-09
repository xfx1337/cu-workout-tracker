"""Заливка расписания в БД.

Расписание в ЦУ фиксированное и недельное, поэтому здесь описывается один
типовой week-pattern, а скрипт разворачивает его на нужный период.

Как пользоваться:
    1. Впиши занятия из картинки с расписанием в SCHEDULE.
    2. Поставь период в WEEKS_FROM / WEEKS_TO.
    3. python seed_trainings.py            # покажет, что создаст (dry-run)
       python seed_trainings.py --apply    # реально запишет в БД

Повторный запуск безопасен: занятия с тем же названием и временем не дублируются.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionMaker, init_db
from app.models import Training
from app.tz import fmt_dt, to_utc

# ---------------------------------------------------------------- расписание

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@dataclass(frozen=True)
class Slot:
    weekday: int
    time: str            # "18:30"
    title: str
    instructor: str = ""
    description: str = ""
    location: str = ""
    duration_min: int = 60
    capacity: int = settings.default_capacity


# Описания — рабочий вариант, правится здесь или в админке у конкретного занятия.
DESCRIPTIONS = {
    "Здоровая спина": "Мягкий комплекс на укрепление мышц спины и кора. Подойдёт после долгого дня за ноутбуком.",
    "Пилатес": "Работа с глубокими мышцами, контроль дыхания и осанки. Уровень: любой.",
    "Памп": "Силовая тренировка со штангой под музыку. Высокий темп, много повторений.",
    "Функциональная тренировка": "Круговая тренировка на всё тело: сила, выносливость, координация.",
    "Боди скульпт": "Проработка всех групп мышц с собственным весом и небольшим оборудованием.",
    "Силовая тренировка": "Базовые силовые упражнения с отягощением. Тренер поможет с техникой.",
    "Йога": "Асаны, растяжка и дыхание. Спокойный темп, коврики есть.",
    "Стрейчинг": "Растяжка всего тела, работа над гибкостью и подвижностью суставов.",
    "Осанка и баланс": "Упражнения на устойчивость, равновесие и правильное положение спины.",
}

LOCATION = "Спортзал ЦУ"


def _slot(weekday: int, time: str, title: str, instructor: str) -> Slot:
    return Slot(weekday, time, title, instructor, DESCRIPTIONS.get(title, ""), LOCATION)


# Расписание групповых тренировок ЦУ (с официальной картинки)
SCHEDULE: list[Slot] = [
    # понедельник
    _slot(MON, "18:30", "Памп", "Серикова Наталья"),
    _slot(MON, "19:40", "Пилатес", "Серикова Наталья"),
    # вторник
    _slot(TUE, "08:30", "Здоровая спина", "Ломовицкая Юлия"),
    _slot(TUE, "18:30", "Функциональная тренировка", "Серикова Наталья"),
    _slot(TUE, "19:40", "Йога", "Ломовицкая Юлия"),
    # среда
    _slot(WED, "18:30", "Боди скульпт", "Котова Юлия"),
    _slot(WED, "19:40", "Стрейчинг", "Котова Юлия"),
    # четверг
    _slot(THU, "08:30", "Пилатес", "Котова Юлия"),
    _slot(THU, "18:30", "Силовая тренировка", "Солобуденко Анастасия"),
    _slot(THU, "19:40", "Здоровая спина", "Ломовицкая Юлия"),
    # пятница
    _slot(FRI, "18:30", "Функциональная тренировка", "Солобуденко Анастасия"),
    _slot(FRI, "19:40", "Осанка и баланс", "Солобуденко Анастасия"),
]

# Период, на который разворачиваем расписание (включительно)
WEEKS_FROM = date(2026, 9, 8)
WEEKS_TO = date(2026, 12, 25)

# ---------------------------------------------------------------------------


def generate() -> list[tuple[Slot, datetime]]:
    """Возвращает пары (слот, время начала в UTC)."""
    result: list[tuple[Slot, datetime]] = []
    day = WEEKS_FROM
    while day <= WEEKS_TO:
        for slot in SCHEDULE:
            if slot.weekday != day.weekday():
                continue
            hh, mm = (int(x) for x in slot.time.split(":"))
            starts_at = to_utc(datetime(day.year, day.month, day.day, hh, mm))
            result.append((slot, starts_at))
        day += timedelta(days=1)
    return sorted(result, key=lambda pair: pair[1])


async def run(apply: bool) -> None:
    await init_db()
    planned = generate()

    created = skipped = 0
    async with SessionMaker() as session:
        for slot, starts_at in planned:
            exists = await session.scalar(
                select(Training).where(Training.title == slot.title, Training.starts_at == starts_at)
            )
            if exists:
                skipped += 1
                continue
            created += 1
            print(f"  + {fmt_dt(starts_at)} — {slot.title} / {slot.instructor} ({slot.capacity} мест)")
            if apply:
                session.add(
                    Training(
                        title=slot.title,
                        description=slot.description,
                        instructor=slot.instructor,
                        location=slot.location,
                        starts_at=starts_at,
                        duration_min=slot.duration_min,
                        capacity=slot.capacity,
                    )
                )
        if apply:
            await session.commit()

    print()
    print(f"Всего в плане: {len(planned)} | новых: {created} | уже есть: {skipped}")
    if not apply:
        print("Это был dry-run. Чтобы записать в БД: python seed_trainings.py --apply")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
