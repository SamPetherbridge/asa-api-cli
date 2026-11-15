"""Main CLI application."""

from typing import Annotated

import typer
from asa_api_client.cli import v1_smoke

from asa_api_cli import (
    ad_groups,
    ads,
    apps,
    auth,
    brand,
    budget_orders,
    campaigns,
    countries,
    geo,
    impression_share,
    keywords,
    optimize,
    product_pages,
    reports,
    translate,
)
from asa_api_cli.utils import EXIT_ERROR, cli_state, error_console, init_consoles, print_error

app = typer.Typer(
    name="asa",
    help="Apple Search Ads API CLI - Manage campaigns, ad groups, keywords, and reports.",
    rich_markup_mode="rich",
)


def _register_analyze_command(target_app: typer.Typer) -> None:
    """Register ``asa analyze`` from asa-api-client, or a clean install-hint fallback."""
    try:
        from asa_api_client.cli.analyze import analyze as analyze_command
    except ImportError:

        @target_app.command(
            "analyze",
            help="Generate an Excel performance analysis workbook (requires [analyze] extra)",
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )
        def _analyze_missing_extra() -> None:
            """Generate an Excel performance analysis workbook.

            Requires optional dependencies (pandas and xlsxwriter).
            """
            print_error(
                "Missing Optional Dependencies",
                "The 'analyze' command requires pandas and xlsxwriter.",
                details=('Install with: pip install "asa-api-cli[analyze]" or uv tool install "asa-api-cli[analyze]"'),
            )
            raise typer.Exit(EXIT_ERROR)

        return

    target_app.command(
        "analyze",
        help="Generate an Excel performance analysis workbook",
    )(analyze_command)


# Register sub-commands
app.add_typer(auth.app, name="auth", help="Authentication commands")
app.add_typer(brand.app, name="brand", help="Create brand protection campaigns")
app.add_typer(campaigns.app, name="campaigns", help="Manage campaigns")
app.add_typer(ad_groups.app, name="ad-groups", help="Manage ad groups")
app.add_typer(ads.app, name="ads", help="View and manage ads (creatives) within ad groups")
app.add_typer(keywords.app, name="keywords", help="Manage keywords")
app.add_typer(product_pages.app, name="product-pages", help="View custom product pages (CPP)")
app.add_typer(apps.app, name="apps", help="Search the App Store for advertisable apps")
app.add_typer(geo.app, name="geo", help="Search geographic locations for targeting")
app.add_typer(countries.app, name="countries", help="List supported countries and regions")
app.add_typer(budget_orders.app, name="budget-orders", help="View budget orders")
app.add_typer(reports.app, name="reports", help="Generate reports")
app.add_typer(optimize.app, name="optimize", help="Optimization tools")
app.add_typer(impression_share.app, name="impression-share", help="Impression share analysis")
app.add_typer(translate.app, name="translate", help="Translate keywords to multiple languages")
_register_analyze_command(app)
app.command(
    "v1-smoke",
    help="Run a read-only smoke test against the Apple Ads Platform API v1",
)(v1_smoke.command)


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        from importlib.metadata import PackageNotFoundError, version

        from asa_api_cli import __version__ as cli_version

        try:
            api_version = version("asa-api-client")
        except PackageNotFoundError:  # pragma: no cover
            api_version = "unknown"

        print(f"asa-api-cli {cli_version} (asa-api-client {api_version})")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Show version and exit",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Suppress informational output",
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show verbose output",
            is_eager=True,
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output in JSON format (implies machine-readable output)",
            is_eager=True,
        ),
    ] = False,
    no_input: Annotated[
        bool,
        typer.Option(
            "--no-input",
            help="Disable interactive prompts (fail instead of prompting)",
            is_eager=True,
        ),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option(
            "--no-color",
            help="Disable colored output",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Apple Search Ads API CLI.

    Manage your Apple Search Ads campaigns, ad groups, keywords,
    and generate performance reports from the command line.

    Set up authentication using environment variables:

        export ASA_CLIENT_ID="SEARCHADS.your-client-id"
        export ASA_TEAM_ID="YOUR_TEAM_ID"
        export ASA_KEY_ID="YOUR_KEY_ID"
        export ASA_ORG_ID="123456"
        export ASA_PRIVATE_KEY_PATH="/path/to/private-key.pem"

    Or test your credentials:

        asa auth test

    Enable shell completion:

        asa --install-completion
    """
    # Set global CLI state
    cli_state.quiet = quiet
    cli_state.verbose = verbose
    cli_state.no_input = no_input
    cli_state.json_output = json_output

    if no_color:
        init_consoles(no_color=True)

    if ctx.invoked_subcommand is None and not version:
        error_console.print(ctx.get_help())
        raise typer.Exit()


if __name__ == "__main__":
    app()
