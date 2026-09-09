from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import settings
from app.keyboards import (
    NavCB, PollCB, TrainingCB, WeekCB, day_kb, my_bookings_kb, schedule_kb, training_kb, week_kb,
)
from app.models import Admin, Attendance, Booking, BookingStatus, Student, Training
from app.render import plural_ru, render_week, week_title
from app.services import bookings as bookings_svc
from app.services import enforcement
from app.services import trainings as trainings_svc
from app.services.bookings import BookResult, CancelResult
from app.tz import (
    MONTHS_RU, WEEKDAYS_RU, fmt_short, to_local, week_offset_of, week_start,
)
from app.utils import safe_edit, safe_edit_caption

log = logging.getLogger(__name__)
router = Router(name="student")


async def _gate(message: Message, student: Student) -> bool:
    """Пускаем к записи только авторизованных, подтвердивших справку и не забаненных."""
    from app.handlers.common import send_auth_prompt, send_consent

    if not student.is_authorized:
        await send_auth_prompt(message, student)
        return False
    if student.consent_accepted_at is None:
        await send_consent(message)
        return False
    if student.is_banned:
        await message.answer(texts.BANNED.format(reason=student.ban_reason or "решение администратора"))
        return False
    return True


# ---------- недельная сетка картинкой ----------

MAX_WEEK_OFFSET = 30


async def _week_view(
    session: AsyncSession, student: Student, offset: int, is_admin: bool = False
) -> tuple[bytes, str, InlineKeyboardMarkup]:
    monday = week_start(offset)
    items = await trainings_svc.for_week(session, monday)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}

    png = render_week(items, taken, booked_ids, monday)

    # На картинке отменённые занятия видны (плашкой «ОТМЕНЕНА»), но записываться
    # на них нельзя — поэтому в кнопки дней и в списки они не попадают.
    live = [t for t in items if not t.is_cancelled]

    days: list[tuple[int, bool]] = []
    for weekday in sorted({to_local(t.starts_at).weekday() for t in live}):
        same_day = [t for t in live if to_local(t.starts_at).weekday() == weekday]
        days.append((weekday, any(t.id in booked_ids for t in same_day)))

    has_next = offset < MAX_WEEK_OFFSET and bool(
        await trainings_svc.for_week(session, week_start(offset + 1))
    )

    caption = (
        f"<b>Расписание</b> · {week_title(monday, monday + timedelta(days=6))}\n"
        "Выбери день недели 👇"
    )
    if not items:
        caption = f"<b>{week_title(monday, monday + timedelta(days=6))}</b>\nНа эту неделю занятий нет."
    return png, caption, week_kb(offset, days, has_next, is_admin)


async def _send_week(
    message: Message,
    session: AsyncSession,
    student: Student,
    offset: int,
    *,
    edit: bool,
    is_admin: bool = False,
) -> None:
    png, caption, kb = await _week_view(session, student, offset, is_admin)
    photo = BufferedInputFile(png, filename=f"schedule-{week_start(offset)}.png")
    if edit and message.photo:
        await message.edit_media(
            InputMediaPhoto(media=photo, caption=caption, parse_mode=ParseMode.HTML),
            reply_markup=kb,
        )
    else:
        await message.answer_photo(photo, caption=caption, reply_markup=kb)


async def open_schedule(
    message: Message,
    session: AsyncSession,
    student: Student,
    admin: Admin | None,
    offset: int = 0,
    *,
    edit: bool = False,
) -> None:
    """Единая точка входа в расписание — из /start, из авторизации, из кнопок."""
    if student.is_banned:
        await message.answer(texts.BANNED.format(reason=student.ban_reason or "решение администратора"))
        return
    try:
        await _send_week(message, session, student, offset, edit=edit, is_admin=bool(admin))
    except Exception:
        log.exception("не удалось нарисовать расписание, отдаю текстом")
        text, kb = await _text_schedule(session, student)
        await message.answer(text, reply_markup=kb)


async def _day_view(
    session: AsyncSession, student: Student, offset: int, weekday: int
) -> tuple[str, InlineKeyboardMarkup]:
    day = week_start(offset) + timedelta(days=weekday)
    # отменённые занятия студенту показываем только на картинке
    items = [t for t in await trainings_svc.for_day(session, day) if not t.is_cancelled]
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}

    lines = [f"<b>{WEEKDAYS_RU[weekday]}, {day.day} {MONTHS_RU[day.month - 1]}</b>", ""]
    if not items:
        lines.append("В этот день занятий нет.")
    for t in items:
        free = max(t.capacity - taken.get(t.id, 0), 0)
        start = to_local(t.starts_at)
        if t.id in booked_ids:
            note = "ты записан"
        elif free == 0:
            note = "мест нет"
        else:
            note = f"свободно {free} {plural_ru(free, 'место', 'места', 'мест')}"
        who = f" · {t.instructor}" if t.instructor else ""
        lines.append(f"<b>{start:%H:%M}</b> {t.title}{who} — {note}")

    if items:
        lines += ["", "Выбери тренировку, чтобы прочитать описание и записаться."]
    triples = [(t, taken.get(t.id, 0), t.id in booked_ids) for t in items]
    return "\n".join(lines), day_kb(offset, weekday, triples)


async def _training_caption(
    session: AsyncSession, student: Student, training: Training
) -> tuple[str, bool]:
    taken = await trainings_svc.taken(session, training.id)
    booking = await bookings_svc.get_booking(session, student.id, training.id)
    is_booked = booking is not None and booking.status is BookingStatus.BOOKED
    text = trainings_svc.card(training, taken)
    if training.is_cancelled:
        text += "\n\nЗанятие не состоится, записаться нельзя."
    elif is_booked:
        text += "\n\n✅ <b>Ты записан.</b>"
    return text, is_booked


async def _show(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Экран может быть и фото с подписью (сетка), и обычным текстом (напоминалка)."""
    if message.photo:
        await safe_edit_caption(message, text, kb)
    else:
        await safe_edit(message, text, kb)


@router.message(Command("schedule"))
async def cmd_schedule(
    message: Message, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    if not await _gate(message, student):
        return
    await open_schedule(message, session, student, admin, offset=0)


@router.callback_query(NavCB.filter(F.action == "schedule"))
async def nav_schedule(
    call: CallbackQuery, callback_data: NavCB, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    await call.answer()

    # Расписание — это фото. Если нажали из текстового экрана (админка, напоминалка),
    # править нечего: убираем его и присылаем свежую картинку.
    if not call.message.photo:
        try:
            await call.message.delete()
        except TelegramBadRequest as exc:
            log.info("не смог удалить сообщение перед показом расписания: %s", exc)
        await open_schedule(call.message, session, student, admin, callback_data.offset, edit=False)
        return

    await open_schedule(call.message, session, student, admin, callback_data.offset, edit=True)


@router.callback_query(WeekCB.filter(F.action == "week"))
async def show_week(
    call: CallbackQuery, callback_data: WeekCB, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    await call.answer()
    await _send_week(
        call.message, session, student, callback_data.offset, edit=True, is_admin=bool(admin)
    )


@router.callback_query(WeekCB.filter(F.action == "day"))
async def show_day(
    call: CallbackQuery, callback_data: WeekCB, session: AsyncSession, student: Student
) -> None:
    text, kb = await _day_view(session, student, callback_data.offset, callback_data.weekday)
    await call.answer()
    await _show(call.message, text, kb)


# ---------- запасной текстовый вариант ----------

async def _text_schedule(session: AsyncSession, student: Student) -> tuple[str, object | None]:
    items = await trainings_svc.upcoming(session)
    if not items:
        return texts.NO_TRAININGS, None
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    pairs = [(t, taken.get(t.id, 0)) for t in items]
    booked_ids = {b.training_id for b in await bookings_svc.active_for_student(session, student.id)}
    text = (
        "<b>Ближайшие тренировки</b>\n"
        "🟢 есть места · 🔴 мест нет · ✅ ты записан\n\n"
        "Нажми на тренировку, чтобы посмотреть описание и записаться."
    )
    return text, schedule_kb(pairs, booked_ids)


@router.callback_query(TrainingCB.filter(F.action == "back"))
async def back_to_schedule(call: CallbackQuery, session: AsyncSession, student: Student) -> None:
    text, kb = await _text_schedule(session, student)
    await call.answer()
    await safe_edit(call.message, text, kb)


# ---------- карточка тренировки, запись и отмена ----------

@router.callback_query(TrainingCB.filter(F.action == "view"))
async def view_training(
    call: CallbackQuery, callback_data: TrainingCB, session: AsyncSession, student: Student
) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    if training is None:
        await call.answer("Тренировка не найдена", show_alert=True)
        return

    text, is_booked = await _training_caption(session, student, training)
    await call.answer()
    await _show(
        call.message, text,
        training_kb(training, is_booked, callback_data.offset, callback_data.weekday),
    )


@router.callback_query(TrainingCB.filter(F.action == "book"))
async def book_training(
    call: CallbackQuery, callback_data: TrainingCB, session: AsyncSession, student: Student
) -> None:
    if not await _gate(call.message, student):
        await call.answer()
        return

    training = await trainings_svc.by_id(session, callback_data.training_id)
    if training is None:
        await call.answer("Тренировка не найдена", show_alert=True)
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
        await call.answer(alerts[result], show_alert=True)
        # состояние могло измениться, пока студент смотрел карточку — перерисуем
        await _refresh_after_change(call, session, student, training, callback_data)
        return

    await call.answer("Записал ✅")
    taken = await trainings_svc.taken(session, training.id)
    text = texts.BOOKED_OK.format(
        training=trainings_svc.card(training, taken), minutes=settings.reminder_minutes_before
    )
    await _refresh_after_change(call, session, student, training, callback_data, caption=text)


@router.callback_query(TrainingCB.filter(F.action == "cancel"))
async def cancel_booking(
    call: CallbackQuery, callback_data: TrainingCB, session: AsyncSession, student: Student
) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    if training is None:
        await call.answer("Тренировка не найдена", show_alert=True)
        return

    result = await bookings_svc.cancel(session, student, training)

    if result is CancelResult.TOO_LATE:
        await call.answer(
            texts.CANCEL_TOO_LATE.format(minutes=settings.cancel_deadline_minutes), show_alert=True
        )
        return
    if result is CancelResult.NOT_FOUND:
        await call.answer("Активной записи нет", show_alert=True)
        return

    await call.answer("Запись отменена")
    taken = await trainings_svc.taken(session, training.id)
    text = f"{texts.CANCELLED_OK}\n\n{trainings_svc.card(training, taken)}"
    await _refresh_after_change(call, session, student, training, callback_data, caption=text)


async def _refresh_after_change(
    call: CallbackQuery,
    session: AsyncSession,
    student: Student,
    training: Training,
    callback_data: TrainingCB,
    caption: str | None = None,
) -> None:
    """После записи/отмены статус изменился — перерисовываем картинку недели."""
    text, is_booked = await _training_caption(session, student, training)
    if caption is not None:
        text = caption

    if not call.message.photo:
        await safe_edit(call.message, text, training_kb(training, is_booked))
        return

    offset = callback_data.offset if callback_data.weekday >= 0 else week_offset_of(training.starts_at)
    weekday = callback_data.weekday if callback_data.weekday >= 0 else to_local(training.starts_at).weekday()
    png, _, _ = await _week_view(session, student, offset)
    await call.message.edit_media(
        InputMediaPhoto(
            media=BufferedInputFile(png, filename="schedule.png"),
            caption=text,
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=training_kb(training, is_booked, offset, weekday),
    )


ATTENDANCE_WORDS = {
    Attendance.ATTENDED: "✅ был",
    Attendance.NO_SHOW: "❌ пропуск",
    Attendance.EXCUSED: "➖ отменил",
    Attendance.UNKNOWN: "❔ без отметки",
}


async def _my_view(
    session: AsyncSession, student: Student, offset: int, is_admin: bool
) -> tuple[str, InlineKeyboardMarkup]:
    active = await bookings_svc.active_for_student(session, student.id)

    lines = ["<b>Твои записи</b>", ""]
    if not active:
        lines.append(texts.NO_BOOKINGS)
    for b in active:
        lines.append(f"• {fmt_short(b.training.starts_at)} — {b.training.title}")

    history = await bookings_svc.history_for_student(session, student.id, limit=5)
    if history:
        lines += ["", "<b>История</b>", ""]
        for b in history:
            # занятие отменил админ — это не пропуск и не отметка студента
            note = (
                "🚫 занятие отменили"
                if b.status is BookingStatus.CANCELLED_BY_ADMIN
                else ATTENDANCE_WORDS[b.attendance]
            )
            lines.append(f"• {fmt_short(b.training.starts_at)} — {b.training.title} — {note}")
    if student.no_show_count:
        lines += ["", f"⚠️ Пропусков без отмены: {student.no_show_count} из {settings.no_show_limit}"]

    return "\n".join(lines), my_bookings_kb(active, offset, is_admin)


@router.message(Command("my"))
async def cmd_my(
    message: Message, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    if not await _gate(message, student):
        return
    text, kb = await _my_view(session, student, 0, bool(admin))
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCB.filter(F.action == "my"))
async def nav_my(
    call: CallbackQuery, callback_data: NavCB, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    text, kb = await _my_view(session, student, callback_data.offset, bool(admin))
    await call.answer()
    await _show(call.message, text, kb)


@router.callback_query(PollCB.filter())
async def poll_answer(
    call: CallbackQuery, callback_data: PollCB, session: AsyncSession, bot: Bot
) -> None:
    booking = await session.get(Booking, callback_data.booking_id)
    if booking is None:
        await call.answer("Запись не найдена", show_alert=True)
        return

    if callback_data.came:
        await enforcement.mark_attended(session, booking)
        await call.answer("Спасибо!")
        await call.message.edit_text(f"{call.message.text}\n\n✅ Отмечено: был на тренировке")
        await call.message.answer(texts.POLL_THANKS_YES)
        return

    # честно признался, что не пришёл
    if settings.self_reported_absence_counts:
        await session.refresh(booking, ["student"])
        await enforcement.mark_no_show(session, bot, booking, count_strike=True)
    else:
        await enforcement.mark_excused(session, booking)

    await call.answer()
    await call.message.edit_text(f"{call.message.text}\n\n❌ Отмечено: не был на тренировке")
    await call.message.answer(texts.POLL_THANKS_NO)
