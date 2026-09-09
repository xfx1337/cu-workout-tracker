"""Реакции-эмодзи как «кнопки».

В TiMe (Mattermost) у бота нет входящего HTTP, пока не задан MM_PUBLIC_URL, поэтому
нажатие кнопки заменяем реакцией: бот вешает на сообщение набор эмодзи, пользователь
«кликает», добавив одну из них, и по WebSocket прилетает reaction_added.

Здесь собраны имена эмодзи (их принимает /reactions) и их символы для подписей.
Имена берём из стандартного набора Mattermost, чтобы реакция гарантированно создалась.
"""

from __future__ import annotations

# emoji_name (для API) -> символ (для подписей в тексте)
EMOJI: dict[str, str] = {
    "white_check_mark": "✅",
    "x": "❌",
    "arrow_left": "⬅️",
    "arrow_right": "➡️",
    "heavy_plus_sign": "➕",
    "mag": "🔎",
    "no_entry": "🚫",
    "calendar": "🗓",
    "bar_chart": "📊",
    "outbox_tray": "📤",
    "inbox_tray": "📥",
    "busts_in_silhouette": "👥",
    "shield": "🛡",
    "fire": "🔥",
    "wrench": "🛠",
    "ticket": "🎫",
    "information_source": "ℹ️",
    "memo": "📝",
    "pause_button": "⏸",
    "crown": "👑",
    "point_right": "👉",
}

# числа 1..10 — стандартные эмодзи-цифры Mattermost: (имя, символ)
NUMBERS: list[tuple[str, str]] = [
    ("one", "1️⃣"), ("two", "2️⃣"), ("three", "3️⃣"), ("four", "4️⃣"),
    ("five", "5️⃣"), ("six", "6️⃣"), ("seven", "7️⃣"), ("eight", "8️⃣"),
    ("nine", "9️⃣"), ("ten", "🔟"),
]


def number(i: int) -> tuple[str, str]:
    """(имя эмодзи, символ) для номера 1..10."""
    return NUMBERS[i - 1]


def symbol(name: str) -> str:
    return EMOJI.get(name, name)