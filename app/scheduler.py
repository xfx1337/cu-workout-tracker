"""Фоновый тик: напоминания, опрос после тренировки, авто-пропуски.

Состояние хранится в БД (reminder_sent_at / poll_sent_at), поэтому перезапуск
бота ничего не ломает и не приводит к дублям.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import texts
from app.config import settings
from app.db import SessionMaker
from app.keyboards import poll_kb, reminder_kb
from app.models import Attendance, Booking, BookingStatus, Training
from app.services import enforcement
from app.services import trainings as trainings_svc
from app.tz import now_utc

log = logging.getLogger(__name__)

TICK_SECONDS = 60


async def _pending(session: AsyncSession, *conditions) -> list[Booking]:
    stmt = (
        select(Booking)
        .join(Training)
        .where(
            Booking.status == BookingStatus.BOOKED,
            Training.is_cancelled.is_(False),
            *conditions,
        )
        .options(selectinload(Booking.student), selectinload(Booking.training))
    )
    return list(await session.scalars(stmt))


async def send_reminders(session: AsyncSession, bot: Bot) -> int:
    now = now_utc()
    horizon = now + timedelta(minutes=settings.reminder_minutes_before)
    bookings = await _pending(
        session,
        Booking.reminder_sent_at.is_(None),
        Training.starts_at > now,
        Training.starts_at <= horizon,
    )

    sent = 0
    for b in bookings:
        minutes = max(int((b.training.starts_at - now).total_seconds() // 60), 1)
        text = texts.REMINDER.format(minutes=minutes, training=trainings_svc.card(b.training))
        try:
            await bot.send_message(b.student.tg_user_id, text, reply_markup=reminder_kb(b.training_id))
            sent += 1
        except Exception as exc:
            log.info("reminder to %s failed: %s", b.student.tg_user_id, exc)
        b.reminder_sent_at = now
    if bookings:
        await session.commit()
    return sent


async def send_polls(session: AsyncSession, bot: Bot) -> int:
    now = now_utc()
    bookings = await _pending(
        session,
        Booking.poll_sent_at.is_(None),
        Training.starts_at <= now,
    )

    sent = 0
    for b in bookings:
        if trainings_svc.ends_at(b.training) > now:
            continue  # тренировка ещё идёт
        text = texts.POLL.format(training=trainings_svc.card(b.training))
        try:
            await bot.send_message(b.student.tg_user_id, text, reply_markup=poll_kb(b.id))
            sent += 1
        except Exception as exc:
            log.info("poll to %s failed: %s", b.student.tg_user_id, exc)
        b.poll_sent_at = now
        await session.commit()
    return sent


async def close_silent(session: AsyncSession, bot: Bot) -> int:
    """Не ответил на опрос за отведённое время — считаем пропуском."""
    if not settings.silence_counts:
        return 0

    deadline = now_utc() - timedelta(hours=settings.silence_grace_hours)
    bookings = await _pending(
        session,
        Booking.poll_sent_at.is_not(None),
        Booking.poll_sent_at <= deadline,
        Booking.attendance == Attendance.UNKNOWN,
    )

    for b in bookings:
        await enforcement.mark_no_show(session, bot, b, count_strike=True)
    return len(bookings)


async def tick(bot: Bot) -> None:
    async with SessionMaker() as session:
        reminders = await send_reminders(session, bot)
        polls = await send_polls(session, bot)
        closed = await close_silent(session, bot)
    if reminders or polls or closed:
        log.info("tick: reminders=%s polls=%s no_shows=%s", reminders, polls, closed)


async def run(bot: Bot) -> None:
    log.info("scheduler started (tick=%ss)", TICK_SECONDS)
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
