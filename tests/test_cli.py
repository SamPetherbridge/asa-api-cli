"""Tests for the CLI application."""

from typer.testing import CliRunner

from asa_api_cli import app

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


def test_root_help_lists_lookup_commands() -> None:
    """Root help should advertise the v5 lookup command groups."""
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
