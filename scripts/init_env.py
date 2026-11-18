"""Create a private local configuration once; never overwrite a filled .env."""
from pathlib import Path
from secrets import token_urlsafe

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    print(".env уже существует; оставляю его без изменений")
else:
    password = token_urlsafe(32)
    target.write_text(
        "BOT_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE\n"
        "DATABASE_URL=sqlite:///santa.db\n"
        "OWNER_IDS=\n"
        "LOG_LEVEL=INFO\n"
        f"POSTGRES_PASSWORD={password}\n",
        encoding="utf-8",
    )
    print(".env создан. Вставьте токен после BOT_TOKEN= без кавычек")
