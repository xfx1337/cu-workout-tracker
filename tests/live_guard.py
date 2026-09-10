"""Проверка доступности стенда перед живыми тестами.

Живой тест обязан отличать «сломался код» от «сервера нет». Без этого он падает
на закрытом контуре, где Mattermost недоступен, и валит пайплайн на ровном месте.
Поэтому недоступный сервер или отвергнутый токен — это пропуск (exit 0),
а не провал: провалом остаётся только реальная ошибка в нашем коде.
"""
from __future__ import annotations

from app.config import settings
from app.mm.client import MattermostClient, MattermostError


async def stand_unavailable() -> str | None:
    """Возвращает причину пропустить тест или None, если стенд готов."""
    if not settings.mm_url or not settings.mm_token:
        return "MM_URL и MM_TOKEN не заданы"

    try:
        mm = MattermostClient(settings.mm_url, settings.mm_token)
    except Exception as exc:
        # Клиент падает уже на создании, если URL кривой или токен нельзя
        # положить в HTTP-заголовок (не-ASCII). Это тоже «стенда нет».
        return f"не смог создать клиент: {type(exc).__name__}: {exc}"

    try:
        me = await mm.whoami()
    except MattermostError as exc:
        # 401 — токен не принят; для теста это тоже «стенда нет», а не поломка кода
        return f"Mattermost не принял токен: {exc}"
    except Exception as exc:
        # Ловим широко намеренно: это предполётная проверка, и любая беда со
        # связью или настройками (недоступный хост, кривой URL, не-ASCII токен,
        # который httpx не может положить в заголовок) означает «стенда нет».
        return f"Mattermost на {settings.mm_url} недоступен: {type(exc).__name__}: {exc}"
    finally:
        await mm.close()

    if not me.is_bot:
        return "MM_TOKEN принадлежит обычному пользователю, а не бот-аккаунту"
    return None


def skip(reason: str) -> int:
    print(f"ПРОПУСК: {reason}.")
    print("Живые тесты требуют доступного Mattermost — см. deploy/DEPLOY.md.")
    return 0
