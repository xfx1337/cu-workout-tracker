"""Диспетчер: превращает реакцию/текст в вызов обработчика.

Действия кодируются строками вида «префикс:аргумент[:аргумент]» — их кладёт
в карту реакций BotContext.send(). Здесь они разбираются и маршрутизируются.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers import admin as admin_h
from app.handlers import common as common_h
from app.handlers import student as student_h
from app.models import Admin, Student
from app.runtime import BotContext, UserSession


async def dispatch_action(
    ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, action: str,
) -> None:
    if not action:
        return
    parts = action.split(":")
    head = parts[0]

    # --- общее ---
    if action == "fsm:cancel":
        await admin_h.cancel_fsm(ctx, session, s, admin)
        return
    if action == "consent:accept":
        await common_h.consent_accept(ctx, session, s, student, admin)
        return

    # --- навигация ---
    if head == "nav":
        sub = parts[1]
        offset = s.data.get("offset", 0)
        if sub == "schedule":
            await common_h.nav_schedule(ctx, session, s, student, admin, offset)
        elif sub == "my":
            await common_h.nav_my(ctx, session, s, student, admin, offset)
        elif sub == "help":
            await common_h.nav_help(ctx, session, s, student, admin, offset)
        elif sub == "admin":
            await common_h.nav_admin(ctx, session, s, student, admin, offset)
        return

    # --- студент: неделя ---
    if head == "wk":
        sub = parts[1]
        if sub == "week":
            await student_h.open_schedule(ctx, session, s, student, admin, offset=int(parts[2]))
        elif sub == "day":
            await student_h.show_day(ctx, session, s, student, s.data.get("offset", 0), int(parts[2]))
        return

    # --- студент: тренировка / запись / опрос ---
    if head == "tr":
        sub = parts[1]
        training_id = int(parts[2])
        if sub == "view":
            await student_h.show_training(ctx, session, s, student, training_id)
        elif sub == "book":
            await student_h.book(ctx, session, s, student, training_id)
        elif sub == "cancel":
            await student_h.cancel(ctx, session, s, student, training_id)
        return

    if head == "poll":
        came = parts[1] == "yes"
        await student_h.poll_answer(ctx, session, s, student, int(parts[2]), came)
        return

    # --- всё остальное — админка ---
    if admin is None:
        return

    if head == "adm":
        await _admin_menu_action(ctx, session, s, admin, parts[1:])
        return

    if head == "awk":
        sub = parts[1]
        if sub == "week":
            await admin_h.show_admin_week(ctx, session, s, offset=int(parts[2]))
        elif sub == "day":
            await admin_h.show_admin_day(ctx, session, s, s.data.get("offset", 0), int(parts[2]))
        return

    if head == "atr":
        sub = parts[1]
        training_id = int(parts[2])
        if sub == "view":
            await admin_h.show_admin_training(ctx, session, s, training_id)
        elif sub == "people":
            await admin_h.show_people(ctx, session, s, admin, training_id)
        elif sub == "attend":
            await admin_h.show_attendance(ctx, session, s, training_id)
        elif sub == "cancel":
            await admin_h.cancel_training_ask(ctx, session, s, training_id)
        elif sub == "cancel_confirm":
            await admin_h.cancel_training_do(ctx, session, s, admin, training_id)
        return

    if head == "att":
        await admin_h.attendance_toggle(ctx, session, s, admin, int(parts[2]))
        return

    if head == "ast":
        sub = parts[1]
        student_id = int(parts[2])
        if sub == "view":
            await admin_h.show_student(ctx, session, s, admin, student_id)
        elif sub == "ban":
            await admin_h.student_ban(ctx, session, s, admin, student_id)
        elif sub == "unban":
            await admin_h.student_unban(ctx, session, s, admin, student_id)
        elif sub == "reset":
            await admin_h.student_reset(ctx, session, s, admin, student_id)
        return


async def _admin_menu_action(
    ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin, parts: list[str],
) -> None:
    key = parts[0]
    if key == "menu":
        await admin_h.admin_menu(ctx, session, s, None, admin)
    elif key == "trainings":
        await admin_h.show_admin_week(ctx, session, s, offset=0)
    elif key == "new":
        await admin_h.new_training_start(ctx, session, s, admin)
    elif key == "export_schedule":
        await admin_h.export_schedule(ctx, session, s, admin)
    elif key == "import_schedule":
        await admin_h.import_schedule_start(ctx, session, s, admin)
    elif key == "students":
        await admin_h.find_student_start(ctx, session, s, admin)
    elif key == "banned":
        await admin_h.banned_students(ctx, session, s, admin)
    elif key == "stats":
        await admin_h.show_stats(ctx, session, s, admin)
    elif key == "export":
        await admin_h.export_csv(ctx, session, s, admin)
    elif key == "admins":
        await admin_h.show_admins(ctx, session, s, admin)
    elif key == "add_admin":
        await admin_h.add_admin_start(ctx, session, s, admin)
    elif key == "del_admin":
        await admin_h.del_admin(ctx, session, s, admin, int(parts[1]))
    elif key == "import_apply":
        await admin_h.import_schedule_apply(ctx, session, s, admin)