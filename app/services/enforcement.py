"""Правила «3 пропуска без отмены — запись закрыта».

Пропуск засчитывается страйком только если студент не отменил запись заранее.
Что именно считать страйком — настраивается в .env:
  SELF_REPORTED_ABSENCE_COUNTS — честный ответ «не смог прийти» после тренировки
  SILENCE_COUNTS               — молчание в ответ на опрос
Отметка админа в разделе «посещаемость» перебивает всё.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import settings
from app.models import Attendance, Booking, Student
from app.services import students as students_svc

log = logging.getLogger(__name__)


async def _notify(bot: Bot, tg_user_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_user_id, text)
    except Exception as exc:  # заблокировал бота, удалил чат и т.п.
        log.info("cannot notify %s: %s", tg_user_id, exc)


async def mark_attended(session: AsyncSession, booking: Booking) -> None:
    await _revoke_strike(session, booking)
    booking.attendance = Attendance.ATTENDED
    await session.commit()


async def mark_excused(session: AsyncSession, booking: Booking) -> None:
    await _revoke_strike(session, booking)
    booking.attendance = Attendance.EXCUSED
    await session.commit()


async def _revoke_strike(session: AsyncSession, booking: Booking) -> None:
    """Если за эту запись раньше начислили страйк — снимаем."""
    if not booking.counted_as_strike:
        return
    student = await session.get(Student, booking.student_id)
    if student and student.no_show_count > 0:
        student.no_show_count -= 1
        if student.ban_reason == texts.AUTO_BAN_REASON and student.no_show_count < settings.no_show_limit:
            student.is_banned = False
            student.ban_reason = None
    booking.counted_as_strike = False
    await session.commit()


async def mark_no_show(
    session: AsyncSession,
    bot: Bot,
    booking: Booking,
    *,
    count_strike: bool,
    notify: bool = True,
) -> None:
    booking.attendance = Attendance.NO_SHOW
    await session.commit()

    if not count_strike or booking.counted_as_strike:
        return

    student = booking.student
    booking.counted_as_strike = True
    await session.commit()

    count, just_banned = await students_svc.add_strike(session, student)
    if not notify:
        return

    if just_banned:
        await _notify(bot, student.tg_user_id, texts.AUTO_BANNED.format(limit=settings.no_show_limit))
    else:
        await _notify(
            bot,
            student.tg_user_id,
            texts.STRIKE_WARN.format(count=count, limit=settings.no_show_limit),
        )
