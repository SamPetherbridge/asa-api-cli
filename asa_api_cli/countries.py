"""Supported countries/regions and ad languages CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    get_client,
    handle_api_error,
    output_data,
    print_warning,
    spinner,
)

app = typer.Typer(help="View supported countries, regions, and ad languages")

COUNTRY_COLUMNS = ["country_code", "name", "default_languages", "supported_languages"]


@app.command("list")
def list_countries(
    countries: Annotated[
        list[str] | None,
        typer.Option("--country", "-c", help="Filter by ISO 3166-1 alpha-2 country code(s)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of countries to return"),
    ] = 200,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List Apple Ads supported countries/regions and their ad languages.

    Uses ``POST /v1/metadata/apps/supported-languages/query``.

    Examples:
        asa countries list
        asa countries list --country US --country CA
        asa countries list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching supported countries and languages..."):
                q = Query().page(size=limit, fetch_total_count=True)
                if countries:
                    q = q.where("countryCode", "IN", [c.upper() for c in countries])
                page = client.apps.query_supported_languages(q)

            if not page.result:
                print_warning("No countries returned")
                return

            rows: list[dict[str, Any]] = []
            for item in sorted(page.result, key=lambda c: c.country_code or ""):
                defaults = [loc.language_code or loc.language or "" for loc in (item.ads_default_languages or [])]
                supported = [loc.language_code or loc.language or "" for loc in (item.ads_supported_languages or [])]
                rows.append(
                    {
                        "country_code": item.country_code or "-",
                        "name": item.name or "-",
                        "default_languages": ", ".join(filter(None, defaults)) or "-",
                        "supported_languages": ", ".join(filter(None, supported)) or "-",
                    }
                )

            output_data(rows, COUNTRY_COLUMNS, format, title="Supported countries and regions")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
