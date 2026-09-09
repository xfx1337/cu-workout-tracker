"""Клиент авторизации.

По ТЗ: отправляем telegram user id на ручку ЦУ, в ответ получаем student id.
Пока ручки нет — работает заглушка (см. settings.auth_is_stub).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StudentIdentity:
    student_id: str
    full_name: str | None = None
    email: str | None = None


async def resolve_student(tg_user_id: int, tg_username: str | None = None) -> StudentIdentity | None:
    """Вернёт личность студента или None, если ЦУ его не знает."""
    if settings.auth_is_stub:
        # ЗАГЛУШКА: пускаем всех. Убрать, когда появится реальная ручка.
        return StudentIdentity(
            student_id=f"stub-{tg_user_id}",
            full_name=f"@{tg_username}" if tg_username else None,
        )

    headers = {}
    if settings.auth_api_token:
        headers["Authorization"] = f"Bearer {settings.auth_api_token}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                settings.auth_api_url, params={"telegram_id": tg_user_id}, headers=headers
            )
    except httpx.HTTPError as exc:
        log.warning("auth request failed: %s", exc)
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        log.warning("auth returned %s: %s", resp.status_code, resp.text[:200])
        return None

    data = resp.json()
    student_id = data.get("student_id") or data.get("id")
    if not student_id:
        return None

    return StudentIdentity(
        student_id=str(student_id),
        full_name=data.get("full_name") or data.get("name"),
        email=data.get("email"),
    )


def login_url(tg_user_id: int) -> str | None:
    if not settings.auth_login_url:
        return None
    sep = "&" if "?" in settings.auth_login_url else "?"
    return f"{settings.auth_login_url}{sep}telegram_id={tg_user_id}"
