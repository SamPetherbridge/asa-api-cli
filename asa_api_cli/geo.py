"""Geo location search CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.geo import GeoEntityType, SearchSupplySourceType

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    print_warning,
    spinner,
)

app = typer.Typer(help="Search geographic locations for ad targeting")

GEO_COLUMNS = ["id", "display_name", "entity", "country_or_region", "admin_area"]


@app.command("search")
def search_geo(
    query: Annotated[
        str,
        typer.Argument(help="Location search query (city, state/region, or country name)"),
    ],
    country: Annotated[
        str,
        typer.Option("--country", "-c", help="ISO 3166-1 alpha-2 country code (e.g. US, AU)"),
    ],
    entity: Annotated[
        GeoEntityType | None,
        typer.Option("--entity", "-e", help="Entity type: Country, AdminArea, Locality, or PostalCode"),
    ] = None,
    supply_source: Annotated[
        SearchSupplySourceType,
        typer.Option("--supply-source", "-s", help="Supply source scope (APPSTORE or MAPS)"),
    ] = SearchSupplySourceType.APPSTORE,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 20,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Search geographic locations within a country for ad group targeting.

    Examples:
        asa geo search "San Francisco" --country US
        asa geo search "New South Wales" -c AU --entity AdminArea
        asa geo search "London" -c GB --format json
    """
    client = get_client()

    try:
        with client:
            with spinner(f"Searching geo locations for '{query}'..."):
                page = client.geo.search(
                    supply_source=supply_source,
                    query=query,
                    country_code=country.upper(),
                    entity=entity,
                    page_size=limit,
                )

            if not page.result:
                print_warning("No locations found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": g.id or "-",
                    "display_name": g.display_name or "-",
                    "entity": enum_value(g.entity) or "-",
                    "country_or_region": g.country_or_region or country.upper(),
                    "admin_area": g.admin_area or "-",
                }
                for g in page.result
            ]

            output_data(rows, GEO_COLUMNS, format, title=f"Geo results for '{query}' ({country.upper()})")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
