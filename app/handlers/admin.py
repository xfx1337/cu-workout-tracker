from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import reactions as R
from app import texts
from app.config import settings
from app.models import Admin, Attendance, Booking, Student, Training
from app.render import render_week, week_title
from app.runtime import BotContext, UserSession
from app.services import bookings as bookings_svc
from app.services import enforcement
from app.services import schedule_io
from app.services import stats as stats_svc
from app.services import students as students_svc
from app.services import trainings as trainings_svc
from app.tz import (
    MONTHS_RU, WEEKDAYS_RU, fmt_dt, fmt_short, now_utc, parse_local, to_local,
    week_offset_of, week_start,
)

log = logging.getLogger(__name__)

MIN_WEEK_OFFSET = -12
MAX_WEEK_OFFSET = 30
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

ATTENDANCE_ICONS = {
    Attendance.ATTENDED: "✅",
    Attendance.NO_SHOW: "❌",
    Attendance.EXCUSED: "➖",
    Attendance.UNKNOWN: "❔",
}
CYCLE = {
    Attendance.UNKNOWN: Attendance.ATTENDED,
    Attendance.ATTENDED: Attendance.NO_SHOW,
    Attendance.NO_SHOW: Attendance.EXCUSED,
    Attendance.EXCUSED: Attendance.ATTENDED,
}

MENU_TEXT = "🛠 <b>Админка</b>\n\nВыбирай раздел реакцией 👇"

# пункты меню: (ключ, подпись) — нумеруются для реакций
MENU_ITEMS = [
    ("trainings", "Тренировки"),
    ("new", "Новая тренировка"),
    ("export_schedule", "Выгрузить расписание (xlsx)"),
    ("import_schedule", "Загрузить расписание (xlsx)"),
    ("students", "Найти студента"),
    ("banned", "Заблокированные"),
    ("stats", "Статистика"),
    ("export", "Записи в CSV"),
    ("admins", "Админы"),
]


async def _menu_reactions() -> dict[str, str]:
    reactions: dict[str, str] = {}
    for i, (key, _label) in enumerate(MENU_ITEMS, 1):
        name, _ = R.number(i)
        reactions[name] = f"adm:{key}"
    reactions["arrow_left"] = "nav:schedule"
    return reactions


async def admin_menu(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin) -> None:
    lines = [MENU_TEXT, ""]
    for i, (_key, label) in enumerate(MENU_ITEMS, 1):
        name, sym = R.number(i)
        lines.append(f"{sym} {label}")
    lines.append(f"{R.symbol('arrow_left')} К расписанию")
    await ctx.send(
        s.user_id, "\n".join(lines), reactions=await _menu_reactions(),
        data={"screen": "admin_menu"},
    )


# ---------- тренировки: картинка недели ----------

async def _week_view(session: AsyncSession, offset: int) -> tuple[bytes, str]:
    monday = week_start(offset)
    items = await trainings_svc.for_week(session, monday)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
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
        "Выбери день реакцией 👇"
    )
    if not items:
        caption = (
            f"📅 <b>{week_title(monday, monday + timedelta(days=6))}</b>\n"
            "На эту неделю занятий нет."
        )

    reactions: dict[str, str] = {}
    legend: list[tuple[int, str]] = []
    for i, weekday in enumerate(days, 1):
        name, _ = R.number(i)
        reactions[name] = f"awk:day:{weekday}"
        legend.append((i, WEEKDAYS_RU[weekday]))
    if has_prev:
        reactions["arrow_left"] = f"awk:week:{offset - 1}"
    if has_next:
        reactions["arrow_right"] = f"awk:week:{offset + 1}"
    reactions["no_entry"] = "adm:menu"

    if legend:
        caption += "\n\n" + _legend(legend)
    if has_prev or has_next:
        caption += "\n← / → — соседние недели"
    return png, caption, reactions


def _legend(pairs: list[tuple[int, str]]) -> str:
    return "  ".join(f"{R.symbol(R.number(i)[0])} {label}" for i, label in pairs)


async def show_admin_week(ctx: BotContext, session: AsyncSession, s: UserSession, offset: int = 0) -> None:
    monday = week_start(offset)
    png, caption, reactions = await _week_view(session, offset)
    await ctx.send(
        s.user_id, caption,
        file_bytes=png, filename=f"admin-{monday}.png",
        reactions=reactions,
        data={"screen": "admin_week", "offset": offset},
    )


async def show_admin_day(ctx: BotContext, session: AsyncSession, s: UserSession, offset: int, weekday: int) -> None:
    day = week_start(offset) + timedelta(days=weekday)
    items = await trainings_svc.for_day(session, day)
    taken = await trainings_svc.taken_map(session, [t.id for t in items])
    lines = [f"<b>{WEEKDAYS_RU[weekday]}, {day.day} {MONTHS_RU[day.month - 1]}</b>", ""]
    if not items:
        lines.append("В этот день занятий нет.")
    reactions: dict[str, str] = {}
    legend: list[tuple[int, str]] = []
    for i, t in enumerate(items, 1):
        busy = taken.get(t.id, 0)
        start = to_local(t.starts_at)
        note = "отменена" if t.is_cancelled else f"{busy}/{t.capacity}"
        who = f" · {t.instructor}" if t.instructor else ""
        lines.append(f"<b>{start:%H:%M}</b> {t.title}{who} — {note}")
        name, _ = R.number(i)
        reactions[name] = f"atr:view:{t.id}"
        legend.append((i, f"{start:%H:%M} {t.title}"))
    lines += ["", "Выбери тренировку реакцией 👇"]
    if legend:
        lines.append(_legend(legend))
    reactions["arrow_left"] = f"awk:week:{offset}"
    reactions["no_entry"] = "adm:menu"

    await ctx.send(
        s.user_id, "\n".join(lines), reactions=reactions,
        data={"screen": "admin_day", "offset": offset, "weekday": weekday},
    )


async def show_admin_training(ctx: BotContext, session: AsyncSession, s: UserSession, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    if training is None:
        await ctx.notify(s.user_id, "Не найдено.")
        return
    taken = await trainings_svc.taken(session, training.id)
    is_past = trainings_svc.ends_at(training) <= now_utc()
    offset = s.data.get("offset", 0)
    weekday = s.data.get("weekday", 0)

    reactions: dict[str, str] = {
        "busts_in_silhouette": f"atr:people:{training.id}",
    }
    if is_past:
        reactions["white_check_mark"] = f"atr:attend:{training.id}"
    if not training.is_cancelled and not is_past:
        reactions["x"] = f"atr:cancel:{training.id}"
    reactions["arrow_left"] = f"awk:day:{weekday}"
    reactions["no_entry"] = "adm:menu"

    await ctx.send(
        s.user_id, trainings_svc.card(training, taken), reactions=reactions,
        data={"screen": "admin_training", "offset": offset, "weekday": weekday, "training_id": training.id},
    )


async def show_people(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    people = await trainings_svc.participants(session, training_id)
    lines = [f"👥 <b>{training.title}</b> — {fmt_dt(training.starts_at)}", ""]
    if not people:
        lines.append("Пока никто не записался.")
    for i, b in enumerate(people, 1):
        st = b.student
        extra = f" · {st.external_student_id}" if getattr(st, "external_student_id", None) else ""
        lines.append(f"{i}. {st.display_name}{extra}")
    lines.append("")
    lines.append(f"Всего: {len(people)}/{training.capacity}")
    offset = s.data.get("offset", 0)
    weekday = s.data.get("weekday", 0)
    await ctx.send(
        s.user_id, "\n".join(lines),
        reactions={"arrow_left": f"atr:view:{training_id}", "no_entry": "adm:menu"},
        data={"screen": "admin_training", "offset": offset, "weekday": weekday, "training_id": training.id},
    )


async def cancel_training_ask(ctx: BotContext, session: AsyncSession, s: UserSession, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    taken = await trainings_svc.taken(session, training.id)
    await ctx.send(
        s.user_id,
        f"Отменить тренировку?\n\n{trainings_svc.card(training, taken)}\n\n"
        f"Записанным ({taken}) уйдёт уведомление.\nНажми 🔥 для подтверждения.",
        reactions={"fire": f"atr:cancel_confirm:{training.id}",
                   "arrow_left": f"atr:view:{training.id}",
                   "no_entry": "adm:menu"},
        data={"screen": "admin_training", "offset": s.data.get("offset", 0),
              "weekday": s.data.get("weekday", 0), "training_id": training.id},
    )


async def cancel_training_do(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    if training is None or training.is_cancelled:
        await ctx.notify(s.user_id, "Уже отменена.")
        return

    affected = await trainings_svc.cancel(session, training)
    await students_svc.log_action(
        session, admin.email, admin.mm_username, "cancel_training",
        f"training={training.id} affected={len(affected)}",
    )
    sent = 0
    for student in affected:
        try:
            await ctx.notify(student.mm_user_id, texts.TRAINING_CANCELLED_NOTICE.format(
                training=trainings_svc.card(training)
            ))
            sent += 1
        except Exception as exc:
            log.info("cannot notify %s: %s", student.mm_user_id, exc)

    taken = await trainings_svc.taken(session, training.id)
    await ctx.notify(s.user_id, f"❌ Отменено. Уведомлено студентов: {sent}.")
    await show_admin_training(ctx, session, s, training.id)


# ---------- посещаемость ----------

async def show_attendance(ctx: BotContext, session: AsyncSession, s: UserSession, training_id: int) -> None:
    training = await trainings_svc.by_id(session, training_id)
    people = await trainings_svc.participants(session, training_id)
    text = (
        f"✅ <b>Посещаемость</b>\n{training.title} — {fmt_dt(training.starts_at)}\n\n"
        "Тыкай по студенту реакцией, чтобы переключить отметку:\n"
        "❔ нет → ✅ был → ❌ не пришёл → ➖ уважительно\n\n"
        "❌ добавляет пропуск, остальные отметки — снимают."
    )
    if not people:
        text += "\n\nНикто не записывался."
    reactions: dict[str, str] = {}
    legend: list[tuple[int, str]] = []
    for i, b in enumerate(people, 1):
        name, _ = R.number(i)
        reactions[name] = f"att:toggle:{b.id}"
        legend.append((i, f"{ATTENDANCE_ICONS[b.attendance]} {b.student.display_name}"))
    if legend:
        text += "\n\n" + _legend(legend)
    reactions["arrow_left"] = f"atr:view:{training_id}"
    reactions["no_entry"] = "adm:menu"
    offset = s.data.get("offset", 0)
    weekday = s.data.get("weekday", 0)
    await ctx.send(
        s.user_id, text, reactions=reactions,
        data={"screen": "attendance", "offset": offset, "weekday": weekday, "training_id": training_id},
    )


async def attendance_toggle(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, booking_id: int) -> None:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        await ctx.notify(s.user_id, "Запись не найдена.")
        return
    await session.refresh(booking, ["student"])
    new_value = CYCLE[booking.attendance]
    if new_value is Attendance.ATTENDED:
        await enforcement.mark_attended(session, booking)
    elif new_value is Attendance.EXCUSED:
        await enforcement.mark_excused(session, booking)
    else:
        await enforcement.mark_no_show(session, ctx, booking, count_strike=True)
    await students_svc.log_action(
        session, admin.email, admin.mm_username, "set_attendance",
        f"booking={booking.id} value={new_value.value}",
    )
    training_id = s.data.get("training_id", booking.training_id)
    await show_attendance(ctx, session, s, training_id)


# ---------- создание тренировки (FSM) ----------

async def new_training_start(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    await ctx.send(
        s.user_id, "➕ <b>Новая тренировка</b>\n\nШаг 1/7. Название?",
        reactions={"x": "fsm:cancel"}, fsm="new_training.title", fsm_data={},
        data={"screen": "admin_new_training"},
    )


_NEW_PROMPTS = {
    "new_training.instructor": "Шаг 2/7. Тренер? (или «-», чтобы пропустить)",
    "new_training.description": "Шаг 3/7. Описание? (или «-», чтобы пропустить)",
    "new_training.location": "Шаг 4/7. Место? (или «-»)",
    "new_training.when": (
        "Шаг 5/7. Дата и время начала в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        f"Часовой пояс: {settings.tz_name}\n\nНапример: <code>15.09.2026 18:30</code>"
    ),
    "new_training.duration": "Шаг 6/7. Длительность в минутах? (например <code>60</code>)",
    "new_training.capacity": (
        f"Шаг 7/7. Сколько мест? (отправь «-», чтобы взять по умолчанию — {settings.default_capacity})"
    ),
}


async def _new_training_step(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, step: str, text: str) -> None:
    value = text.strip()
    if step == "new_training.title":
        if not value:
            await ctx.notify(s.user_id, "Название не может быть пустым. Введи ещё раз.")
            return
        s.fsm_data["title"] = value
        await _advance_new_training(ctx, session, s, admin, "new_training.instructor")
        return
    if step == "new_training.instructor":
        s.fsm_data["instructor"] = "" if value == "-" else value
        await _advance_new_training(ctx, session, s, admin, "new_training.description")
        return
    if step == "new_training.description":
        s.fsm_data["description"] = "" if value == "-" else value
        await _advance_new_training(ctx, session, s, admin, "new_training.location")
        return
    if step == "new_training.location":
        s.fsm_data["location"] = "" if value == "-" else value
        await _advance_new_training(ctx, session, s, admin, "new_training.when")
        return
    if step == "new_training.when":
        try:
            starts_at = parse_local(value)
        except ValueError:
            await ctx.notify(s.user_id, "Не понял дату. Нужен формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.")
            return
        s.fsm_data["starts_at"] = starts_at.isoformat()
        await _advance_new_training(ctx, session, s, admin, "new_training.duration")
        return
    if step == "new_training.duration":
        if not value.isdigit():
            await ctx.notify(s.user_id, "Нужно число минут.")
            return
        s.fsm_data["duration"] = int(value)
        await _advance_new_training(ctx, session, s, admin, "new_training.capacity")
        return
    if step == "new_training.capacity":
        if value == "-":
            capacity = settings.default_capacity
        elif value.isdigit() and int(value) > 0:
            capacity = int(value)
        else:
            await ctx.notify(s.user_id, "Нужно положительное число или «-».")
            return
        data = s.fsm_data
        training = await trainings_svc.create(
            session,
            title=data["title"],
            description=data.get("description", ""),
            instructor=data.get("instructor", ""),
            location=data.get("location", ""),
            starts_at=datetime.fromisoformat(data["starts_at"]),
            duration_min=data["duration"],
            capacity=capacity,
        )
        await students_svc.log_action(
            session, admin.email, admin.mm_username, "create_training", f"training={training.id}"
        )
        s.fsm = ""
        s.fsm_data = {}
        await ctx.notify(s.user_id, f"✅ Тренировка создана:\n\n{trainings_svc.card(training, 0)}")
        await admin_menu(ctx, session, s, None, admin)
        return


async def _advance_new_training(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, step: str) -> None:
    await ctx.send(
        s.user_id, _NEW_PROMPTS[step],
        reactions={"x": "fsm:cancel"}, fsm=step,
        data=s.data,
    )


# ---------- студенты ----------

async def find_student_start(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    await ctx.send(
        s.user_id, "🔎 Пришли часть имени, @username или email — найду студента.",
        reactions={"x": "fsm:cancel"}, fsm="find_student",
    )


async def _find_student(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, text: str) -> None:
    s.fsm = ""
    s.fsm_data = {}
    found = await students_svc.search(session, text)
    await _show_students_list(ctx, session, s, admin, found, header="Нашёл: {len}")


async def banned_students(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    found = await students_svc.banned_list(session)
    await _show_students_list(ctx, session, s, admin, found, header=f"🚫 Заблокированных: {len(found)}" if found else "🚫 Заблокированных нет.")


async def _show_students_list(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, students: list[Student], header: str) -> None:
    if not students:
        await ctx.send(
            s.user_id, header,
            reactions={"arrow_left": "adm:menu"},
            data={"screen": "admin_menu"},
        )
        return
    lines = [header, ""]
    reactions: dict[str, str] = {}
    legend: list[tuple[int, str]] = []
    for i, st in enumerate(students[:9], 1):
        mark = "🚫" if st.is_banned else "👤"
        lines.append(f"{mark} {st.display_name} · пропусков {st.no_show_count}")
        name, _ = R.number(i)
        reactions[name] = f"ast:view:{st.id}"
        legend.append((i, st.display_name))
    lines += ["", "Выбери студента реакцией 👇"]
    lines.append(_legend(legend))
    reactions["arrow_left"] = "adm:menu"
    await ctx.send(
        s.user_id, "\n".join(lines), reactions=reactions,
        data={"screen": "admin_students"},
    )


async def show_student(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, student_id: int) -> None:
    student = await students_svc.by_id(session, student_id)
    if student is None:
        await ctx.notify(s.user_id, "Студент не найден.")
        return
    lines = [
        f"👤 <b>{student.display_name}</b>",
        f"пользователь: @{student.mm_username}" if student.mm_username else f"mm id: {student.mm_user_id}",
    ]
    if student.email:
        lines.append(f"email: {student.email}")
    lines.append(f"справка/ТБ: {'✅' if student.consent_accepted_at else '—'}")
    lines.append(f"пропусков без отмены: {student.no_show_count} из {settings.no_show_limit}")
    if student.is_banned:
        lines.append(f"🚫 <b>заблокирован</b>: {student.ban_reason or 'без причины'}")

    active = await bookings_svc.active_for_student(session, student.id)
    if active:
        lines.append("\n<b>Записан на:</b>")
        lines += [f"• {fmt_short(b.training.starts_at)} — {b.training.title}" for b in active]

    reactions: dict[str, str] = {}
    if student.is_banned:
        reactions["white_check_mark"] = f"ast:unban:{student.id}"
    else:
        reactions["no_entry"] = f"ast:ban:{student.id}"
    if student.no_show_count:
        reactions["memo"] = f"ast:reset:{student.id}"
    reactions["arrow_left"] = "adm:menu"

    await ctx.send(
        s.user_id, "\n".join(lines), reactions=reactions,
        data={"screen": "admin_student", "student_id": student.id},
    )


async def student_ban(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, student_id: int) -> None:
    student = await students_svc.by_id(session, student_id)
    await students_svc.set_ban(session, student, True, "решение администратора")
    await students_svc.log_action(session, admin.email, admin.mm_username, "ban", f"student={student.id}")
    try:
        await ctx.notify(student.mm_user_id, texts.BANNED.format(reason="решение администратора"))
    except Exception as exc:
        log.info("cannot notify %s: %s", student.mm_user_id, exc)
    await show_student(ctx, session, s, admin, student.id)


async def student_unban(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, student_id: int) -> None:
    student = await students_svc.by_id(session, student_id)
    await students_svc.set_ban(session, student, False)
    await students_svc.log_action(session, admin.email, admin.mm_username, "unban", f"student={student.id}")
    try:
        await ctx.notify(student.mm_user_id, "♻️ Доступ к записи на тренировки восстановлен.")
    except Exception as exc:
        log.info("cannot notify %s: %s", student.mm_user_id, exc)
    await show_student(ctx, session, s, admin, student.id)


async def student_reset(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, student_id: int) -> None:
    student = await students_svc.by_id(session, student_id)
    await students_svc.reset_strikes(session, student)
    await students_svc.log_action(session, admin.email, admin.mm_username, "reset_strikes", f"student={student.id}")
    await show_student(ctx, session, s, admin, student.id)


# ---------- статистика и выгрузка ----------

async def show_stats(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    o = await stats_svc.overview(session)
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"Студентов в боте: {o['total_students']} (с почтой {o['with_email']})",
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
    await ctx.send(
        s.user_id, "\n".join(lines),
        reactions={"arrow_left": "adm:menu"},
        data={"screen": "admin_stats"},
    )


async def export_csv(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    payload = await stats_svc.export_csv(session)
    channel = await ctx.dm(s.user_id)
    file_id = await ctx.mm.upload_file(channel, "bookings.csv", payload)
    await ctx.mm.create_post(channel, "Все записи: тренировка, студент, статус, посещаемость.", file_ids=[file_id])
    await ctx.send(
        s.user_id, "📤 Файл отправлен выше.",
        reactions={"arrow_left": "adm:menu"},
        data={"screen": "admin_stats"},
    )


async def export_schedule(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    payload = await schedule_io.export_workbook(session)
    channel = await ctx.dm(s.user_id)
    file_id = await ctx.mm.upload_file(channel, "raspisanie.xlsx", payload)
    await ctx.mm.create_post(
        channel,
        "🗓 <b>Расписание на неделю</b>\n\n"
        "В файле — период действия и занятия на одну неделю. "
        "Поправь и загрузи обратно через «📥 Загрузить расписание».",
        file_ids=[file_id],
    )
    await ctx.send(
        s.user_id, "🗓 Файл отправлен выше.",
        reactions={"arrow_left": "adm:menu"},
        data={"screen": "admin_stats"},
    )


async def import_schedule_start(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    await ctx.send(
        s.user_id,
        "📥 <b>Загрузка расписания</b>\n\n"
        "Пришли .xlsx-файл в том же виде, что отдаёт «🗓 Выгрузить расписание».\n"
        "Покажу, что получится, до того как что-то менять.",
        reactions={"x": "fsm:cancel"}, fsm="import.waiting_file",
        data={"screen": "admin_import"},
    )


async def _import_file(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, file_ids: list[str], text: str) -> None:
    if not file_ids:
        await ctx.notify(s.user_id, "Жду .xlsx-файл (не текст).")
        return
    try:
        data = await ctx.download(file_ids[0])
    except Exception as exc:
        log.info("не смог скачать файл: %s", exc)
        await ctx.notify(s.user_id, "Не смог скачать файл, попробуй ещё раз.")
        return
    if len(data) > MAX_UPLOAD_BYTES:
        await ctx.notify(s.user_id, "Файл слишком большой. Ожидаю таблицу до 2 МБ.")
        return

    parsed = schedule_io.parse_workbook(data)
    if not parsed.ok:
        problems = "\n".join(f"• {e}" for e in parsed.errors[:12])
        more = f"\n…и ещё {len(parsed.errors) - 12}" if len(parsed.errors) > 12 else ""
        await ctx.notify(s.user_id, f"❌ <b>Файл не подошёл</b>\n\n{problems}{more}")
        return

    result = await schedule_io.preview(session, parsed)
    s.fsm = "import.confirm"
    s.fsm_data["parsed"] = parsed

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
        items = ", ".join(f"{slot.at:%H:%M} {slot.title}" for slot in sorted(by_day[weekday], key=lambda x: x.at))
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
    await ctx.send(
        s.user_id, "\n".join(lines),
        reactions={"fire": "adm:import_apply", "x": "fsm:cancel"},
        fsm="import.confirm",
        data={"screen": "admin_import"},
    )


async def import_schedule_apply(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    parsed = s.fsm_data.get("parsed")
    s.fsm = ""
    s.fsm_data = {}
    if parsed is None:
        await ctx.notify(s.user_id, "Файл потерялся, пришли его заново.")
        return
    result = await schedule_io.apply_schedule(session, parsed)
    await students_svc.log_action(
        session, admin.email, admin.mm_username, "import_schedule",
        f"{parsed.starts_on}..{parsed.ends_on} created={result.created} removed={result.removed}",
    )
    notified = 0
    for mm_user_id, what in result.notify:
        try:
            await ctx.notify(
                mm_user_id,
                "🗓 Расписание изменилось, твоя запись отменена:\n\n"
                f"{what}\n\nЗагляни в расписание и запишись заново.",
            )
            notified += 1
        except Exception as exc:
            log.info("cannot notify %s: %s", mm_user_id, exc)

    lines = [
        "✅ <b>Расписание обновлено</b>",
        "",
        f"Период: {parsed.starts_on:%d.%m.%Y} — {parsed.ends_on:%d.%m.%Y}",
        f"Создано занятий: {result.created}",
        f"Удалено старых: {result.removed}",
    ]
    if result.notify:
        lines.append(f"Предупреждено студентов: {notified} из {len(result.notify)}")
    await ctx.send(
        s.user_id, "\n".join(lines),
        reactions={"arrow_left": "adm:menu"},
        data={"screen": "admin_stats"},
    )


# ---------- админы ----------

async def show_admins(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    items = await students_svc.list_admins(session)
    lines = ["🛡 <b>Админы</b>", "", "👑 — суперадмин из .env (снять нельзя).",
             "Новый админ добавляется по корпоративной почте."]
    reactions: dict[str, str] = {}
    legend: list[tuple[int, str]] = []
    for i, a in enumerate(items, 1):
        label = a.email or a.mm_username or "?"
        lines.append(f"{'👑' if a.is_superadmin else '🛡'} {label}")
        if not a.is_superadmin and admin.is_superadmin:
            name, _ = R.number(i)
            reactions[name] = f"adm:del_admin:{a.id}"
            legend.append((i, f"снять {label}"))
    if admin.is_superadmin:
        reactions["heavy_plus_sign"] = "adm:add_admin"
    if legend:
        lines += ["", "Снять админа реакцией:"]
        lines.append(_legend(legend))
    reactions["arrow_left"] = "adm:menu"
    await ctx.send(
        s.user_id, "\n".join(lines), reactions=reactions,
        data={"screen": "admin_admins"},
    )


async def add_admin_start(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin) -> None:
    if not admin.is_superadmin:
        await ctx.notify(s.user_id, "Только суперадмин может добавлять админов.")
        return
    await ctx.send(
        s.user_id, "Пришли корпоративную почту нового админа.",
        reactions={"x": "fsm:cancel"}, fsm="add_admin",
    )


async def _add_admin(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, text: str) -> None:
    s.fsm = ""
    s.fsm_data = {}
    created = await students_svc.add_admin(session, text)
    if created is None:
        await ctx.notify(s.user_id, "Пустая или некорректная почта.")
        return
    await students_svc.log_action(session, admin.email, admin.mm_username, "add_admin", created.email)
    await ctx.notify(s.user_id, f"✅ {created.email} теперь админ.")
    await show_admins(ctx, session, s, admin)


async def del_admin(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, admin_id: int) -> None:
    if not admin.is_superadmin:
        await ctx.notify(s.user_id, "Только суперадмин может снимать админов.")
        return
    ok = await students_svc.remove_admin(session, admin_id)
    await ctx.notify(s.user_id, "Снят ✅" if ok else "Нельзя снять суперадмина.")
    await show_admins(ctx, session, s, admin)


# ---------- FSM-диспетчер ----------

async def handle_fsm_post(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, text: str, file_ids: list[str]) -> None:
    """Обрабатывает текстовое сообщение, когда активен FSM-шаг."""
    if admin is None:
        # FSM у нас только у админов; сбиваем чужой/устаревший шаг
        s.fsm = ""
        s.fsm_data = {}
        return
    step = s.fsm
    if step.startswith("new_training."):
        await _new_training_step(ctx, session, s, admin, step, text)
        return
    if step == "find_student":
        await _find_student(ctx, session, s, admin, text)
        return
    if step == "add_admin":
        await _add_admin(ctx, session, s, admin, text)
        return
    if step == "import.waiting_file":
        await _import_file(ctx, session, s, admin, file_ids, text)
        return
    if step == "import.confirm":
        # ждали реакцию подтверждения — текст игнорируем
        await ctx.notify(s.user_id, "Нажми 🔥 для подтверждения или ❌ для отмены.")
        return
    # неизвестный шаг — выходим из FSM
    s.fsm = ""
    s.fsm_data = {}


async def cancel_fsm(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin | None) -> None:
    s.fsm = ""
    s.fsm_data = {}
    if admin:
        await admin_menu(ctx, session, s, None, admin)