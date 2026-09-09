from __future__ import annotations

import enum
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import IS_POSTGRES
from app.models import Attendance, Booking, BookingStatus, Student, Training
from app.tz import now_utc


class BookResult(str, enum.Enum):
    OK = "ok"
    ALREADY = "already"
    FULL = "full"
    CANCELLED = "cancelled"
    PAST = "past"
    BANNED = "banned"


class CancelResult(str, enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    TOO_LATE = "too_late"


async def active_for_student(session: AsyncSession, student_id: int) -> list[Booking]:
    stmt = (
        select(Booking)
        .join(Training)
        .where(
            Booking.student_id == student_id,
            Booking.status == BookingStatus.BOOKED,
            Training.starts_at > now_utc(),
            Training.is_cancelled.is_(False),
        )
        .options(selectinload(Booking.training))
        .order_by(Training.starts_at)
    )
    return list(await session.scalars(stmt))


async def history_for_student(session: AsyncSession, student_id: int, limit: int = 20) -> list[Booking]:
    stmt = (
        select(Booking)
        .join(Training)
        .where(Booking.student_id == student_id, Training.starts_at <= now_utc())
        .options(selectinload(Booking.training))
        .order_by(Training.starts_at.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def get_booking(session: AsyncSession, student_id: int, training_id: int) -> Booking | None:
    return await session.scalar(
        select(Booking).where(Booking.student_id == student_id, Booking.training_id == training_id)
    )


async def book(session: AsyncSession, student: Student, training: Training) -> BookResult:
    if student.is_banned:
        return BookResult.BANNED
    if training.is_cancelled:
        return BookResult.CANCELLED
    if training.starts_at <= now_utc():
        return BookResult.PAST

    # На Postgres блокируем строку тренировки: пока идёт подсчёт мест и вставка,
    # параллельный воркер к этому же занятию не подойдёт. На SQLite приёма нет,
    # там от гонки страхует пересчёт очереди после вставки (ниже).
    if IS_POSTGRES:
        await session.execute(select(Training.id).where(Training.id == training.id).with_for_update())

    existing = await get_booking(session, student.id, training.id)
    if existing and existing.status == BookingStatus.BOOKED:
        await session.commit()   # снимаем блокировку строки; менять нечего
        return BookResult.ALREADY

    taken = await session.scalar(
        select(func.count(Booking.id)).where(
            Booking.training_id == training.id, Booking.status == BookingStatus.BOOKED
        )
    ) or 0
    if taken >= training.capacity:
        await session.commit()
        return BookResult.FULL

    if existing:
        # повторная запись после отмены — переиспользуем строку (unique constraint)
        booking = existing
        booking.status = BookingStatus.BOOKED
        booking.attendance = Attendance.UNKNOWN
        booking.counted_as_strike = False
        booking.cancelled_at = None
        booking.reminder_sent_at = None
        booking.poll_sent_at = None
        booking.created_at = now_utc()
    else:
        booking = Booking(training_id=training.id, student_id=student.id, created_at=now_utc())
        session.add(booking)

    await session.commit()

    # Проверка «есть места» и вставка не атомарны: два студента могут одновременно
    # занять последнее место. Пересчитываем очередь и уступаем место тому, кто успел раньше.
    ahead = await session.scalar(
        select(func.count(Booking.id)).where(
            Booking.training_id == training.id,
            Booking.status == BookingStatus.BOOKED,
            Booking.id != booking.id,
            or_(
                Booking.created_at < booking.created_at,
                and_(Booking.created_at == booking.created_at, Booking.id < booking.id),
            ),
        )
    ) or 0
    if ahead >= training.capacity:
        booking.status = BookingStatus.CANCELLED_BY_STUDENT
        booking.cancelled_at = now_utc()
        booking.attendance = Attendance.EXCUSED
        await session.commit()
        return BookResult.FULL

    return BookResult.OK


async def cancel(session: AsyncSession, student: Student, training: Training) -> CancelResult:
    booking = await get_booking(session, student.id, training.id)
    if booking is None or booking.status != BookingStatus.BOOKED:
        return CancelResult.NOT_FOUND

    deadline = training.starts_at - timedelta(minutes=settings.cancel_deadline_minutes)
    if now_utc() > deadline:
        return CancelResult.TOO_LATE

    booking.status = BookingStatus.CANCELLED_BY_STUDENT
    booking.cancelled_at = now_utc()
    booking.attendance = Attendance.EXCUSED
    await session.commit()
    return CancelResult.OK


async def set_attendance(session: AsyncSession, booking: Booking, attendance: Attendance) -> None:
    booking.attendance = attendance
    await session.commit()
