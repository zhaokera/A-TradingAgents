import json
import os
import shutil
import site
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import tomli as tomllib

import app.services.holdings_cli as holdings_cli_module
from app.services.holdings_cli import CLIError, _validate_cli_mongo_configuration


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_installable_holdings_cli_contract():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    setuptools = config["tool"]["setuptools"]

    assert any(dependency.lower().startswith("typer") for dependency in project["dependencies"])
    assert project["scripts"]["holdings"] == "cli.agent:holdings_main"
    assert {"main", "holdings_cli"}.issubset(set(setuptools["py-modules"]))
    assert {"tradingagents*", "app*", "cli*"}.issubset(
        set(setuptools["packages"]["find"]["include"])
    )
    assert "LICENSE" in setuptools["package-data"]["app"]


def test_built_wheel_runs_both_cli_entrypoints_outside_checkout(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE", "main.py", "holdings_cli.py"):
        shutil.copy2(ROOT / filename, source_root / filename)
    for package_name in ("app", "tradingagents", "cli"):
        shutil.copytree(
            ROOT / package_name,
            source_root / package_name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    dist_dir = tmp_path / "dist"
    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--installer",
            "uv",
            "--outdir",
            str(dist_dir),
            str(source_root),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build_result.returncode == 0, build_result.stderr
    wheel_path = next(dist_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel_path) as archive:
        wheel_files = set(archive.namelist())
    assert "holdings_cli.py" in wheel_files
    assert "app/services/holdings_cli.py" in wheel_files
    assert "app/LICENSE" in wheel_files

    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    installed_python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [str(installed_python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    installed_site = subprocess.run(
        [str(installed_python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    ).stdout.strip()
    environment["PYTHONPATH"] = os.pathsep.join(
        [installed_site, site.getsitepackages()[0]]
    )
    environment["A_TRADINGAGENTS_SESSION_FILE"] = str(outside_dir / "session.json")
    environment.pop("A_TRADINGAGENTS_PASSWORD", None)
    for key in (
        "MONGODB_HOST",
        "MONGODB_PORT",
        "MONGODB_DATABASE",
        "MONGODB_USERNAME",
        "MONGODB_PASSWORD",
        "MONGODB_URI",
    ):
        environment.pop(key, None)

    module_help = subprocess.run(
        [str(installed_python), "-m", "holdings_cli", "--help"],
        cwd=outside_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    console_help = subprocess.run(
        [str(bin_dir / "holdings"), "--help"],
        cwd=outside_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert module_help.returncode == 0
    assert console_help.returncode == 0
    assert "持仓数据 JSON CLI" in module_help.stdout
    assert "持仓数据 JSON CLI" in console_help.stdout

    path_probe = subprocess.run(
        [
            str(installed_python),
            "-c",
            "import app, holdings_cli, json; "
            "print(json.dumps({'app': app.__file__, 'module': holdings_cli.__file__}))",
        ],
        cwd=outside_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert path_probe.returncode == 0, path_probe.stderr
    loaded_paths = json.loads(path_probe.stdout)
    assert str(venv_dir) in loaded_paths["app"]
    assert str(venv_dir) in loaded_paths["module"]

    missing_config = subprocess.run(
        [str(bin_dir / "holdings"), "list"],
        cwd=outside_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert missing_config.returncode == 3
    assert missing_config.stdout == ""
    assert json.loads(missing_config.stderr)["error"]["code"] == "authentication_required"


def test_cli_mongo_configuration_accepts_repo_env_file(tmp_path):
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "services" / "holdings_cli.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "MONGODB_HOST=localhost\nMONGODB_DATABASE=tradingagentscn\n",
        encoding="utf-8",
    )

    result = _validate_cli_mongo_configuration(cwd=tmp_path, environ={})

    assert result == {
        "source": "cwd_env_file",
        "path": str(tmp_path / ".env"),
        "expected_database": "tradingagentscn",
    }


def test_cli_mongo_configuration_rejects_arbitrary_env_file(tmp_path):
    (tmp_path / ".env").write_text(
        "MONGODB_HOST=localhost\nMONGODB_DATABASE=wrong\n",
        encoding="utf-8",
    )

    with pytest.raises(CLIError) as exc_info:
        _validate_cli_mongo_configuration(cwd=tmp_path, environ={})

    assert exc_info.value.code == "mongo_config_required"


def test_cli_mongo_configuration_accepts_explicit_host_and_database(tmp_path):
    result = _validate_cli_mongo_configuration(
        cwd=tmp_path,
        environ={"MONGODB_HOST": "127.0.0.1", "MONGODB_DATABASE": "tradingagentscn"},
    )

    assert result == {
        "source": "process_environment",
        "path": None,
        "expected_database": "tradingagentscn",
    }


def test_cli_mongo_configuration_fails_closed_outside_repo(tmp_path):
    with pytest.raises(CLIError) as exc_info:
        _validate_cli_mongo_configuration(cwd=tmp_path, environ={})

    assert exc_info.value.code == "mongo_config_required"
    assert "MONGODB_HOST" in exc_info.value.message
    assert "MONGODB_DATABASE" in exc_info.value.message


def test_cli_mongo_configuration_does_not_accept_only_one_env_value(tmp_path):
    environment = dict(os.environ)
    environment.clear()
    environment["MONGODB_HOST"] = "127.0.0.1"

    with pytest.raises(CLIError) as exc_info:
        _validate_cli_mongo_configuration(cwd=tmp_path, environ=environment)

    assert exc_info.value.code == "mongo_config_required"


def test_cli_help_ignores_malformed_env_file_in_unrelated_directory(tmp_path):
    (tmp_path / ".env").write_text("MONGODB_PORT=not-a-number\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "holdings_cli", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0
    assert "持仓数据 JSON CLI" in result.stdout
    assert "ValidationError" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "message_fragment"),
    [
        (["get"], "--code"),
        (["trades", "--limit", "0"], "--limit"),
    ],
)
def test_cli_argument_errors_are_structured_json(arguments, message_fragment):
    result = subprocess.run(
        [sys.executable, "-m", "holdings_cli", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_cli_arguments"
    assert message_fragment in payload["error"]["message"]
    assert "Usage:" not in result.stderr


def test_process_environment_connection_values_ignore_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MONGODB_HOST=wrong\nMONGODB_DATABASE=wrong\nMONGODB_PORT=not-a-number\n",
        encoding="utf-8",
    )

    values = holdings_cli_module._mongo_connection_values(
        {
            "source": "process_environment",
            "path": str(env_file),
            "expected_database": "explicit_name",
        },
        environ={
            "MONGODB_HOST": "127.0.0.1",
            "MONGODB_DATABASE": "explicit_name",
        },
    )

    assert values["MONGODB_HOST"] == "127.0.0.1"
    assert values["MONGODB_DATABASE"] == "explicit_name"
    assert "MONGODB_PORT" not in values


def test_repo_env_docker_mongo_host_maps_to_loopback_for_host_cli():
    resolved = holdings_cli_module._resolve_cli_mongo_host(
        "mongodb",
        configuration={"source": "cwd_env_file"},
        environ={},
    )

    assert resolved == "127.0.0.1"


def test_repo_env_docker_mongo_host_stays_on_service_dns_inside_container():
    resolved = holdings_cli_module._resolve_cli_mongo_host(
        "mongodb",
        configuration={"source": "cwd_env_file"},
        environ={"DOCKER_CONTAINER": "true"},
    )

    assert resolved == "mongodb"


def test_explicit_mongo_host_is_not_rewritten():
    resolved = holdings_cli_module._resolve_cli_mongo_host(
        "mongodb",
        configuration={"source": "process_environment"},
        environ={},
    )

    assert resolved == "mongodb"


def test_connect_cli_database_uses_loopback_for_repo_docker_host(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MONGODB_HOST=mongodb\n"
        "MONGODB_PORT=27017\n"
        "MONGODB_DATABASE=tradingagentscn\n",
        encoding="utf-8",
    )
    captured = {}

    class FakeClient:
        def __getitem__(self, name):
            return type("FakeDatabase", (), {"name": name})()

    def fake_mongo_client(**options):
        captured.update(options)
        return FakeClient()

    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    monkeypatch.setattr(holdings_cli_module, "MongoClient", fake_mongo_client)

    database = holdings_cli_module._connect_cli_database(
        {
            "source": "cwd_env_file",
            "path": str(env_file),
            "expected_database": "tradingagentscn",
        }
    )

    assert database.name == "tradingagentscn"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 27017


def test_get_database_uses_validated_connection_and_verifies_resolved_name(monkeypatch):
    class FakeDatabase:
        name = "explicit_name"

    configuration = {
        "source": "process_environment",
        "path": None,
        "expected_database": "explicit_name",
    }
    captured = []
    monkeypatch.setattr(
        holdings_cli_module,
        "_validate_cli_mongo_configuration",
        lambda: configuration,
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_connect_cli_database",
        lambda value: captured.append(value) or FakeDatabase(),
    )

    database = holdings_cli_module._get_database()

    assert database.name == "explicit_name"
    assert captured == [configuration]


def test_get_database_rejects_resolved_database_mismatch(monkeypatch):
    class FakeDatabase:
        name = "explicit_name_v0_local"

    monkeypatch.setattr(
        holdings_cli_module,
        "_validate_cli_mongo_configuration",
        lambda: {
            "source": "process_environment",
            "path": None,
            "expected_database": "explicit_name",
        },
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "_connect_cli_database",
        lambda _configuration: FakeDatabase(),
    )

    with pytest.raises(CLIError) as exc_info:
        holdings_cli_module._get_database()

    assert exc_info.value.code == "mongo_config_mismatch"
