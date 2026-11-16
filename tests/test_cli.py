"""Tests for the CLI application."""

import importlib.metadata
import io
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import hatchling.build
import pytest
import typer
from asa_api_client.exceptions import PartialFailureError
from asa_api_client.v1.models.base import V1Page, V1Pagination
from asa_api_client.v1.models.product_pages import ProductPageDetails
from packaging.requirements import Requirement
from rich.console import Console
from typer.testing import CliRunner

from asa_api_cli import app, product_pages, utils
from asa_api_cli.main import _register_analyze_command
from asa_api_cli.optimize import _get_negative_analysis_agent
from asa_api_cli.translate import TranslateSettings, get_translation_agent

runner = CliRunner()


def test_version() -> None:
    """Test --version flag."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "asa-api-cli" in result.stdout


def test_help() -> None:
    """Test --help flag."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "campaigns" in result.stdout
    assert "ad-groups" in result.stdout
    assert "keywords" in result.stdout
    assert "reports" in result.stdout
    assert "recommendations" in result.stdout
    assert "suggestions" in result.stdout
    assert "change-history" in result.stdout


def test_campaigns_help() -> None:
    """Test campaigns subcommand help."""
    result = runner.invoke(app, ["campaigns", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout


def test_auth_help() -> None:
    """Test auth subcommand help."""
    result = runner.invoke(app, ["auth", "--help"])
    assert result.exit_code == 0
    assert "show" in result.stdout
    assert "test" in result.stdout
    assert "accounts" in result.stdout
    assert "orgs" in result.stdout


def test_root_help_lists_lookup_commands() -> None:
    """Root help should advertise the Platform API v1 lookup command groups."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "apps" in result.stdout
    assert "geo" in result.stdout
    assert "countries" in result.stdout


def test_apps_help() -> None:
    """Test apps subcommand help."""
    result = runner.invoke(app, ["apps", "--help"])
    assert result.exit_code == 0
    assert "search" in result.stdout
    assert "get" in result.stdout
    assert "eligibilities" in result.stdout


def test_geo_help() -> None:
    """Test geo subcommand help."""
    result = runner.invoke(app, ["geo", "--help"])
    assert result.exit_code == 0
    assert "search" in result.stdout


def test_countries_help() -> None:
    """Test countries subcommand help."""
    result = runner.invoke(app, ["countries", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout


def test_ads_help() -> None:
    """Test ads subcommand help."""
    result = runner.invoke(app, ["ads", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "get" in result.stdout
    assert "create" in result.stdout
    assert "update" in result.stdout
    assert "delete" in result.stdout
    assert "creatives" in result.stdout


def test_ads_create_creative_set_rejected() -> None:
    """CREATIVE_SET is not supported by the create command (retired in Platform API v1)."""
    result = runner.invoke(
        app,
        ["ads", "create", "-c", "1", "-a", "2", "-n", "x", "-t", "CREATIVE_SET"],
    )
    assert result.exit_code != 0


def test_ads_create_custom_page_requires_product_page() -> None:
    """CUSTOM_PRODUCT_PAGE without --product-page should fail before any API call."""
    result = runner.invoke(
        app,
        ["ads", "create", "-c", "1", "-a", "2", "-n", "x", "-t", "CUSTOM_PRODUCT_PAGE"],
    )
    assert result.exit_code != 0


def test_ads_create_default_page_rejects_product_page() -> None:
    """DEFAULT_PRODUCT_PAGE must not be given a --product-page."""
    result = runner.invoke(
        app,
        ["ads", "create", "-c", "1", "-a", "2", "-n", "x", "-t", "DEFAULT_PRODUCT_PAGE", "-p", "pp1"],
    )
    assert result.exit_code != 0


def test_ads_create_dry_run_succeeds_offline() -> None:
    """A valid --dry-run create previews the payload and exits 0 without an API call."""
    result = runner.invoke(
        app,
        ["ads", "create", "-c", "1", "-a", "2", "-n", "Test ad", "-t", "DEFAULT_PRODUCT_PAGE", "--dry-run"],
    )
    assert result.exit_code == 0


def test_ads_update_requires_a_field() -> None:
    """update with neither --name nor --status should fail before any API call."""
    result = runner.invoke(app, ["ads", "update", "789", "-c", "1", "-a", "2"])
    assert result.exit_code != 0


def test_product_pages_help() -> None:
    """Test product-pages subcommand help."""
    result = runner.invoke(app, ["product-pages", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "locales" in result.stdout
    assert "app-locales" in result.stdout


def test_budget_orders_help() -> None:
    """Test budget-orders subcommand help."""
    result = runner.invoke(app, ["budget-orders", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "get" in result.stdout


def test_root_help_lists_analyze_and_v1_smoke() -> None:
    """Root help should expose asa-api-client's analyze and v1-smoke commands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.stdout
    assert "v1-smoke" in result.stdout


def test_v1_smoke_help() -> None:
    """v1-smoke subcommand help should work without optional pandas/xlsxwriter extras."""
    result = runner.invoke(app, ["v1-smoke", "--help"])
    assert result.exit_code == 0
    assert "Platform API v1" in result.stdout


def test_analyze_missing_extra_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pandas/xlsxwriter are absent, analyze prints an install hint and exits 1."""
    monkeypatch.setitem(sys.modules, "asa_api_client.cli.analyze", None)
    temp_app = typer.Typer()
    _register_analyze_command(temp_app)

    result = runner.invoke(temp_app, ["--period", "30d", "-o", "out.xlsx"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "asa-api-cli[analyze]" in combined


def test_analyze_registered_when_extra_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When asa_api_client.cli.analyze is importable, its command is mounted directly."""
    fake_mod = ModuleType("asa_api_client.cli.analyze")

    def fake_analyze(period: str = "30d") -> None:
        """Generate an Excel performance analysis workbook."""
        print(f"analyzed:{period}")

    fake_mod.analyze = fake_analyze  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asa_api_client.cli.analyze", fake_mod)

    temp_app = typer.Typer()
    _register_analyze_command(temp_app)

    result = runner.invoke(temp_app, ["--period", "90d"])
    assert result.exit_code == 0
    assert "analyzed:90d" in result.stdout


def test_auth_missing_private_key_model_error(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Model-level Settings ValidationError (empty loc) should not raise IndexError."""
    monkeypatch.setenv("ASA_CLIENT_ID", "SEARCHADS.test-client-id")
    monkeypatch.setenv("ASA_TEAM_ID", "TESTTEAM")
    monkeypatch.setenv("ASA_KEY_ID", "TESTKEY")
    monkeypatch.setenv("ASA_AD_ACCOUNT_ID", "123456")
    monkeypatch.delenv("ASA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("ASA_PRIVATE_KEY_PATH", raising=False)

    missing_env = f"{tmp_path}/nonexistent.env"

    show_res = runner.invoke(app, ["auth", "show", "--env-file", missing_env])
    assert show_res.exit_code == 2
    assert "private_key" in (show_res.stdout + (show_res.stderr or ""))

    test_res = runner.invoke(app, ["auth", "test", "--env-file", missing_env])
    assert test_res.exit_code == 2
    assert "private_key" in (test_res.stdout + (test_res.stderr or ""))


def test_handle_api_error_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """PartialFailureError per-item details should be rendered by handle_api_error."""
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120)
    monkeypatch.setattr(utils, "error_console", test_console)

    err = PartialFailureError(
        "Bulk keyword update failed",
        status_code=200,
        details=[
            {
                "field": "bidAmount",
                "messageCode": "INVALID_CURRENCY",
                "message": "Currency mismatch",
            }
        ],
    )
    utils.handle_api_error(err)
    rendered = buf.getvalue()
    assert "Bulk keyword update failed" in rendered
    assert "bidAmount: Currency mismatch (INVALID_CURRENCY)" in rendered


def test_product_pages_passes_adam_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """product-pages list must filter ProductPagesResource by adamId."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.product_pages.query.return_value = V1Page[ProductPageDetails](
        result=[ProductPageDetails(id="pp-1", adamId="987654321", name="Holiday", state="AVAILABLE")],
        pagination=V1Pagination(totalCount=1, offset=0, limit=100),
    )
    monkeypatch.setattr(product_pages, "get_client", lambda: mock_client)

    result = runner.invoke(app, ["product-pages", "list", "--adam-id", "987654321", "--format", "json"])
    assert result.exit_code == 0
    mock_client.product_pages.query.assert_called_once()
    passed_query = mock_client.product_pages.query.call_args.args[0]
    payload = passed_query.to_payload()
    assert {"field": "adamId", "operator": "EQUALS", "value": 987654321} in payload["filters"]
    assert "pp-1" in result.stdout


def test_pydantic_ai_agents_construct() -> None:
    """Both translation and negative-keyword agents construct cleanly on pydantic-ai v2."""
    settings = TranslateSettings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="sk-ant-test-key",
        gemini_api_key="gemini-test-key",
    )
    for provider in ("anthropic", "gemini"):
        t_agent = get_translation_agent(settings=settings, provider=provider)
        assert t_agent is not None
        n_agent = _get_negative_analysis_agent(settings=settings, provider=provider)
        assert n_agent is not None


def test_pyproject_rejects_conflicting_client_releases() -> None:
    """Issue #74: Dependency bounds exclude asa-api-client<=1.0.0 that claimed the `asa` script."""
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"] == {"asa": "asa_api_cli:app"}

    core_reqs = [Requirement(r) for r in pyproject["project"]["dependencies"]]
    client_core = next(r for r in core_reqs if r.name == "asa-api-client")
    assert not client_core.specifier.contains("0.6.1")
    assert not client_core.specifier.contains("1.0.0")
    assert client_core.specifier.contains("1.0.1")

    analyze_reqs = [Requirement(r) for r in pyproject["project"]["optional-dependencies"]["analyze"]]
    client_analyze = next(r for r in analyze_reqs if r.name == "asa-api-client")
    assert "cli" in client_analyze.extras
    assert not client_analyze.specifier.contains("1.0.0")
    assert client_analyze.specifier.contains("1.0.1")

    client_eps = [
        ep.name
        for ep in importlib.metadata.distribution("asa-api-client").entry_points
        if ep.group == "console_scripts"
    ]
    assert "asa" not in client_eps
    assert "asa-client" in client_eps


def _assert_installed_asa_surface(asa_bin: Path, outside_dir: Path, clean_env: dict[str, str]) -> None:
    """Verify executable identity and core CLI/analyze help from outside the source checkout."""
    ver = subprocess.run([str(asa_bin), "--version"], cwd=outside_dir, env=clean_env, capture_output=True, text=True)
    assert ver.returncode == 0, ver.stderr
    assert ver.stdout.startswith("asa-api-cli ")
    assert "(asa-api-client " in ver.stdout

    root_help = subprocess.run([str(asa_bin), "--help"], cwd=outside_dir, env=clean_env, capture_output=True, text=True)
    assert root_help.returncode == 0, root_help.stderr
    for expected_cmd in ("reports", "auth", "analyze", "v1-smoke"):
        assert expected_cmd in root_help.stdout

    rep_help = subprocess.run(
        [str(asa_bin), "reports", "export", "--help"],
        cwd=outside_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert rep_help.returncode == 0, rep_help.stderr
    assert "--app" in rep_help.stdout

    auth_help = subprocess.run(
        [str(asa_bin), "auth", "--help"],
        cwd=outside_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert auth_help.returncode == 0, auth_help.stderr
    assert "accounts" in auth_help.stdout

    analyze_help = subprocess.run(
        [str(asa_bin), "analyze", "--help"],
        cwd=outside_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert analyze_help.returncode == 0, analyze_help.stderr
    assert "--period" in analyze_help.stdout
    assert "--output" in analyze_help.stdout


def _uv_pip_install(uv_bin: str, venv_python: Path, outside_dir: Path, args: list[str]) -> None:
    """Install packages into an isolated venv, preferring uv's offline wheel cache first."""
    base_cmd = [uv_bin, "pip", "install", "--python", str(venv_python)]
    offline = subprocess.run([*base_cmd, "--offline", *args], cwd=outside_dir, capture_output=True, text=True)
    if offline.returncode == 0:
        return
    online = subprocess.run([*base_cmd, *args], cwd=outside_dir, capture_output=True, text=True)
    assert online.returncode == 0, f"uv pip install failed:\n{offline.stderr}\n{online.stderr}"


def test_isolated_install_with_analyze_extra_and_client_reinstall_preserves_asa(tmp_path: Path) -> None:
    """Issue #74: Fresh isolated install + reinstalling asa-api-client preserves the `asa` command."""
    uv_bin = shutil.which("uv")
    assert uv_bin is not None, "uv is required to run isolated installation smoke tests"

    repo_root = Path(__file__).resolve().parents[1]
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    venv_dir = tmp_path / "isolated-venv"

    prev_cwd = Path.cwd()
    try:
        os.chdir(repo_root)
        wheel_name = hatchling.build.build_wheel(str(wheels_dir))
    finally:
        os.chdir(prev_cwd)
    wheel_path = wheels_dir / wheel_name
    assert wheel_path.is_file()

    subprocess.run(
        [uv_bin, "venv", "--no-project", "--python", sys.executable, str(venv_dir)],
        cwd=outside_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    exe_suffix = ".exe" if os.name == "nt" else ""
    venv_python = bin_dir / f"python{exe_suffix}"
    asa_bin = bin_dir / f"asa{exe_suffix}"
    asa_client_bin = bin_dir / f"asa-client{exe_suffix}"

    # 1. Fresh install of built asa-api-cli[analyze] wheel into isolated venv
    _uv_pip_install(uv_bin, venv_python, outside_dir, [f"asa-api-cli[analyze] @ {wheel_path.as_uri()}"])

    clean_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    clean_env["PYTHONNOUSERSITE"] = "1"
    clean_env["NO_COLOR"] = "1"

    assert asa_bin.is_file()
    assert asa_client_bin.is_file()
    _assert_installed_asa_surface(asa_bin, outside_dir, clean_env)

    client_help = subprocess.run(
        [str(asa_client_bin), "--help"],
        cwd=outside_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert client_help.returncode == 0, client_help.stderr
    assert "analyze" in client_help.stdout
    assert "smoke" in client_help.stdout

    # 2. Reinstall supported asa-api-client dependency and verify `asa` was NOT overwritten
    _uv_pip_install(
        uv_bin,
        venv_python,
        outside_dir,
        ["--reinstall-package", "asa-api-client", "asa-api-client[cli]>=1.0.1"],
    )
    _assert_installed_asa_surface(asa_bin, outside_dir, clean_env)
