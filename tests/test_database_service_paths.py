from pathlib import Path

from app.services.database_service import DatabaseService


def test_database_service_falls_back_when_data_directory_is_unwritable(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "app.services.database_service.settings.TRADINGAGENTS_DATA_DIR",
        "/proc/a-tradingagents-data",
    )
    monkeypatch.setattr(
        "app.services.database_service.tempfile.gettempdir",
        lambda: str(tmp_path),
    )

    service = DatabaseService()

    assert Path(service.backup_dir) == tmp_path / "a-tradingagents-data" / "backups"
    assert Path(service.export_dir) == tmp_path / "a-tradingagents-data" / "exports"
    assert Path(service.backup_dir).is_dir()
    assert Path(service.export_dir).is_dir()
