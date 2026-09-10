from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import settings
from app.models import Admin, Attendance, Booking, BookingStatus, Student
from app.render import plural_ru, render_week, week_title
from app.runtime import BotContext, UserSession
from app.services import bookings as bookings_svc
from app.services import enforcement
from app.services import trainings as trainings_svc
from app.services.bookings import BookResult, CancelResult
from app.tz import MONTHS_RU, WEEKDAYS_RU, fmt_short, to_local, week_start
from app.ui import ADMIN, HELP, MY, Choice, group, screen

log = logging.getLogger(__name__)

MAX_WEEK_OFFSET = 30

ATTENDANCE_WORDS = {
    Attendance.ATTENDED: "✅ был",
    Attendance.NO_SHOW: "❌ пропуск",
    Attendance.EXCUSED: "➖ отменил",
    Attendance.UNKNOWN: "❔ без отметки",
}


async def _gate(ctx: BotContext, s: UserSession, student: Student) -> bool:
    from app.handlers.common import show_consent

    if student.is_banned:
        await ctx.notify(s.user_id, texts.BANNED.format(reason=student.ban_reason or "решение администратора"))
        return False
    if student.consent_accepted_at is None:
        await show_consent(ctx, None, s, student)
        return False
    return True


# ---------- недельная сетка картинкой ----------

async def open_schedule(
    ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0,
) -> None:
    if not await _gate(ctx, s, student):
        return
    monday = week_start(offset)
    items = await trainings_svc.for_week(session, monday)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}

    caption = (
        f"<b>Расписание</b> · {week_title(monday, monday + timedelta(days=6))}\n"
        "Выбери день недели 👇"
    )
    if not items:
        caption = f"<b>{week_title(monday, monday + timedelta(days=6))}</b>\nНа эту неделю занятий нет."

    try:
        png = render_week(items, taken, booked_ids, monday)
    except Exception:
        log.exception("не удалось нарисовать расписание, отдаю текстом")
        await _text_schedule(ctx, session, s, student)
        return

    live = [t for t in items if not t.is_cancelled]
    days = sorted({to_local(t.starts_at).weekday() for t in live})
    has_next = offset < MAX_WEEK_OFFSET and bool(
        await trainings_svc.for_week(session, week_start(offset + 1))
    )

    day_choices = [Choice(WEEKDAYS_RU[weekday], f"wk:day:{weekday}") for weekday in days]
    nav: list[Choice] = [MY, HELP]
    if offset > 0:
        nav.insert(0, Choice("← Прошлая неделя", f"wk:week:{offset - 1}", emoji="arrow_left"))
    if has_next:
        nav.insert(0, Choice("Следующая неделя →", f"wk:week:{offset + 1}", emoji="arrow_right"))
    if admin:
        nav.append(ADMIN)

    await ctx.show(
        s.user_id, screen(caption, group(*day_choices), group(*nav), image=png, filename=f"schedule-{monday}.png"),
        data={"screen": "week", "offset": offset},
    )


async def show_day(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, offset: int, weekday: int) -> None:
    day = week_start(offset) + timedelta(days=weekday)
    items = [t for t in await trainings_svc.for_day(session, day) if not t.is_cancelled]
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}

    lines = [f"<b>{WEEKDAYS_RU[weekday]}, {day.day} {MONTHS_RU[day.month - 1]}</b>", ""]
    if not items:
        lines.append("В этот день занятий нет.")
    choices: list[Choice] = []
    for t in items:
        free = max(t.capacity - taken.get(t.id, 0), 0)
        if t.id in booked_ids:
            note = "ты записан"
        elif free == 0:
            note = "мест нет"
        else:
            note = f"свободно {free} {plural_ru(free, 'место', 'места', 'мест')}"
        who = f" · {t.instructor}" if t.instructor else ""
        start = to_local(t.starts_at)
        lines.append(f"<b>{start:%H:%M}</b> {t.title}{who} — {note}")
        choices.append(Choice(f"{start:%H:%M} {t.title}", f"tr:view:{t.id}"))

    lines += ["", "Выбери тренировку 👇"]
    choices.append(Choice("Назад", "nav:schedule", emoji="arrow_left"))

    await ctx.show(
        s.user_id, screen("\n".join(lines), group(*choices)),
        data={"screen": "day", "offset": offset, "weekday": weekday},
    )


async def show_training(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    if training is None:
        await ctx.notify(s.user_id, "Тренировка не найдена.")
        return
    taken = await trainings_svc.taken(session, training.id)
    booking = await bookings_svc.get_booking(session, student.id, training.id)
    is_booked = booking is not None and booking.status is BookingStatus.BOOKED

    text = trainings_svc.card(training, taken)
    if training.is_cancelled:
        text += "\n\nЗанятие не состоится, записаться нельзя."
    elif is_booked:
        text += "\n\n✅ <b>Ты записан.</b>"

    offset = s.data.get("offset", 0)
    weekday = s.data.get("weekday", -1)
    choices: list[Choice] = []
    if not training.is_cancelled:
        if is_booked:
            choices.append(Choice("Отменить запись", f"tr:cancel:{training.id}", style="danger", emoji="x"))
        else:
            choices.append(Choice("Записаться", f"tr:book:{training.id}", style="primary", emoji="heavy_plus_sign"))
    if weekday >= 0:
        choices.append(Choice("Назад", f"wk:day:{weekday}", emoji="arrow_left"))
    else:
        choices.append(Choice("Назад", "nav:schedule", emoji="arrow_left"))
    choices += [MY, HELP]

    await ctx.show(
        s.user_id, screen(text, group(*choices)),
        data={"screen": "training", "offset": offset, "weekday": weekday, "training_id": training.id},
    )


async def _text_schedule(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student) -> None:
    items = await trainings_svc.upcoming(session)
    if not items:
        await ctx.show(
            s.user_id, screen(texts.NO_TRAININGS, group(HELP)),
            data={"screen": "help"},
        )
        return
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}
    lines = ["<b>Ближайшие тренировки</b>", ""]
    choices: list[Choice] = []
    for t in items[:9]:
        free = max(t.capacity - taken.get(t.id, 0), 0)
        mark = "✅" if t.id in booked_ids else ("🔴" if free == 0 else "🟢")
        lines.append(f"{mark} {fmt_short(t.starts_at)} · {t.title} · {taken.get(t.id, 0)}/{t.capacity}")
        choices.append(Choice(fmt_short(t.starts_at), f"tr:view:{t.id}"))
    lines += ["", "Нажми тренировку:"]
    choices.append(HELP)
    await ctx.show(s.user_id, screen("\n".join(lines), group(*choices)))


# ---------- запись и отмена ----------

async def book(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, training_id: int) -> None:
    if not await _gate(ctx, s, student):
        return
    training = await trainings_svc.by_id(session, training_id)
    if training is None:
        await ctx.notify(s.user_id, "Тренировка не найдена.")
        return

    result = await bookings_svc.book(session, student, training)
    if result is not BookResult.OK:
        alerts = {
            BookResult.ALREADY: texts.ALREADY_BOOKED,
            BookResult.FULL: texts.FULL.format(capacity=training.capacity),
            BookResult.CANCELLED: "Эта тренировка отменена.",
            BookResult.PAST: "Эта тренировка уже прошла.",
            BookResult.BANNED: "Запись для тебя закрыта.",
        }
        await ctx.notify(s.user_id, alerts[result])
        await show_training(ctx, session, s, student, training.id)
        return

    taken = await trainings_svc.taken(session, training.id)
    await ctx.notify(s.user_id, texts.BOOKED_OK.format(
        training=trainings_svc.card(training, taken), minutes=settings.reminder_minutes_before
    ))
    await show_training(ctx, session, s, student, training.id)


async def cancel(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    if training is None:
        await ctx.notify(s.user_id, "Тренировка не найдена.")
        return

    result = await bookings_svc.cancel(session, student, training)
    if result is CancelResult.TOO_LATE:
        await ctx.notify(s.user_id, texts.CANCEL_TOO_LATE.format(minutes=settings.cancel_deadline_minutes))
        return
    if result is CancelResult.NOT_FOUND:
        await ctx.notify(s.user_id, "Активной записи нет.")
        return

    taken = await trainings_svc.taken(session, training.id)
    await ctx.notify(s.user_id, f"{texts.CANCELLED_OK}\n\n{trainings_svc.card(training, taken)}")
    await show_training(ctx, session, s, student, training.id)


async def my_bookings(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0) -> None:
    if not await _gate(ctx, s, student):
        return
    active = await bookings_svc.active_for_student(session, student.id)
    lines = ["<b>Твои записи</b>", ""]
    if not active:
        lines.append(texts.NO_BOOKINGS)
    else:
        for i, b in enumerate(active, 1):
            lines.append(f"{i}. {fmt_short(b.training.starts_at)} — {b.training.title}")

    history = await bookings_svc.history_for_student(session, student.id, limit=5)
    if history:
        lines += ["", "<b>История</b>", ""]
        for b in history:
            note = (
                "🚫 занятие отменили"
                if b.status is BookingStatus.CANCELLED_BY_ADMIN
                else ATTENDANCE_WORDS[b.attendance]
            )
            lines.append(f"• {fmt_short(b.training.starts_at)} — {b.training.title} — {note}")
    if student.no_show_count:
        lines += ["", f"⚠️ Пропусков без отмены: {student.no_show_count} из {settings.no_show_limit}"]

    choices: list[Choice] = [Choice(f"Отменить: {b.training.title}", f"tr:cancel:{b.training_id}")
                             for b in active]
    choices.append(Choice("Назад", "nav:schedule", emoji="arrow_left"))
    choices += [HELP]
    if admin:
        choices.append(ADMIN)
    if active:
        lines += ["", "Можно отменить запись:"]

    await ctx.show(
        s.user_id, screen("\n".join(lines), group(*choices)),
        data={"screen": "my", "offset": offset},
    )


async def poll_answer(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, booking_id: int, came: bool) -> None:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        await ctx.notify(s.user_id, "Запись не найдена.")
        return

    if came:
        await enforcement.mark_attended(session, booking)
        await ctx.notify(s.user_id, texts.POLL_THANKS_YES)
        return

    if settings.self_reported_absence_counts:
        await session.refresh(booking, ["student"])
        await enforcement.mark_no_show(session, ctx, booking, count_strike=True)
    else:
        await enforcement.mark_excused(session, booking)
    await ctx.notify(s.user_id, texts.POLL_THANKS_NO)