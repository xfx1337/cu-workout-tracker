from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

log = logging.getLogger(__name__)


async def safe_edit(message: Message, text: str, reply_markup=None) -> None:
    """edit_text, который не падает на 'message is not modified'."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def safe_edit_caption(message: Message, caption: str, reply_markup=None) -> None:
    """То же самое для сообщений с фото — у них правится подпись, а не текст."""
    try:
        await message.edit_caption(caption=caption, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def edit_view(message: Message, text: str, reply_markup=None) -> None:
    """Правит экран на месте: у фото — подпись, у обычного сообщения — текст."""
    if message.photo:
        await safe_edit_caption(message, text, reply_markup)
    else:
        await safe_edit(message, text, reply_markup)


async def _drop(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest as exc:
        # телеграм не даёт удалять сообщения старше 48 часов — не беда
        log.info("не удалось удалить сообщение: %s", exc)


async def swap_to_photo(message: Message, photo, caption: str, reply_markup=None) -> None:
    """Показать картинку. Текстовое сообщение в фото не редактируется — заменяем."""
    from aiogram.enums import ParseMode
    from aiogram.types import InputMediaPhoto

    if message.photo:
        await message.edit_media(
            InputMediaPhoto(media=photo, caption=caption, parse_mode=ParseMode.HTML),
            reply_markup=reply_markup,
        )
        return
    await _drop(message)
    await message.answer_photo(photo, caption=caption, reply_markup=reply_markup)


async def swap_to_text(message: Message, text: str, reply_markup=None) -> None:
    """Показать текстовый экран вместо картинки."""
    if not message.photo:
        await safe_edit(message, text, reply_markup)
        return
    await _drop(message)
    await message.answer(text, reply_markup=reply_markup)
