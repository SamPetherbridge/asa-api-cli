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
    assert "orgs" in result.stdout


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


def test_ads_help() -> None:
    """Test ads subcommand help."""
    result = runner.invoke(app, ["ads", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "get" in result.stdout
    assert "create" in result.stdout
    assert "update" in result.stdout
    assert "delete" in result.stdout


def test_ads_create_creative_set_rejected() -> None:
    """CREATIVE_SET is not supported by the create command (no asset selection)."""
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


def test_budget_orders_help() -> None:
    """Test budget-orders subcommand help."""
    result = runner.invoke(app, ["budget-orders", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "get" in result.stdout
