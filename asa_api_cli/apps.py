"""App lookup and eligibility CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="Search App Store apps, inspect metadata, and check ad eligibility")

APP_COLUMNS = ["adam_id", "app_name", "developer_name", "countries"]
ELIGIBILITY_COLUMNS = [
    "adam_id",
    "state",
    "country_or_region",
    "supply_source",
    "supply_placement",
    "device_class",
    "reasons",
]


@app.command("search")
def search_apps(
    query: Annotated[
        str | None,
        typer.Argument(help="Search query (app name or keyword; optional when --own is set)"),
    ] = None,
    own: Annotated[
        bool,
        typer.Option("--own", help="Only return apps owned by your organization"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 20,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Search for App Store apps available for Apple Ads campaigns.

    Examples:
        asa apps search "Calculator"
        asa apps search --own
        asa apps search "Chippy" --own
        asa apps search "Notes" --format json
    """
    if not query and not own:
        print_error(
            "Missing Query",
            "Provide a search query or pass --own to list your organization's apps.",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            label = f"Searching apps for '{query}'..." if query else "Fetching organization apps..."
            with spinner(label):
                page = client.apps.search(
                    query=query or None,
                    return_owned_apps=True if own else None,
                    limit=limit,
                )

            if not page.result:
                print_warning("No apps found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "adam_id": item.adam_id,
                    "app_name": item.app_name,
                    "developer_name": item.developer_name,
                    "countries": ", ".join(item.country_or_region_codes),
                }
                for item in page.result
            ]

            title = f"Apps matching '{query}'" if query else "Owned Apps"
            output_data(rows, APP_COLUMNS, format, title=title)
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_app(
    adam_id: Annotated[int, typer.Argument(help="App Store Adam ID")],
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Show App Store metadata for an app.

    Example:
        asa apps get 123456789
    """
    client = get_client()

    try:
        with client:
            with spinner(f"Fetching app {adam_id}..."):
                details = client.apps.get(adam_id)

            if format == OutputFormat.JSON:
                from asa_api_cli.utils import output_json

                output_json(details)
            else:
                storefronts = details.available_storefronts or []
                storefront_str = ", ".join(storefronts[:15]) + ("..." if len(storefronts) > 15 else "")
                print_result_panel(
                    f"App: {details.app_name or adam_id}",
                    {
                        "Adam ID": details.id or str(adam_id),
                        "Developer": details.artist_name or "-",
                        "Primary Language": details.primary_language or "-",
                        "Primary Genre": details.primary_genre or "-",
                        "Secondary Genre": details.secondary_genre or "-",
                        "Device Classes": ", ".join(enum_value(d) for d in (details.device_classes or [])) or "-",
                        "Storefronts": f"{len(storefronts)} ({storefront_str})" if storefronts else "-",
                    },
                    style="cyan",
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("eligibilities")
def list_eligibilities(
    adam_id_arg: Annotated[
        int | None,
        typer.Argument(help="App Store Adam ID"),
    ] = None,
    adam_id_opt: Annotated[
        int | None,
        typer.Option("--adam-id", "-a", help="App Store Adam ID"),
    ] = None,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country/region code (e.g. US)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Check ad serving eligibility for an app across placements and countries.

    Examples:
        asa apps eligibilities 123456789
        asa apps eligibilities -a 123456789 --country US
    """
    adam_id = adam_id_arg if adam_id_arg is not None else adam_id_opt
    if adam_id is None:
        print_error("Missing Adam ID", "Provide an App Store Adam ID (e.g. 'asa apps eligibilities 123456789').")
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            with spinner(f"Checking eligibilities for app {adam_id}..."):
                q = Query().where("adamId", "EQUALS", adam_id).page(size=limit, fetch_total_count=True)
                if country:
                    q = q.where("countryOrRegion", "EQUALS", country.upper())
                page = client.apps.query_eligibilities(q)

            if not page.result:
                print_warning("No eligibility records found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "adam_id": r.adam_id,
                    "state": enum_value(r.state) or "-",
                    "country_or_region": r.country_or_region or "-",
                    "supply_source": r.supply_source or "-",
                    "supply_placement": r.supply_placement or "-",
                    "device_class": r.device_class or "-",
                    "reasons": ", ".join(r.reasons) if r.reasons else "-",
                }
                for r in page.result
            ]

            output_data(rows, ELIGIBILITY_COLUMNS, format, title=f"Eligibilities for {adam_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
