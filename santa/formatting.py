"""Telegram-safe formatting for member references."""

from html import escape


def profile_link(user_id: int, name: str) -> str:
    """Link a known Telegram user by numeric ID, even without a public username."""
    return f'<a href="tg://user?id={int(user_id)}">{escape(name)}</a>'
