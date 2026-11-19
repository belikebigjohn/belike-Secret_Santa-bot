"""Run migrations using the connection supplied by the application."""

from alembic import context

from santa.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
