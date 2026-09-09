from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import settings
from app.keyboards import (
    ATTENDANCE_ICONS, AdmCB, AdmStudentCB, AdmTrainingCB, AdmWeekCB, AttendCB, NavCB,
    admin_day_kb, admin_menu_kb, admin_training_kb, admin_week_kb, admins_kb, attendance_kb,
    back_to_admin_kb, confirm_cancel_kb, import_confirm_kb, student_card_kb, students_list_kb,
)
from app.models import Admin, Attendance, Booking, Student
from app.services import bookings as bookings_svc
from app.services import enforcement
from app.services import schedule_io
from app.services import stats as stats_svc
from app.services import students as students_svc
from app.services import trainings as trainings_svc
from app.render import render_week, week_title
from app.tz import (
    MONTHS_RU, WEEKDAYS_RU, fmt_dt, fmt_short, now_utc, parse_local, to_local, week_offset_of,
    week_start,
)
from app.utils import edit_view, safe_edit, swap_to_photo, swap_to_text

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event, admin: Admin | None = None) -> bool:
        return admin is not None


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class NewTraining(StatesGroup):
    title = State()
    instructor = State()
    description = State()
    location = State()
    when = State()
    duration = State()
    capacity = State()


class FindStudent(StatesGroup):
    query = State()


class AddAdmin(StatesGroup):
    username = State()


class ImportSchedule(StatesGroup):
    waiting_file = State()
    confirm = State()


MENU_TEXT = "🛠 <b>Админка</b>\n\nВыбирай раздел:"


# ---------- меню ----------

@router.message(Command("admin"))
async def admin_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(MENU_TEXT, reply_markup=admin_menu_kb())


@router.callback_query(NavCB.filter(F.action == "admin"))
async def admin_menu_from_schedule(call: CallbackQuery, state: FSMContext) -> None:
    """Расписание — картинка, поэтому админку открываем отдельным сообщением."""
    await state.clear()
    await call.answer()
    await call.message.answer(MENU_TEXT, reply_markup=admin_menu_kb())


@router.callback_query(AdmCB.filter(F.action == "menu"))
async def admin_menu_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    # экран тренировок — картинка, её в текст не отредактируешь
    await swap_to_text(call.message, MENU_TEXT, admin_menu_kb())


# ---------- тренировки ----------

# Назад админ может уйти дальше студента: посещаемость отмечают задним числом.
MIN_WEEK_OFFSET = -12
MAX_WEEK_OFFSET = 30


async def _week_view(session: AsyncSession, offset: int) -> tuple[bytes, str, object]:
    monday = week_start(offset)
    items = await trainings_svc.for_week(session, monday)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])

    # booked_ids пустой: админу на карточках нужна занятость, а не «ты записан»
    png = render_week(items, taken, set(), monday)

    days = sorted({to_local(t.starts_at).weekday() for t in items})
    has_prev = offset > MIN_WEEK_OFFSET and bool(
        await trainings_svc.for_week(session, week_start(offset - 1))
    )
    has_next = offset < MAX_WEEK_OFFSET and bool(
        await trainings_svc.for_week(session, week_start(offset + 1))
    )

    total = sum(taken.get(t.id, 0) for t in items)
    caption = (
        f"📅 <b>Тренировки</b> · {week_title(monday, monday + timedelta(days=6))}\n"
        f"Занятий: {len(items)}, записей: {total}\n\n"
        "Выбери день 👇"
    )
    if not items:
        caption = (
            f"📅 <b>{week_title(monday, monday + timedelta(days=6))}</b>\n"
            "На эту неделю занятий нет."
        )
    return png, caption, admin_week_kb(offset, days, has_prev, has_next)


async def _show_week(call: CallbackQuery, session: AsyncSession, offset: int) -> None:
    png, caption, kb = await _week_view(session, offset)
    await swap_to_photo(
        call.message,
        BufferedInputFile(png, filename=f"admin-{week_start(offset)}.png"),
        caption,
        kb,
    )


@router.callback_query(AdmCB.filter(F.action == "trainings"))
async def admin_trainings(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    await _show_week(call, session, 0)


@router.callback_query(AdmWeekCB.filter(F.action == "week"))
async def admin_week(call: CallbackQuery, callback_data: AdmWeekCB, session: AsyncSession) -> None:
    await call.answer()
    await _show_week(call, session, callback_data.offset)


@router.callback_query(AdmWeekCB.filter(F.action == "day"))
async def admin_day(call: CallbackQuery, callback_data: AdmWeekCB, session: AsyncSession) -> None:
    offset, weekday = callback_data.offset, callback_data.weekday
    day = week_start(offset) + timedelta(days=weekday)
    items = await trainings_svc.for_day(session, day)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])

    lines = [f"📅 <b>{WEEKDAYS_RU[weekday]}, {day.day} {MONTHS_RU[day.month - 1]}</b>", ""]
    if not items:
        lines.append("В этот день занятий нет.")
    for t in items:
        busy = taken.get(t.id, 0)
        start = to_local(t.starts_at)
        note = "отменена" if t.is_cancelled else f"{busy}/{t.capacity}"
        who = f" · {t.instructor}" if t.instructor else ""
        lines.append(f"<b>{start:%H:%M}</b> {t.title}{who} — {note}")

    pairs = [(t, taken.get(t.id, 0)) for t in items]
    await call.answer()
    await edit_view(call.message, "\n".join(lines), admin_day_kb(offset, weekday, pairs))


@router.callback_query(AdmTrainingCB.filter(F.action == "view"))
async def admin_training_view(
    call: CallbackQuery, callback_data: AdmTrainingCB, session: AsyncSession
) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    if training is None:
        await call.answer("Не найдено", show_alert=True)
        return
    taken = await trainings_svc.taken(session, training.id)
    is_past = trainings_svc.ends_at(training) <= now_utc()
    await call.answer()
    await edit_view(
        call.message,
        trainings_svc.card(training, taken),
        admin_training_kb(training, is_past, callback_data.offset, callback_data.weekday),
    )


@router.callback_query(AdmTrainingCB.filter(F.action == "people"))
async def admin_training_people(
    call: CallbackQuery, callback_data: AdmTrainingCB, session: AsyncSession
) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    people = await trainings_svc.participants(session, callback_data.training_id)
    lines = [f"👥 <b>{training.title}</b> — {fmt_dt(training.starts_at)}", ""]
    if not people:
        lines.append("Пока никто не записался.")
    for i, b in enumerate(people, 1):
        s = b.student
        extra = f" · {s.external_student_id}" if s.external_student_id else ""
        uname = f" · @{s.tg_username}" if s.tg_username else ""
        lines.append(f"{i}. {s.display_name}{uname}{extra}")
    lines.append("")
    lines.append(f"Всего: {len(people)}/{training.capacity}")
    await call.answer()
    await edit_view(
        call.message,
        "\n".join(lines),
        admin_training_kb(
            training,
            trainings_svc.ends_at(training) <= now_utc(),
            callback_data.offset,
            callback_data.weekday,
        ),
    )


@router.callback_query(AdmTrainingCB.filter(F.action == "cancel"))
async def admin_training_cancel_ask(call: CallbackQuery, callback_data: AdmTrainingCB, session: AsyncSession) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    taken = await trainings_svc.taken(session, training.id)
    await call.answer()
    await edit_view(
        call.message,
        f"Отменить тренировку?\n\n{trainings_svc.card(training, taken)}\n\n"
        f"Записанным ({taken}) уйдёт уведомление.",
        confirm_cancel_kb(training.id, callback_data.offset, callback_data.weekday),
    )


@router.callback_query(AdmTrainingCB.filter(F.action == "cancel_confirm"))
async def admin_training_cancel_do(
    call: CallbackQuery, callback_data: AdmTrainingCB, session: AsyncSession, bot: Bot, admin: Admin
) -> None:
    training = await trainings_svc.by_id(session, callback_data.training_id)
    if training is None or training.is_cancelled:
        await call.answer("Уже отменена", show_alert=True)
        return

    affected = await trainings_svc.cancel(session, training)
    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "cancel_training",
        f"training={training.id} affected={len(affected)}",
    )

    sent = 0
    for student in affected:
        try:
            await bot.send_message(
                student.tg_user_id,
                texts.TRAINING_CANCELLED_NOTICE.format(training=trainings_svc.card(training)),
            )
            sent += 1
        except Exception as exc:
            log.info("cannot notify %s: %s", student.tg_user_id, exc)

    await call.answer("Тренировка отменена")
    # возвращаемся к карточке: на ней теперь видно «ОТМЕНЕНА»
    taken = await trainings_svc.taken(session, training.id)
    await edit_view(
        call.message,
        f"❌ Отменено. Уведомлено студентов: {sent}.\n\n{trainings_svc.card(training, taken)}",
        admin_training_kb(training, False, callback_data.offset, callback_data.weekday),
    )


# ---------- посещаемость ----------

CYCLE = {
    Attendance.UNKNOWN: Attendance.ATTENDED,
    Attendance.ATTENDED: Attendance.NO_SHOW,
    Attendance.NO_SHOW: Attendance.EXCUSED,
    Attendance.EXCUSED: Attendance.ATTENDED,
}


async def _attendance_view(
    session: AsyncSession, training_id: int, offset: int = 0, weekday: int = 0
) -> tuple[str, object]:
    training = await trainings_svc.by_id(session, training_id)
    people = await trainings_svc.participants(session, training_id)
    text = (
        f"✅ <b>Посещаемость</b>\n{training.title} — {fmt_dt(training.starts_at)}\n\n"
        "Тыкай по студенту, чтобы переключить отметку:\n"
        "❔ нет отметки → ✅ был → ❌ не пришёл → ➖ уважительно\n\n"
        "❌ добавляет пропуск студенту, остальные отметки — снимают."
    )
    if not people:
        text += "\n\nНикто не записывался."
    return text, attendance_kb(people, training_id, offset, weekday)


@router.callback_query(AdmTrainingCB.filter(F.action == "attend"))
async def admin_attendance(call: CallbackQuery, callback_data: AdmTrainingCB, session: AsyncSession) -> None:
    text, kb = await _attendance_view(
        session, callback_data.training_id, callback_data.offset, callback_data.weekday
    )
    await call.answer()
    await edit_view(call.message, text, kb)


@router.callback_query(AttendCB.filter())
async def admin_attendance_toggle(
    call: CallbackQuery, callback_data: AttendCB, session: AsyncSession, bot: Bot
) -> None:
    booking = await session.get(Booking, callback_data.booking_id)
    if booking is None:
        await call.answer("Запись не найдена", show_alert=True)
        return

    await session.refresh(booking, ["student"])
    new_value = CYCLE[booking.attendance]

    if new_value is Attendance.ATTENDED:
        await enforcement.mark_attended(session, booking)
    elif new_value is Attendance.EXCUSED:
        await enforcement.mark_excused(session, booking)
    else:
        await enforcement.mark_no_show(session, bot, booking, count_strike=True)

    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "set_attendance",
        f"booking={booking.id} value={new_value.value}",
    )

    await call.answer(f"{ATTENDANCE_ICONS[new_value]} {booking.student.display_name}")
    training = await trainings_svc.by_id(session, booking.training_id)
    offset = week_offset_of(training.starts_at)
    weekday = to_local(training.starts_at).weekday()
    text, kb = await _attendance_view(session, booking.training_id, offset, weekday)
    await edit_view(call.message, text, kb)


# ---------- создание тренировки ----------

@router.callback_query(AdmCB.filter(F.action == "new"))
async def new_training_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NewTraining.title)
    await call.answer()
    await call.message.edit_text("➕ <b>Новая тренировка</b>\n\nШаг 1/7. Название?")


@router.message(NewTraining.title)
async def new_training_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(NewTraining.instructor)
    await message.answer("Шаг 2/7. Тренер? (или «-», чтобы пропустить)")


@router.message(NewTraining.instructor)
async def new_training_instructor(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.update_data(instructor="" if text == "-" else text)
    await state.set_state(NewTraining.description)
    await message.answer("Шаг 3/7. Описание? (или «-», чтобы пропустить)")


@router.message(NewTraining.description)
async def new_training_description(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.update_data(description="" if text == "-" else text)
    await state.set_state(NewTraining.location)
    await message.answer("Шаг 4/7. Место? (или «-»)")


@router.message(NewTraining.location)
async def new_training_location(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.update_data(location="" if text == "-" else text)
    await state.set_state(NewTraining.when)
    await message.answer(
        "Шаг 5/7. Дата и время начала в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        f"Часовой пояс: {settings.tz_name}\n\nНапример: <code>15.09.2026 18:30</code>"
    )


@router.message(NewTraining.when)
async def new_training_when(message: Message, state: FSMContext) -> None:
    try:
        starts_at = parse_local(message.text)
    except ValueError:
        await message.answer("Не понял дату. Нужен формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>. Попробуй ещё раз.")
        return
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(NewTraining.duration)
    await message.answer("Шаг 6/7. Длительность в минутах? (например <code>60</code>)")


@router.message(NewTraining.duration)
async def new_training_duration(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Нужно число минут.")
        return
    await state.update_data(duration=int(message.text.strip()))
    await state.set_state(NewTraining.capacity)
    await message.answer(
        f"Шаг 7/7. Сколько мест? (отправь «-», чтобы взять значение по умолчанию — {settings.default_capacity})"
    )


@router.message(NewTraining.capacity)
async def new_training_capacity(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = message.text.strip()
    if raw == "-":
        capacity = settings.default_capacity
    elif raw.isdigit() and int(raw) > 0:
        capacity = int(raw)
    else:
        await message.answer("Нужно положительное число или «-».")
        return

    data = await state.get_data()
    training = await trainings_svc.create(
        session,
        title=data["title"],
        description=data["description"],
        instructor=data["instructor"],
        location=data["location"],
        starts_at=datetime.fromisoformat(data["starts_at"]),
        duration_min=data["duration"],
        capacity=capacity,
    )
    await state.clear()
    await students_svc.log_action(
        session, message.from_user.id, message.from_user.username, "create_training", f"training={training.id}"
    )
    await message.answer(
        f"✅ Тренировка создана:\n\n{trainings_svc.card(training, 0)}", reply_markup=back_to_admin_kb()
    )


# ---------- студенты ----------

@router.callback_query(AdmCB.filter(F.action == "students"))
async def find_student_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FindStudent.query)
    await call.answer()
    await call.message.edit_text(
        "🔎 Пришли часть имени, @username, email или student id — найду студента."
    )


@router.message(FindStudent.query)
async def find_student_run(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    found = await students_svc.search(session, message.text)
    if not found:
        await message.answer("Никого не нашёл.", reply_markup=back_to_admin_kb())
        return
    await message.answer(f"Нашёл: {len(found)}", reply_markup=students_list_kb(found))


@router.callback_query(AdmCB.filter(F.action == "banned"))
async def banned_students(call: CallbackQuery, session: AsyncSession) -> None:
    found = await students_svc.banned_list(session)
    await call.answer()
    if not found:
        await call.message.edit_text("🚫 Заблокированных нет.", reply_markup=back_to_admin_kb())
        return
    await call.message.edit_text(f"🚫 Заблокированных: {len(found)}", reply_markup=students_list_kb(found))


def _student_card(student: Student) -> str:
    lines = [
        f"👤 <b>{student.display_name}</b>",
        f"telegram: @{student.tg_username}" if student.tg_username else f"telegram id: {student.tg_user_id}",
    ]
    if student.external_student_id:
        lines.append(f"student id: <code>{student.external_student_id}</code>")
    if student.email:
        lines.append(f"email: {student.email}")
    lines.append(f"справка/ТБ: {'✅' if student.consent_accepted_at else '—'}")
    lines.append(f"пропусков без отмены: {student.no_show_count} из {settings.no_show_limit}")
    if student.is_banned:
        lines.append(f"🚫 <b>заблокирован</b>: {student.ban_reason or 'без причины'}")
    return "\n".join(lines)


async def _show_student(call: CallbackQuery, session: AsyncSession, student_id: int) -> None:
    student = await students_svc.by_id(session, student_id)
    if student is None:
        await call.answer("Не найден", show_alert=True)
        return

    text = _student_card(student)
    active = await bookings_svc.active_for_student(session, student.id)
    if active:
        text += "\n\n<b>Записан на:</b>\n" + "\n".join(
            f"• {fmt_short(b.training.starts_at)} — {b.training.title}" for b in active
        )
    await safe_edit(call.message, text, student_card_kb(student))


@router.callback_query(AdmStudentCB.filter(F.action == "view"))
async def student_view(call: CallbackQuery, callback_data: AdmStudentCB, session: AsyncSession) -> None:
    await call.answer()
    await _show_student(call, session, callback_data.student_id)


@router.callback_query(AdmStudentCB.filter(F.action == "ban"))
async def student_ban(
    call: CallbackQuery, callback_data: AdmStudentCB, session: AsyncSession, bot: Bot
) -> None:
    student = await students_svc.by_id(session, callback_data.student_id)
    await students_svc.set_ban(session, student, True, "решение администратора")
    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "ban", f"student={student.id}"
    )
    try:
        await bot.send_message(student.tg_user_id, texts.BANNED.format(reason="решение администратора"))
    except Exception as exc:
        log.info("cannot notify %s: %s", student.tg_user_id, exc)
    await call.answer("Заблокирован")
    await _show_student(call, session, student.id)


@router.callback_query(AdmStudentCB.filter(F.action == "unban"))
async def student_unban(
    call: CallbackQuery, callback_data: AdmStudentCB, session: AsyncSession, bot: Bot
) -> None:
    student = await students_svc.by_id(session, callback_data.student_id)
    await students_svc.set_ban(session, student, False)
    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "unban", f"student={student.id}"
    )
    try:
        await bot.send_message(student.tg_user_id, "♻️ Доступ к записи на тренировки восстановлен.")
    except Exception as exc:
        log.info("cannot notify %s: %s", student.tg_user_id, exc)
    await call.answer("Разблокирован")
    await _show_student(call, session, student.id)


@router.callback_query(AdmStudentCB.filter(F.action == "reset"))
async def student_reset(call: CallbackQuery, callback_data: AdmStudentCB, session: AsyncSession) -> None:
    student = await students_svc.by_id(session, callback_data.student_id)
    await students_svc.reset_strikes(session, student)
    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "reset_strikes", f"student={student.id}"
    )
    await call.answer("Пропуски сброшены")
    await _show_student(call, session, student.id)


# ---------- статистика и выгрузка ----------

@router.callback_query(AdmCB.filter(F.action == "stats"))
async def show_stats(call: CallbackQuery, session: AsyncSession) -> None:
    o = await stats_svc.overview(session)
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"Студентов в боте: {o['total_students']} (авторизовано {o['authorized']})",
        f"Заблокировано: {o['banned']}",
        "",
        f"Тренировок всего: {o['total_trainings']} (будущих {o['upcoming']})",
        f"Активных записей: {o['active_bookings']}",
        f"Отмечено посещений: {o['attended']}",
        f"Пропусков без отмены: {o['no_shows']}",
    ]

    per = await stats_svc.per_training(session, limit=10)
    if per:
        lines += ["", "<b>Последние тренировки</b> (записалось / пришло / пропустили)"]
        for t, booked, att, no_show in per:
            lines.append(f"• {fmt_short(t.starts_at)} {t.title}: {booked} / {att} / {no_show}")

    await call.answer()
    await call.message.edit_text("\n".join(lines), reply_markup=back_to_admin_kb())


@router.callback_query(AdmCB.filter(F.action == "export"))
async def export(call: CallbackQuery, session: AsyncSession) -> None:
    payload = await stats_svc.export_csv(session)
    await call.answer("Готовлю файл…")
    await call.message.answer_document(
        BufferedInputFile(payload, filename="bookings.csv"),
        caption="Все записи: тренировка, студент, статус, посещаемость.",
    )


# ---------- расписание одним файлом ----------

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@router.callback_query(AdmCB.filter(F.action == "export_schedule"))
async def export_schedule(call: CallbackQuery, session: AsyncSession) -> None:
    payload = await schedule_io.export_workbook(session)
    await call.answer("Собираю файл…")
    await call.message.answer_document(
        BufferedInputFile(payload, filename="raspisanie.xlsx"),
        caption=(
            "🗓 <b>Расписание на неделю</b>\n\n"
            "В файле — период действия и занятия на одну неделю. "
            "Поправь и загрузи обратно кнопкой «📥 Загрузить расписание» — "
            "бот сам разложит их по всем неделям периода."
        ),
        reply_markup=back_to_admin_kb(),
    )


@router.callback_query(AdmCB.filter(F.action == "import_schedule"))
async def import_schedule_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImportSchedule.waiting_file)
    await call.answer()
    await swap_to_text(
        call.message,
        "📥 <b>Загрузка расписания</b>\n\n"
        "Пришли .xlsx-файл в том же виде, что отдаёт «🗓 Выгрузить расписание»:\n"
        "• B2 — начало периода, B3 — конец периода\n"
        "• с 6-й строки — занятия на <b>одну</b> неделю\n\n"
        "Занятия внутри периода будут заменены на новые. "
        "Прошедшие останутся — на них держится посещаемость.\n\n"
        "Покажу, что получится, до того как что-то менять.",
        back_to_admin_kb(),
    )


@router.message(ImportSchedule.waiting_file, F.document)
async def import_schedule_file(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    document = message.document
    if not (document.file_name or "").lower().endswith((".xlsx", ".xlsm")):
        await message.answer("Нужен файл .xlsx — тот, что отдала кнопка выгрузки.")
        return
    if (document.file_size or 0) > MAX_UPLOAD_BYTES:
        await message.answer("Файл слишком большой. Ожидаю обычную таблицу до 2 МБ.")
        return

    buf = io.BytesIO()
    await bot.download(document, destination=buf)
    parsed = schedule_io.parse_workbook(buf.getvalue())

    if not parsed.ok:
        problems = "\n".join(f"• {e}" for e in parsed.errors[:12])
        more = f"\n…и ещё {len(parsed.errors) - 12}" if len(parsed.errors) > 12 else ""
        await message.answer(
            f"❌ <b>Файл не подошёл</b>\n\n{problems}{more}\n\nПоправь и пришли ещё раз.",
            reply_markup=back_to_admin_kb(),
        )
        return

    result = await schedule_io.preview(session, parsed)
    await state.set_state(ImportSchedule.confirm)
    await state.update_data(parsed=parsed)

    by_day: dict[int, list] = {}
    for slot in parsed.slots:
        by_day.setdefault(slot.weekday, []).append(slot)

    lines = [
        "📥 <b>Проверь, что получится</b>",
        "",
        f"Период: <b>{parsed.starts_on:%d.%m.%Y} — {parsed.ends_on:%d.%m.%Y}</b>",
        f"Занятий в неделю: {len(parsed.slots)}",
        "",
    ]
    for weekday in sorted(by_day):
        items = ", ".join(f"{s.at:%H:%M} {s.title}" for s in sorted(by_day[weekday], key=lambda s: s.at))
        lines.append(f"<b>{WEEKDAYS_RU[weekday]}</b>: {items}")

    lines += [
        "",
        f"Будет создано занятий: <b>{result.created}</b>",
        f"Будет удалено будущих: <b>{result.removed}</b>",
    ]
    if result.kept_past:
        lines.append(f"Прошедших внутри периода не тронем: {result.kept_past}")
    if result.notify:
        lines.append(f"⚠️ Записей у студентов слетит: <b>{len(result.notify)}</b> — их предупредим.")
    await message.answer("\n".join(lines), reply_markup=import_confirm_kb())


@router.message(ImportSchedule.waiting_file)
async def import_schedule_wrong_input(message: Message) -> None:
    await message.answer(
        "Жду .xlsx-файл. Если передумал — нажми «⬅️ В админку».",
        reply_markup=back_to_admin_kb(),
    )


@router.callback_query(AdmCB.filter(F.action == "import_apply"), ImportSchedule.confirm)
async def import_schedule_apply(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    data = await state.get_data()
    parsed = data.get("parsed")
    await state.clear()
    if parsed is None:
        await call.answer("Файл потерялся, пришли его заново", show_alert=True)
        return

    await call.answer("Применяю…")
    result = await schedule_io.apply_schedule(session, parsed)
    await students_svc.log_action(
        session, call.from_user.id, call.from_user.username, "import_schedule",
        f"{parsed.starts_on}..{parsed.ends_on} created={result.created} removed={result.removed}",
    )

    notified = 0
    for tg_user_id, what in result.notify:
        try:
            await bot.send_message(
                tg_user_id,
                "🗓 Расписание изменилось, твоя запись отменена:\n\n"
                f"{what}\n\nЗагляни в расписание и запишись заново.",
            )
            notified += 1
        except Exception as exc:
            log.info("cannot notify %s: %s", tg_user_id, exc)

    lines = [
        "✅ <b>Расписание обновлено</b>",
        "",
        f"Период: {parsed.starts_on:%d.%m.%Y} — {parsed.ends_on:%d.%m.%Y}",
        f"Создано занятий: {result.created}",
        f"Удалено старых: {result.removed}",
    ]
    if result.notify:
        lines.append(f"Предупреждено студентов: {notified} из {len(result.notify)}")
    await swap_to_text(call.message, "\n".join(lines), back_to_admin_kb())


# ---------- админы ----------

@router.callback_query(AdmCB.filter(F.action == "admins"))
async def admins_list(call: CallbackQuery, session: AsyncSession, admin: Admin) -> None:
    items = await students_svc.list_admins(session)
    await call.answer()
    await call.message.edit_text(
        "🛡 <b>Админы</b>\n\n👑 — суперадмин из .env (снять нельзя).\n"
        "Новый админ добавляется по @username: права появятся, когда он напишет боту.",
        reply_markup=admins_kb(items, can_edit=admin.is_superadmin),
    )


@router.callback_query(AdmCB.filter(F.action == "add_admin"))
async def add_admin_start(call: CallbackQuery, state: FSMContext, admin: Admin) -> None:
    if not admin.is_superadmin:
        await call.answer("Только суперадмин может добавлять админов", show_alert=True)
        return
    await state.set_state(AddAdmin.username)
    await call.answer()
    await call.message.edit_text("Пришли @username нового админа.")


@router.message(AddAdmin.username)
async def add_admin_run(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    created = await students_svc.add_admin(session, message.text)
    if created is None:
        await message.answer("Пустой username.", reply_markup=back_to_admin_kb())
        return
    await students_svc.log_action(
        session, message.from_user.id, message.from_user.username, "add_admin", f"@{created.tg_username}"
    )
    await message.answer(f"✅ @{created.tg_username} теперь админ.", reply_markup=back_to_admin_kb())


@router.callback_query(AdmCB.filter(F.action == "del_admin"))
async def del_admin(call: CallbackQuery, callback_data: AdmCB, session: AsyncSession, admin: Admin) -> None:
    if not admin.is_superadmin:
        await call.answer("Только суперадмин может снимать админов", show_alert=True)
        return
    ok = await students_svc.remove_admin(session, callback_data.obj_id)
    await call.answer("Снят" if ok else "Нельзя снять суперадмина", show_alert=not ok)
    items = await students_svc.list_admins(session)
    await call.message.edit_text(
        "🛡 <b>Админы</b>", reply_markup=admins_kb(items, can_edit=admin.is_superadmin)
    )
