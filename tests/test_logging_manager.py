from pathlib import Path

from tradingagents.utils.logging_manager import TradingAgentsLogger


def _config(log_dir: str):
    return {
        "level": "INFO",
        "format": {
            "console": "%(message)s",
            "file": "%(message)s",
        },
        "handlers": {
            "console": {"enabled": False, "colored": False, "level": "INFO"},
            "file": {
                "enabled": True,
                "level": "INFO",
                "max_size": "1MB",
                "backup_count": 1,
                "directory": log_dir,
            },
            "error": {
                "enabled": True,
                "level": "WARNING",
                "directory": log_dir,
                "filename": f"{log_dir}/error.log",
                "max_size": "1MB",
                "backup_count": 1,
            },
            "structured": {
                "enabled": False,
                "level": "INFO",
                "directory": log_dir,
            },
        },
        "loggers": {},
        "docker": {"enabled": False, "stdout_only": True},
    }


def test_unwritable_log_directory_falls_back_to_temp(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(
        "tradingagents.utils.logging_manager.tempfile.gettempdir",
        lambda: str(fallback.parent),
    )

    logger = TradingAgentsLogger(_config("/proc/a-tradingagents/logs"))

    configured = Path(logger.config["handlers"]["file"]["directory"])
    assert configured == fallback.parent / "a-tradingagents-logs"
    assert configured.is_dir()
    assert logger.config["handlers"]["error"]["filename"] == "error.log"
