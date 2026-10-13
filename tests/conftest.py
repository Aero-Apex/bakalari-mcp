"""Every test runs with its own settings file and data folder, never the owner's real ones."""

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BAKALARI_CONFIG", str(tmp_path / "isolated-config.env"))
    monkeypatch.setenv("BAKALARI_DATA_DIR", str(tmp_path / "isolated-data"))
    yield
