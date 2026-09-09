"""Экран бота: текст плюс набор действий.

Одно и то же описание экрана рисуется двумя способами:

* кнопками-attachments, когда сервер Mattermost может достучаться до бота
  (MM_PUBLIC_URL). Выглядит как обычные кнопки, экран обновляется на месте;
* реакциями-эмодзи, когда входящего адреса нет. Тогда подписи уходят в текст
  сообщения нумерованным списком, а «клик» — это поставленная реакция.

Обработчики про это не знают: они описывают экран списком Choice, а выбор
способа отрисовки живёт здесь и в BotContext.send().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app import reactions as R

# Стиль кнопки в Mattermost: обычная, акцентная, опасная.
DEFAULT, PRIMARY, DANGER = "default", "primary", "danger"


@dataclass(slots=True)
class Choice:
    """Одно действие на экране."""

    label: str                 # подпись на кнопке
    action: str                # строка вида «nav:schedule», её разбирает dispatch_action
    style: str = DEFAULT
    emoji: str = ""            # чем показать в режиме реакций; пусто — возьмём номер


@dataclass(slots=True)
class Screen:
    """Экран целиком: текст, картинка и действия, разбитые на визуальные группы."""

    text: str
    groups: list[list[Choice]] = field(default_factory=list)
    image: bytes | None = None
    filename: str = "screen.png"

    @property
    def choices(self) -> list[Choice]:
        return [c for group in self.groups for c in group]


def group(*choices: Choice) -> list[Choice]:
    """Ряд кнопок — в Mattermost это отдельный блок-attachment."""
    return [c for c in choices if c is not None]


def screen(text: str, *groups: Iterable[Choice], image: bytes | None = None,
           filename: str = "screen.png") -> Screen:
    return Screen(
        text=text,
        groups=[list(g) for g in groups if g],
        image=image,
        filename=filename,
    )


# ---------------------------------------------------------------- кнопки

def build_props(scr: Screen, action_url: str) -> dict[str, Any]:
    """props для поста: каждая группа — отдельный блок с кнопками.

    integration.url Mattermost клиентам не отдаёт, поэтому одноразовый токен
    внутри адреса не виден никому, кроме самого сервера.
    """
    attachments = []
    for gi, grp in enumerate(scr.groups):
        actions = []
        for ci, choice in enumerate(grp):
            action: dict[str, Any] = {
                "id": f"g{gi}_{ci}",
                "name": choice.label,
                # без type Mattermost ругается в лог и грозит ошибкой в будущих версиях
                "type": "button",
                "integration": {
                    "url": action_url,
                    "context": {"action": choice.action},
                },
            }
            if choice.style != DEFAULT:
                action["style"] = choice.style
            actions.append(action)
        if actions:
            attachments.append({"actions": actions})
    return {"attachments": attachments} if attachments else {}


# ---------------------------------------------------------------- реакции

def build_reactions(scr: Screen) -> tuple[str, dict[str, str]]:
    """Запасной режим: подписи уходят в текст, «кнопка» — это эмодзи.

    Возвращает (текст с легендой, карта emoji -> action).
    """
    mapping: dict[str, str] = {}
    lines: list[str] = []
    number = 1

    for grp in scr.groups:
        if lines:
            lines.append("")
        for choice in grp:
            emoji = choice.emoji
            if not emoji or emoji in mapping:
                emoji, symbol = R.number(number)
                number += 1
            else:
                symbol = R.symbol(emoji)
            mapping[emoji] = choice.action
            lines.append(f"{symbol} {choice.label}")

    text = scr.text if not lines else f"{scr.text}\n\n" + "\n".join(lines)
    return text, mapping
