"""Container health probe: database access and a recently running delivery worker."""

import os
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def healthy(database_url: str, worker_file: str = "", max_age_seconds: int = 45) -> bool:
    if worker_file:
        try:
            age = time.time() - Path(worker_file).stat().st_mtime
        except OSError:
            return False
        if age > max_age_seconds:
            return False
    engine = None
    try:
        engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}
                               if database_url.startswith("postgresql") else {})
        with engine.connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1
    except SQLAlchemyError:
        return False
    finally:
        if engine is not None:
            engine.dispose()


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not healthy(database_url, os.environ.get("WORKER_HEALTH_FILE", "")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
