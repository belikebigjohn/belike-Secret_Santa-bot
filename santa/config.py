import os
from dataclasses import dataclass
from pathlib import Path


def local_env() -> dict[str, str]:
    """Read simple KEY=VALUE entries without executing shell syntax."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Config:
    token: str
    database_url: str
    owner_ids: frozenset[int]
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        file_values = local_env()

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, file_values.get(name, default))

        token = value("BOT_TOKEN").strip()
        if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
            raise RuntimeError("Вставьте Telegram-токен в BOT_TOKEN файла .env")
        try:
            ids = frozenset(int(part.strip()) for part in value("OWNER_IDS").split(",") if part.strip())
        except ValueError as exc:
            raise RuntimeError("OWNER_IDS должен содержать Telegram ID через запятую") from exc
        if not ids or any(user_id <= 0 for user_id in ids):
            raise RuntimeError("Укажите хотя бы один положительный Telegram ID в OWNER_IDS")
        level = value("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError("LOG_LEVEL должен быть DEBUG, INFO, WARNING, ERROR или CRITICAL")
        return cls(token, value("DATABASE_URL", "sqlite:///santa.db"), ids, level)
