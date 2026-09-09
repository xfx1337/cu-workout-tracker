"""Всё время в БД хранится наивным UTC. Наружу показываем в локальной зоне."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings

LOCAL_TZ = ZoneInfo(settings.tz_name)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def to_utc(dt_local_naive: datetime) -> datetime:
    return dt_local_naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def parse_local(text: str) -> datetime:
    """'25.09.2026 18:30' (локальное время) -> наивный UTC."""
    dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
    return to_utc(dt)


def fmt_dt(dt_utc: datetime) -> str:
    d = to_local(dt_utc)
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.day} {MONTHS_RU[d.month - 1]}, {d:%H:%M}"


def fmt_range(start_utc: datetime, duration_min: int) -> str:
    d = to_local(start_utc)
    end = d + timedelta(minutes=duration_min)
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.day} {MONTHS_RU[d.month - 1]}, {d:%H:%M}–{end:%H:%M}"


def fmt_short(dt_utc: datetime) -> str:
    d = to_local(dt_utc)
    return f"{d:%d.%m} {WEEKDAYS_RU[d.weekday()]} {d:%H:%M}"


# ---------- недели ----------

def local_today() -> date:
    return to_local(now_utc()).date()


def current_week_start() -> date:
    """Понедельник текущей недели в локальной зоне."""
    today = local_today()
    return today - timedelta(days=today.weekday())


def week_start(offset: int) -> date:
    """Понедельник недели, отстоящей от текущей на offset недель."""
    return current_week_start() + timedelta(weeks=offset)


def week_offset_of(dt_utc: datetime) -> int:
    """В какой неделе относительно текущей лежит момент времени."""
    day = to_local(dt_utc).date()
    return ((day - timedelta(days=day.weekday())) - current_week_start()).days // 7
