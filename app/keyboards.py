from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import Admin, Attendance, Booking, Student, Training
from app.tz import WEEKDAYS_RU, fmt_short, to_local


class TrainingCB(CallbackData, prefix="tr"):
    action: str          # view | book | cancel | back
    training_id: int = 0
    # откуда пришли: неделя и день сетки. weekday = -1 — это не из картинки
    # (например, из «Мои записи» или из напоминалки), там экран текстовый.
    offset: int = 0
    weekday: int = -1


class WeekCB(CallbackData, prefix="wk"):
    action: str          # week | day
    offset: int = 0
    weekday: int = -1


class PollCB(CallbackData, prefix="poll"):
    booking_id: int
    came: bool


class AdmTrainingCB(CallbackData, prefix="atr"):
    action: str          # view | cancel | cancel_confirm | people | attend
    training_id: int = 0
    offset: int = 0      # неделя и день сетки — чтобы было куда вернуться
    weekday: int = 0


class AdmWeekCB(CallbackData, prefix="awk"):
    """Навигация по расписанию в админке: та же картинка, что у студента."""
    action: str          # week | day
    offset: int = 0
    weekday: int = 0


class AttendCB(CallbackData, prefix="att"):
    booking_id: int
    value: str           # toggle


class AdmStudentCB(CallbackData, prefix="ast"):
    action: str          # view | ban | unban | reset
    student_id: int


class AdmCB(CallbackData, prefix="adm"):
    action: str          # menu | trainings | new | students | banned | stats | export | admins | add_admin | del_admin
    obj_id: int = 0


class NavCB(CallbackData, prefix="nav"):
    """Нижний ряд кнопок под расписанием."""
    action: str          # schedule | my | help | admin
    offset: int = 0


# ---------- студент ----------

def _nav_row(kb: InlineKeyboardBuilder, offset: int, is_admin: bool) -> None:
    row = InlineKeyboardBuilder()
    row.button(text="🎫 Мои записи", callback_data=NavCB(action="my", offset=offset).pack())
    row.button(text="ℹ️ Помощь", callback_data=NavCB(action="help", offset=offset).pack())
    row.adjust(2)
    kb.attach(row)
    if is_admin:
        adm = InlineKeyboardBuilder()
        adm.button(text="🛠 Админка", callback_data=NavCB(action="admin", offset=offset).pack())
        adm.adjust(1)
        kb.attach(adm)


def back_to_schedule_kb(offset: int, is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К расписанию", callback_data=NavCB(action="schedule", offset=offset).pack())
    kb.adjust(1)
    _nav_row(kb, offset, is_admin)
    return kb.as_markup()


def auth_kb(login_url: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if login_url:
        kb.button(text="🔐 Войти на сайте ЦУ", url=login_url)
        kb.button(text="✅ Я вошёл", callback_data="auth:check")
    else:
        kb.button(text="🔐 Авторизоваться", callback_data="auth:check")
    kb.adjust(1)
    return kb.as_markup()


def consent_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтверждаю", callback_data="consent:accept")
    kb.adjust(1)
    return kb.as_markup()


def schedule_kb(items: list[tuple[Training, int]], booked_ids: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for training, taken in items:
        free = max(training.capacity - taken, 0)
        if training.id in booked_ids:
            mark = "✅"
        elif free == 0:
            mark = "🔴"
        else:
            mark = "🟢"
        kb.button(
            text=f"{mark} {fmt_short(training.starts_at)} · {training.title} ({taken}/{training.capacity})",
            callback_data=TrainingCB(action="view", training_id=training.id).pack(),
        )
    kb.adjust(1)
    return kb.as_markup()


def week_kb(
    offset: int, days: list[tuple[int, bool]], has_next: bool, is_admin: bool = False
) -> InlineKeyboardMarkup:
    """days: (номер дня недели, есть ли своя запись в этот день)."""
    kb = InlineKeyboardBuilder()
    for weekday, mine in days:
        mark = "✅ " if mine else ""
        kb.button(
            text=f"{mark}{WEEKDAYS_RU[weekday]}",
            callback_data=WeekCB(action="day", offset=offset, weekday=weekday).pack(),
        )
    kb.adjust(len(days) or 1)

    nav = InlineKeyboardBuilder()
    if offset > 0:
        nav.button(text="← Пред. неделя", callback_data=WeekCB(action="week", offset=offset - 1).pack())
    if has_next:
        nav.button(text="След. неделя →", callback_data=WeekCB(action="week", offset=offset + 1).pack())
    nav.adjust(2)
    kb.attach(nav)

    _nav_row(kb, offset, is_admin)
    return kb.as_markup()


def day_kb(offset: int, weekday: int, items: list[tuple[Training, int, bool]]) -> InlineKeyboardMarkup:
    """items: (тренировка, занято мест, записан ли студент). Отменённых здесь не бывает."""
    kb = InlineKeyboardBuilder()
    for training, taken, mine in items:
        if mine:
            mark = "✅"
        elif taken >= training.capacity:
            mark = "🔴"
        else:
            mark = "🟢"
        start = to_local(training.starts_at)
        kb.button(
            text=f"{mark} {start:%H:%M} · {training.title}",
            callback_data=TrainingCB(
                action="view", training_id=training.id, offset=offset, weekday=weekday
            ).pack(),
        )
    kb.button(text="⬅️ К расписанию", callback_data=WeekCB(action="week", offset=offset).pack())
    kb.adjust(1)
    return kb.as_markup()


def training_kb(training: Training, is_booked: bool, offset: int = 0, weekday: int = -1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # на отменённую тренировку записаться нельзя — кнопки нет вообще
    if not training.is_cancelled:
        action = "cancel" if is_booked else "book"
        kb.button(
            text="❌ Отменить запись" if is_booked else "✍️ Записаться",
            callback_data=TrainingCB(
                action=action, training_id=training.id, offset=offset, weekday=weekday
            ).pack(),
        )
    if weekday >= 0:
        kb.button(
            text=f"⬅️ К {WEEKDAYS_RU[weekday].lower()}",
            callback_data=WeekCB(action="day", offset=offset, weekday=weekday).pack(),
        )
    else:
        kb.button(text="⬅️ К расписанию", callback_data=NavCB(action="schedule").pack())
    kb.adjust(1)
    return kb.as_markup()


def my_bookings_kb(bookings: list[Booking], offset: int = 0, is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bookings:
        kb.button(
            text=f"❌ Отменить · {fmt_short(b.training.starts_at)} {b.training.title}",
            callback_data=TrainingCB(action="cancel", training_id=b.training_id).pack(),
        )
    kb.button(text="⬅️ К расписанию", callback_data=NavCB(action="schedule", offset=offset).pack())
    kb.adjust(1)
    _nav_row(kb, offset, is_admin)
    return kb.as_markup()


def poll_kb(booking_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, был", callback_data=PollCB(booking_id=booking_id, came=True).pack())
    kb.button(text="❌ Не получилось", callback_data=PollCB(booking_id=booking_id, came=False).pack())
    kb.adjust(2)
    return kb.as_markup()


def reminder_kb(training_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Не смогу прийти",
        callback_data=TrainingCB(action="cancel", training_id=training_id).pack(),
    )
    return kb.as_markup()


# ---------- админка ----------

def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Тренировки", callback_data=AdmCB(action="trainings").pack())
    kb.button(text="➕ Новая тренировка", callback_data=AdmCB(action="new").pack())
    kb.button(text="🗓 Выгрузить расписание", callback_data=AdmCB(action="export_schedule").pack())
    kb.button(text="📥 Загрузить расписание", callback_data=AdmCB(action="import_schedule").pack())
    kb.button(text="🔎 Найти студента", callback_data=AdmCB(action="students").pack())
    kb.button(text="🚫 Заблокированные", callback_data=AdmCB(action="banned").pack())
    kb.button(text="📊 Статистика", callback_data=AdmCB(action="stats").pack())
    kb.button(text="📤 Записи в CSV", callback_data=AdmCB(action="export").pack())
    kb.button(text="🛡 Админы", callback_data=AdmCB(action="admins").pack())
    kb.button(text="📅 Выйти к расписанию", callback_data=NavCB(action="schedule").pack())
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def import_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Да, перезаписать расписание", callback_data=AdmCB(action="import_apply").pack())
    kb.button(text="⬅️ Отмена", callback_data=AdmCB(action="menu").pack())
    kb.adjust(1)
    return kb.as_markup()


def admin_week_kb(
    offset: int, days: list[int], has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """Выбор дня по картинке недели — так же, как у студента."""
    kb = InlineKeyboardBuilder()
    for weekday in days:
        kb.button(
            text=WEEKDAYS_RU[weekday],
            callback_data=AdmWeekCB(action="day", offset=offset, weekday=weekday).pack(),
        )
    kb.adjust(len(days) or 1)

    nav = InlineKeyboardBuilder()
    if has_prev:
        nav.button(text="← Пред. неделя", callback_data=AdmWeekCB(action="week", offset=offset - 1).pack())
    if has_next:
        nav.button(text="След. неделя →", callback_data=AdmWeekCB(action="week", offset=offset + 1).pack())
    nav.adjust(2)
    kb.attach(nav)

    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ В админку", callback_data=AdmCB(action="menu").pack())
    tail.adjust(1)
    kb.attach(tail)
    return kb.as_markup()


def admin_day_kb(offset: int, weekday: int, items: list[tuple[Training, int]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for training, taken in items:
        mark = "❌" if training.is_cancelled else ("🔴" if taken >= training.capacity else "🟢")
        start = to_local(training.starts_at)
        kb.button(
            text=f"{mark} {start:%H:%M} · {training.title} · {taken}/{training.capacity}",
            callback_data=AdmTrainingCB(
                action="view", training_id=training.id, offset=offset, weekday=weekday
            ).pack(),
        )
    kb.button(text="⬅️ К расписанию", callback_data=AdmWeekCB(action="week", offset=offset).pack())
    kb.adjust(1)
    return kb.as_markup()


def admin_training_kb(
    training: Training, is_past: bool, offset: int = 0, weekday: int = 0
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="👥 Участники",
        callback_data=AdmTrainingCB(
            action="people", training_id=training.id, offset=offset, weekday=weekday
        ).pack(),
    )
    if is_past:
        kb.button(
            text="✅ Отметить посещаемость",
            callback_data=AdmTrainingCB(
                action="attend", training_id=training.id, offset=offset, weekday=weekday
            ).pack(),
        )
    if not training.is_cancelled and not is_past:
        kb.button(
            text="❌ Отменить тренировку",
            callback_data=AdmTrainingCB(
                action="cancel", training_id=training.id, offset=offset, weekday=weekday
            ).pack(),
        )
    kb.button(
        text=f"⬅️ К {WEEKDAYS_RU[weekday].lower()}",
        callback_data=AdmWeekCB(action="day", offset=offset, weekday=weekday).pack(),
    )
    kb.adjust(1)
    return kb.as_markup()


def confirm_cancel_kb(training_id: int, offset: int = 0, weekday: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔥 Да, отменить и оповестить",
        callback_data=AdmTrainingCB(
            action="cancel_confirm", training_id=training_id, offset=offset, weekday=weekday
        ).pack(),
    )
    kb.button(
        text="⬅️ Назад",
        callback_data=AdmTrainingCB(
            action="view", training_id=training_id, offset=offset, weekday=weekday
        ).pack(),
    )
    kb.adjust(1)
    return kb.as_markup()


ATTENDANCE_ICONS = {
    Attendance.ATTENDED: "✅",
    Attendance.NO_SHOW: "❌",
    Attendance.EXCUSED: "➖",
    Attendance.UNKNOWN: "❔",
}


def attendance_kb(
    bookings: list[Booking], training_id: int, offset: int = 0, weekday: int = 0
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for b in bookings:
        kb.button(
            text=f"{ATTENDANCE_ICONS[b.attendance]} {b.student.display_name}",
            callback_data=AttendCB(booking_id=b.id, value="toggle").pack(),
        )
    kb.button(
        text="⬅️ К тренировке",
        callback_data=AdmTrainingCB(
            action="view", training_id=training_id, offset=offset, weekday=weekday
        ).pack(),
    )
    kb.adjust(1)
    return kb.as_markup()


def student_card_kb(student: Student) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if student.is_banned:
        kb.button(text="♻️ Разблокировать", callback_data=AdmStudentCB(action="unban", student_id=student.id).pack())
    else:
        kb.button(text="🚫 Заблокировать", callback_data=AdmStudentCB(action="ban", student_id=student.id).pack())
    if student.no_show_count:
        kb.button(
            text=f"🧹 Сбросить пропуски ({student.no_show_count})",
            callback_data=AdmStudentCB(action="reset", student_id=student.id).pack(),
        )
    kb.button(text="⬅️ В админку", callback_data=AdmCB(action="menu").pack())
    kb.adjust(1)
    return kb.as_markup()


def students_list_kb(students: list[Student]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in students:
        mark = "🚫" if s.is_banned else "👤"
        kb.button(
            text=f"{mark} {s.display_name} · пропусков {s.no_show_count}",
            callback_data=AdmStudentCB(action="view", student_id=s.id).pack(),
        )
    kb.button(text="⬅️ В админку", callback_data=AdmCB(action="menu").pack())
    kb.adjust(1)
    return kb.as_markup()


def admins_kb(admins: list[Admin], can_edit: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for a in admins:
        label = f"@{a.tg_username}" if a.tg_username else f"id{a.tg_user_id}"
        if a.is_superadmin:
            kb.button(text=f"👑 {label}", callback_data="noop")
        elif can_edit:
            kb.button(text=f"🗑 {label}", callback_data=AdmCB(action="del_admin", obj_id=a.id).pack())
        else:
            kb.button(text=f"🛡 {label}", callback_data="noop")
    if can_edit:
        kb.button(text="➕ Добавить админа", callback_data=AdmCB(action="add_admin").pack())
    kb.button(text="⬅️ В админку", callback_data=AdmCB(action="menu").pack())
    kb.adjust(1)
    return kb.as_markup()


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В админку", callback_data=AdmCB(action="menu").pack())]]
    )

