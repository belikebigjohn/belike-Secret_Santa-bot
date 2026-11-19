import os
import time

from santa import healthcheck
from santa.healthcheck import healthy


def test_healthcheck_requires_fresh_worker_and_database(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'health.db'}"
    worker_file = tmp_path / "worker"
    assert not healthy(database_url, str(worker_file))
    worker_file.touch()
    assert healthy(database_url, str(worker_file))
    old = time.time() - 120
    os.utime(worker_file, (old, old))
    assert not healthy(database_url, str(worker_file))


def test_healthcheck_reads_local_env_when_database_url_is_unset(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'health.db'}"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(healthcheck, "local_env", lambda: {"DATABASE_URL": database_url})
    healthcheck.main()
