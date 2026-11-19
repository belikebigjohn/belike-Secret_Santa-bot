import os
import time

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
