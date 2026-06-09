"""Geographic location lookup CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError

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

app = typer.Typer(help="Search geographic locations for targeting")

GEO_COLUMNS = ["id", "display_name", "entity", "country_or_region", "admin_area", "locality"]


@app.command("search")
def search_geo(
    query: Annotated[
        str,
        typer.Argument(help="Place name to search for (e.g. California, London)"),
    ],
    country: Annotated[
        str,
        typer.Option("--country", "-c", help="Country code to search within (e.g. US, GB)"),
    ],
    entity: Annotated[
        str | None,
        typer.Option("--entity", "-e", help="Filter by entity type: Country, AdminArea, or Locality"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 50,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Search for geographic locations available for campaign targeting.

    Each result's id can be used to target the location in a campaign.

    Examples:
        asa geo search California --country US
        asa geo search London -c GB --entity Locality
        asa geo search Bayern -c DE --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Searching locations..."):
                results = client.geo.search(
                    query=query,
                    country_code=country.upper(),
                    entity=entity,
                    limit=limit,
                )

            if not results:
                print_warning("No locations found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": loc.id,
                    "display_name": loc.display_name,
                    "entity": enum_value(loc.entity),
                    "country_or_region": loc.country_or_region or "-",
                    "admin_area": loc.admin_area or "-",
                    "locality": loc.locality or "-",
                }
                for loc in results
            ]

            output_data(
                rows,
                GEO_COLUMNS,
                format,
                title=f"Locations in {country.upper()}: {query}",
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
