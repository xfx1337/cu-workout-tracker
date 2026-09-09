from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.auth import login_url, resolve_student
from app.config import settings
from app.keyboards import NavCB, auth_kb, back_to_schedule_kb, consent_kb
from app.models import Admin, Student
from app.services import students as students_svc

router = Router(name="common")


async def send_auth_prompt(message: Message, student: Student) -> None:
    text = texts.AUTH_PROMPT
    if settings.auth_is_stub:
        text = f"{texts.AUTH_PROMPT}\n\n{texts.AUTH_STUB_NOTE}"
    await message.answer(text, reply_markup=auth_kb(login_url(student.tg_user_id)))


async def send_consent(message: Message) -> None:
    await message.answer(texts.CONSENT, reply_markup=consent_kb())


def help_text(is_admin: bool) -> str:
    lines = [
        "<b>Как это работает</b>",
        "",
        "На картинке — расписание на неделю. Прямо на карточке видно, "
        "сколько мест осталось и записан ли ты.",
        "Жми день недели под картинкой → выбирай тренировку → «Записаться».",
        "",
        f"⏰ За {settings.reminder_minutes_before} мин до начала придёт напоминание.",
        "После тренировки бот спросит, получилось ли прийти.",
        "",
        f"⚠️ Если {settings.no_show_limit} раза не прийти, не отменив запись, "
        "доступ к записи закроется. Отменяй заранее — это бесплатно и никак не наказывается.",
    ]
    if is_admin:
        lines += ["", "🛠 У тебя есть доступ к админке — кнопка ниже."]
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(
    message: Message, session: AsyncSession, student: Student, admin: Admin | None, state: FSMContext
) -> None:
    await state.clear()

    if not student.is_authorized:
        # ReplyKeyboardRemove убирает старую клавиатуру у тех, кто застал прошлую версию
        await message.answer(texts.START_NEW, reply_markup=ReplyKeyboardRemove())
        await send_auth_prompt(message, student)
        return

    if student.consent_accepted_at is None:
        await message.answer("С возвращением!", reply_markup=ReplyKeyboardRemove())
        await send_consent(message)
        return

    await message.answer("Секунду, открываю расписание…", reply_markup=ReplyKeyboardRemove())
    from app.handlers.student import open_schedule

    await open_schedule(message, session, student, admin, offset=0)


@router.callback_query(F.data == "auth:check")
async def auth_check(
    call: CallbackQuery, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    identity = await resolve_student(student.tg_user_id, student.tg_username)
    if identity is None:
        await call.answer()
        await call.message.answer(texts.AUTH_FAILED, reply_markup=auth_kb(login_url(student.tg_user_id)))
        return

    await students_svc.attach_identity(session, student, identity)
    await call.answer(texts.AUTH_OK)
    await call.message.edit_text(texts.AUTH_OK)

    if student.consent_accepted_at is None:
        await send_consent(call.message)
        return

    from app.handlers.student import open_schedule

    await open_schedule(call.message, session, student, admin, offset=0)


@router.callback_query(F.data == "consent:accept")
async def consent_accept(
    call: CallbackQuery, session: AsyncSession, student: Student, admin: Admin | None
) -> None:
    await students_svc.accept_consent(session, student)
    await call.answer("Принято")
    await call.message.edit_text(
        f"{texts.CONSENT}\n\n✅ Подтверждено. Теперь можно записываться на тренировки."
    )

    from app.handlers.student import open_schedule

    await open_schedule(call.message, session, student, admin, offset=0)


@router.message(Command("help"))
async def cmd_help(message: Message, admin: Admin | None) -> None:
    await message.answer(help_text(bool(admin)), reply_markup=back_to_schedule_kb(0, bool(admin)))


@router.callback_query(NavCB.filter(F.action == "help"))
async def nav_help(call: CallbackQuery, callback_data: NavCB, admin: Admin | None) -> None:
    from app.utils import safe_edit, safe_edit_caption

    text = help_text(bool(admin))
    kb = back_to_schedule_kb(callback_data.offset, bool(admin))
    await call.answer()
    if call.message.photo:
        await safe_edit_caption(call.message, text, kb)
    else:
        await safe_edit(call.message, text, kb)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()
