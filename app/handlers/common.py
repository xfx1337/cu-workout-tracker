from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import settings
from app.mm.client import MMUser
from app.models import Admin, Student
from app.runtime import BotContext, UserSession
from app.services import students as students_svc
from app.ui import ADMIN, HELP, MY, Choice, group, screen

log = logging.getLogger(__name__)


async def ensure_identity(session: AsyncSession, user: MMUser) -> tuple[Student, Admin | None]:
    """Заводит студента по данным из Mattermost и ищет админа по почте.

    Личность приходит из мессенджера, отдельной авторизации нет.
    """
    student = await students_svc.get_or_create(
        session, user.id, username=user.username, email=user.email, full_name=user.display_name
    )
    admin = await students_svc.is_admin(session, user.id, email=user.email, username=user.username)
    return student, admin


def help_text(is_admin: bool) -> str:
    lines = [
        "<b>Как это работает</b>",
        "",
        "На картинке — расписание на неделю. Прямо на карточке видно, "
        "сколько мест осталось и записан ли ты.",
        "Под картинкой — кнопки: день недели, ←/→ для соседних недель.",
        "",
        "Жми кнопку, чтобы выбрать день и записаться.",
        "",
        f"⏰ За {settings.reminder_minutes_before} мин до начала придёт напоминание.",
        "После тренировки бот спросит, получилось ли прийти.",
        "",
        f"⚠️ Если {settings.no_show_limit} раза не прийти, не отменив запись, "
        "доступ к записи закроется. Отменяй заранее — это бесплатно и никак не наказывается.",
    ]
    if is_admin:
        lines += ["", "🛠 У тебя есть доступ к админке — напиши «админка» или нажми кнопку «Админка»."]
    return "\n".join(lines)


async def show_help(ctx: BotContext, session: AsyncSession, s: UserSession, admin: Admin | None, offset: int = 0) -> None:
    choices = [Choice("Назад", "nav:schedule", emoji="arrow_left"), MY, HELP]
    if admin:
        choices.append(ADMIN)
    await ctx.show(
        s.user_id, screen(help_text(bool(admin)), group(*choices)),
        data={"screen": "help", "offset": offset},
    )


async def show_consent(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student) -> None:
    await ctx.show(
        s.user_id,
        screen(
            f"{texts.CONSENT}\n\nНажми кнопку «Подтверждаю», чтобы дать согласие.",
            group(Choice("Подтверждаю", "consent:accept", emoji="white_check_mark")),
        ),
        data={"screen": "consent"},
    )


async def consent_accept(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None) -> None:
    await students_svc.accept_consent(session, student)
    await ctx.notify(s.user_id, "✅ Подтверждено. Теперь можно записываться на тренировки.")
    from app.handlers import student as student_h

    await student_h.open_schedule(ctx, session, s, student, admin, offset=0)


async def nav_schedule(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0) -> None:
    from app.handlers import student as student_h

    await student_h.open_schedule(ctx, session, s, student, admin, offset=offset)


async def nav_my(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0) -> None:
    from app.handlers import student as student_h

    await student_h.my_bookings(ctx, session, s, student, admin, offset=offset)


async def nav_help(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0) -> None:
    await show_help(ctx, session, s, admin, offset=offset)


async def nav_admin(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, offset: int = 0) -> None:
    if not admin:
        return
    from app.handlers import admin as admin_h

    await admin_h.admin_menu(ctx, session, s, student, admin)


async def handle_text(ctx: BotContext, session: AsyncSession, s: UserSession, student: Student, admin: Admin | None, text: str) -> None:
    """Входная точка для любого текстового сообщения из лички (не FSM)."""
    from app.handlers import admin as admin_h
    from app.handlers import student as student_h

    norm = text.strip().lower()
    if norm in {"расписание", "start", "начать", "меню", "schedule", "назад"}:
        await student_h.open_schedule(ctx, session, s, student, admin, offset=0)
    elif norm in {"мои", "записи", "my"}:
        await student_h.my_bookings(ctx, session, s, student, admin, offset=0)
    elif norm in {"помощь", "help", "как это работает"}:
        await show_help(ctx, session, s, admin, offset=0)
    elif norm in {"админка", "admin"}:
        if admin:
            await admin_h.admin_menu(ctx, session, s, student, admin)
        else:
            await show_help(ctx, session, s, None, offset=0)
    else:
        # первый контакт / незнакомый текст — просто открываем расписание
        await student_h.open_schedule(ctx, session, s, student, admin, offset=0)