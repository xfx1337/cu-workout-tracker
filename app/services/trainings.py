from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Booking, BookingStatus, Student, Training
from app.tz import fmt_range, now_utc, to_utc


async def create(
    session: AsyncSession,
    *,
    title: str,
    description: str,
    location: str,
    starts_at: datetime,
    duration_min: int,
    capacity: int,
    instructor: str = "",
) -> Training:
    training = Training(
        title=title,
        description=description,
        instructor=instructor,
        location=location,
        starts_at=starts_at,
        duration_min=duration_min,
        capacity=capacity,
    )
    session.add(training)
    await session.commit()
    return training


async def by_id(session: AsyncSession, training_id: int) -> Training | None:
    return await session.get(Training, training_id)


async def upcoming(session: AsyncSession, *, include_cancelled: bool = False, limit: int = 50) -> list[Training]:
    stmt = select(Training).where(Training.starts_at > now_utc())
    if not include_cancelled:
        stmt = stmt.where(Training.is_cancelled.is_(False))
    stmt = stmt.order_by(Training.starts_at).limit(limit)
    return list(await session.scalars(stmt))


async def for_week(session: AsyncSession, week_start_local: date) -> list[Training]:
    """Все занятия недели (включая отменённые — их видно на картинке)."""
    start_utc = to_utc(datetime.combine(week_start_local, time.min))
    end_utc = to_utc(datetime.combine(week_start_local + timedelta(days=7), time.min))
    stmt = (
        select(Training)
        .where(Training.starts_at >= start_utc, Training.starts_at < end_utc)
        .order_by(Training.starts_at)
    )
    return list(await session.scalars(stmt))


async def for_day(session: AsyncSession, day_local: date) -> list[Training]:
    start_utc = to_utc(datetime.combine(day_local, time.min))
    end_utc = to_utc(datetime.combine(day_local + timedelta(days=1), time.min))
    stmt = (
        select(Training)
        .where(Training.starts_at >= start_utc, Training.starts_at < end_utc)
        .order_by(Training.starts_at)
    )
    return list(await session.scalars(stmt))


async def past(session: AsyncSession, limit: int = 30) -> list[Training]:
    stmt = select(Training).where(Training.starts_at <= now_utc()).order_by(Training.starts_at.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def taken_map(session: AsyncSession, training_ids: list[int]) -> dict[int, int]:
    """training_id -> сколько мест занято."""
    if not training_ids:
        return {}
    stmt = (
        select(Booking.training_id, func.count(Booking.id))
        .where(Booking.training_id.in_(training_ids), Booking.status == BookingStatus.BOOKED)
        .group_by(Booking.training_id)
    )
    return {tid: cnt for tid, cnt in (await session.execute(stmt)).all()}


async def taken(session: AsyncSession, training_id: int) -> int:
    return await session.scalar(
        select(func.count(Booking.id)).where(
            Booking.training_id == training_id, Booking.status == BookingStatus.BOOKED
        )
    ) or 0


async def participants(session: AsyncSession, training_id: int) -> list[Booking]:
    stmt = (
        select(Booking)
        .where(Booking.training_id == training_id, Booking.status == BookingStatus.BOOKED)
        .options(selectinload(Booking.student))
        .order_by(Booking.created_at)
    )
    return list(await session.scalars(stmt))


async def cancel(session: AsyncSession, training: Training) -> list[Student]:
    """Отменяет тренировку. Возвращает студентов, которых надо оповестить."""
    bookings = await participants(session, training.id)
    affected = [b.student for b in bookings]
    training.is_cancelled = True
    for b in bookings:
        b.status = BookingStatus.CANCELLED_BY_ADMIN
        b.cancelled_at = now_utc()
    await session.commit()
    return affected


def ends_at(training: Training) -> datetime:
    return training.starts_at + timedelta(minutes=training.duration_min)


def card(training: Training, taken_count: int | None = None) -> str:
    lines = [f"<b>{training.title}</b>", fmt_range(training.starts_at, training.duration_min)]
    if training.instructor:
        lines.append(f"🧑‍🏫 {training.instructor}")
    if training.location:
        lines.append(f"📍 {training.location}")
    if training.description:
        lines.append("")
        lines.append(training.description)
    if taken_count is not None:
        free = max(training.capacity - taken_count, 0)
        lines.append("")
        lines.append(f"Мест: {taken_count}/{training.capacity} (свободно {free})")
    if training.is_cancelled:
        lines.insert(0, "❌ <b>ОТМЕНЕНА</b>")
    return "\n".join(lines)


def short_line(training: Training, taken_count: int) -> str:
    from app.tz import fmt_short

    free = max(training.capacity - taken_count, 0)
    mark = "❌" if training.is_cancelled else ("🔴" if free == 0 else "🟢")
    return f"{mark} {fmt_short(training.starts_at)} · {training.title} · {taken_count}/{training.capacity}"
