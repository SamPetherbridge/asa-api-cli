"""Authentication CLI commands."""

from pathlib import Path
from typing import Annotated, Any

import typer
from asa_api_client import AppleSearchAdsClient, Settings
from asa_api_client.exceptions import AppleSearchAdsError, ConfigurationError
from pydantic import ValidationError
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    console,
    error_console,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_info,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="Authentication commands")


@app.command("test")
def test_auth(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file"),
    ] = Path(".env"),
) -> None:
    """Test authentication credentials.

    Loads configuration from environment variables and .env file,
    then attempts to authenticate with the Apple Search Ads API.

    Examples:
        asa auth test
        asa auth test --env-file .env.production
    """
    print_info("Testing Apple Search Ads API credentials...")
    console.print()

    # Try to load settings
    try:
        if env_file and env_file.exists():
            settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
            print_info(f"Loaded configuration from {env_file}")
        else:
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            if env_file:
                print_info("No .env file found, using environment variables only")
    except ValidationError as e:
        # Show what's missing
        table = Table(
            title="Configuration Status",
            show_header=True,
            header_style="header",
            border_style="muted",
        )
        table.add_column("Setting", style="label")
        table.add_column("Status")

        errors_by_field = {err["loc"][0]: err["msg"] for err in e.errors()}

        for field in ["client_id", "team_id", "key_id", "org_id", "private_key", "private_key_path"]:
            if field in errors_by_field:
                table.add_row(f"ASA_{field.upper()}", f"[error]{errors_by_field[field]}[/error]")
            else:
                table.add_row(f"ASA_{field.upper()}", "[success]OK[/success]")

        error_console.print(table)
        error_console.print()
        print_error("Configuration Error", "Missing or invalid settings")
        raise typer.Exit(EXIT_USAGE) from None

    # Display loaded configuration
    table = Table(
        title="Configuration",
        show_header=True,
        header_style="header",
        border_style="muted",
    )
    table.add_column("Setting", style="label")
    table.add_column("Value")

    # Mask client_id
    client_id_display = settings.client_id[:20] + "..." if len(settings.client_id) > 20 else settings.client_id
    table.add_row("ASA_CLIENT_ID", f"[success]{client_id_display}[/success]")
    table.add_row("ASA_TEAM_ID", f"[success]{settings.team_id}[/success]")
    table.add_row("ASA_KEY_ID", f"[success]{settings.key_id}[/success]")
    table.add_row("ASA_ORG_ID", f"[success]{settings.org_id}[/success]")

    if settings.private_key_path:
        table.add_row("ASA_PRIVATE_KEY_PATH", f"[success]{settings.private_key_path}[/success]")
    if settings.private_key:
        table.add_row("ASA_PRIVATE_KEY", "[success]<set>[/success]")

    error_console.print(table)
    error_console.print()

    if settings.private_key and not settings.private_key_path:
        print_warning(
            "ASA_PRIVATE_KEY env var is deprecated. Use ASA_PRIVATE_KEY_PATH to reference a key file instead."
        )
        error_console.print()

    # Try to authenticate
    print_info("Attempting to authenticate...")

    try:
        client = AppleSearchAdsClient.from_env(env_file=env_file)
    except ConfigurationError as e:
        print_error("Configuration Error", e.message)
        raise typer.Exit(EXIT_ERROR) from None

    try:
        # Try to list campaigns to verify authentication works
        with client:
            with spinner("Authenticating with Apple Search Ads API..."):
                campaigns = client.campaigns.list(limit=1)

            print_result_panel(
                "Authentication Successful",
                {
                    "Organization ID": str(client.org_id),
                    "Total Campaigns": str(campaigns.total_results),
                },
            )

    except AppleSearchAdsError as e:
        print_error("API Error", e.message, f"Status code: {e.status_code}" if e.status_code else None)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("show")
def show_config(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file"),
    ] = Path(".env"),
) -> None:
    """Show current authentication configuration.

    Displays configuration loaded from environment variables and .env file.

    Examples:
        asa auth show
        asa auth show --env-file .env.production
    """
    # Try to load settings
    try:
        if env_file and env_file.exists():
            settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
            source = f"from {env_file}"
        else:
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            source = "from environment variables"

        table = Table(
            title=f"Current Configuration ({source})",
            show_header=True,
            header_style="header",
            border_style="muted",
        )
        table.add_column("Setting", style="label")
        table.add_column("Value")

        # Mask client_id partially
        client_id_display = settings.client_id[:20] + "..." if len(settings.client_id) > 20 else settings.client_id
        table.add_row("ASA_CLIENT_ID", client_id_display)
        table.add_row("ASA_TEAM_ID", settings.team_id)
        table.add_row("ASA_KEY_ID", settings.key_id)
        table.add_row("ASA_ORG_ID", str(settings.org_id))

        if settings.private_key_path:
            table.add_row("ASA_PRIVATE_KEY_PATH", str(settings.private_key_path))
        else:
            table.add_row("ASA_PRIVATE_KEY_PATH", "[muted]<not set>[/muted]")

        if settings.private_key:
            table.add_row("ASA_PRIVATE_KEY", "[success]<set>[/success]")
        else:
            table.add_row("ASA_PRIVATE_KEY", "[muted]<not set>[/muted]")

        error_console.print(table)

        if settings.private_key and not settings.private_key_path:
            error_console.print()
            print_warning(
                "ASA_PRIVATE_KEY env var is deprecated. Use ASA_PRIVATE_KEY_PATH to reference a key file instead."
            )

    except ValidationError as e:
        print_error("Configuration Error", "Could not load settings")
        for err in e.errors():
            field = err["loc"][0]
            msg = err["msg"]
            error_console.print(f"  [error]ASA_{str(field).upper()}:[/error] {msg}")
        raise typer.Exit(EXIT_USAGE) from None


ORG_COLUMNS = ["org_id", "org_name", "currency", "role_names", "time_zone"]


@app.command("orgs")
def list_orgs(
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List organizations your credentials can access.

    Shows each org's ID (use as ASA_ORG_ID), name, currency, and your roles.

    Examples:
        asa auth orgs
        asa auth orgs --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching accessible organizations..."):
                acls = client.acls.list()

            if not acls:
                print_warning("No organizations found for these credentials")
                return

            rows: list[dict[str, Any]] = [
                {
                    "org_id": acl.org_id,
                    "org_name": acl.org_name,
                    "currency": acl.currency or "-",
                    "role_names": ", ".join(acl.role_names) or "-",
                    "time_zone": acl.time_zone or "-",
                }
                for acl in acls
            ]

            output_data(
                rows,
                ORG_COLUMNS,
                format,
                title="Accessible organizations",
                column_labels={"org_id": "Org ID", "time_zone": "Time Zone"},
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
