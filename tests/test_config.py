from santa import config


def test_private_env_and_process_override(tmp_path, monkeypatch):
    package = tmp_path / "santa"
    package.mkdir()
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123456:test_token\nDATABASE_URL=sqlite:///local.db\nOWNER_IDS=123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))
    for key in ("BOT_TOKEN", "DATABASE_URL", "OWNER_IDS"):
        monkeypatch.delenv(key, raising=False)
    result = config.Config.from_env()
    assert result.database_url == "sqlite:///local.db"
    assert result.owner_ids == {123}
    monkeypatch.setenv("DATABASE_URL", "sqlite:///override.db")
    assert config.Config.from_env().database_url == "sqlite:///override.db"
