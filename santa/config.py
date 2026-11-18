import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    token: str
    database_url: str
    owner_ids: frozenset[int]

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "")
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        ids = frozenset(int(part.strip()) for part in os.getenv("OWNER_IDS", "").split(",") if part.strip())
        return cls(token, os.getenv("DATABASE_URL", "sqlite:///santa.db"), ids)
