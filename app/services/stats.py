from __future__ import annotations

import csv
import io

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Attendance, Booking, BookingStatus, Student, Training
from app.tz import fmt_short, now_utc


async def overview(session: AsyncSession) -> dict:
    total_students = await session.scalar(select(func.count(Student.id))) or 0
    authorized = await session.scalar(
        select(func.count(Student.id)).where(Student.external_student_id.is_not(None))
    ) or 0
    banned = await session.scalar(select(func.count(Student.id)).where(Student.is_banned.is_(True))) or 0

    total_trainings = await session.scalar(select(func.count(Training.id))) or 0
    upcoming = await session.scalar(
        select(func.count(Training.id)).where(
            Training.starts_at > now_utc(), Training.is_cancelled.is_(False)
        )
    ) or 0

    active_bookings = await session.scalar(
        select(func.count(Booking.id)).where(Booking.status == BookingStatus.BOOKED)
    ) or 0
    attended = await session.scalar(
        select(func.count(Booking.id)).where(Booking.attendance == Attendance.ATTENDED)
    ) or 0
    no_shows = await session.scalar(
        select(func.count(Booking.id)).where(Booking.attendance == Attendance.NO_SHOW)
    ) or 0

    return {
        "total_students": total_students,
        "authorized": authorized,
        "banned": banned,
        "total_trainings": total_trainings,
        "upcoming": upcoming,
        "active_bookings": active_bookings,
        "attended": attended,
        "no_shows": no_shows,
    }


async def per_training(session: AsyncSession, limit: int = 15) -> list[tuple[Training, int, int, int]]:
    """(тренировка, записалось, пришло, не пришло) по прошедшим тренировкам."""
    trainings = list(
        await session.scalars(
            select(Training).where(Training.starts_at <= now_utc()).order_by(Training.starts_at.desc()).limit(limit)
        )
    )
    result = []
    for t in trainings:
        rows = (
            await session.execute(
                select(Booking.attendance, func.count(Booking.id))
                .where(Booking.training_id == t.id)
                .group_by(Booking.attendance)
            )
        ).all()
        by_att = {a: c for a, c in rows}
        booked = await session.scalar(
            select(func.count(Booking.id)).where(
                Booking.training_id == t.id, Booking.status == BookingStatus.BOOKED
            )
        ) or 0
        result.append((t, booked, by_att.get(Attendance.ATTENDED, 0), by_att.get(Attendance.NO_SHOW, 0)))
    return result


async def export_csv(session: AsyncSession) -> bytes:
    stmt = (
        select(Booking)
        .options(selectinload(Booking.student), selectinload(Booking.training))
        .order_by(Booking.id)
    )
    bookings = list(await session.scalars(stmt))

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        [
            "booking_id", "training_id", "training", "starts_at",
            "student_id", "external_student_id", "full_name", "tg_username", "email",
            "status", "attendance", "strike",
        ]
    )
    for b in bookings:
        writer.writerow(
            [
                b.id, b.training_id, b.training.title, fmt_short(b.training.starts_at),
                b.student_id, b.student.external_student_id or "", b.student.full_name or "",
                b.student.tg_username or "", b.student.email or "",
                b.status.value, b.attendance.value, int(b.counted_as_strike),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")
