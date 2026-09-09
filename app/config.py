from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mattermost (TiMe) ---
    mm_url: str = ""                      # https://time.example.ru
    mm_token: str = ""                    # personal access token бот-аккаунта
    mm_team: str = ""                     # имя команды; пусто = первая доступная
    # Кнопки-attachments требуют, чтобы сервер Mattermost сам ходил к боту по HTTP.
    # Пока такого адреса нет, выбор делается реакциями-эмодзи по WebSocket.
    mm_public_url: str = ""               # адрес нашего бота, виден серверу Mattermost
    mm_listen_port: int = 8080

    # хранится строкой: pydantic-settings пытается парсить list[str] из env как JSON
    admin_usernames_raw: str = Field(default="", validation_alias="ADMIN_USERNAMES")

    database_url: str = "sqlite+aiosqlite:///./workout.db"
    tz_name: str = "Europe/Moscow"

    # Авторизация. Пустой auth_api_url => режим заглушки.
    auth_api_url: str = ""
    auth_api_token: str = ""
    auth_login_url: str = ""

    default_capacity: int = 25
    reminder_minutes_before: int = 30
    cancel_deadline_minutes: int = 0
    no_show_limit: int = 3
    silence_grace_hours: int = 24
    self_reported_absence_counts: bool = False
    silence_counts: bool = True

    @property
    def admin_usernames(self) -> list[str]:
        return [x.strip().lstrip("@").lower() for x in self.admin_usernames_raw.split(",") if x.strip()]

    @property
    def use_buttons(self) -> bool:
        """Настоящие кнопки возможны, только если Mattermost может достучаться до нас."""
        return bool(self.mm_public_url)

    @property
    def auth_is_stub(self) -> bool:
        return not self.auth_api_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
