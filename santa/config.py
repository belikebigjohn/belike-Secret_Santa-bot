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

    @classmethod
    def from_env(cls) -> "Config":
        file_values = local_env()

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, file_values.get(name, default))

        token = value("BOT_TOKEN")
        if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
            raise RuntimeError("Вставьте Telegram-токен в BOT_TOKEN файла .env")
        ids = frozenset(int(part.strip()) for part in value("OWNER_IDS").split(",") if part.strip())
        return cls(token, value("DATABASE_URL", "sqlite:///santa.db"), ids)
