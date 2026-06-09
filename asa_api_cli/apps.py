"""App lookup CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    get_client,
    handle_api_error,
    output_data,
    print_warning,
    spinner,
)

app = typer.Typer(help="Search the App Store for advertisable apps")

APP_COLUMNS = ["adam_id", "app_name", "developer_name", "countries"]


@app.command("search")
def search_apps(
    query: Annotated[
        str,
        typer.Argument(help="Search term (app name or keyword)"),
    ],
    own: Annotated[
        bool,
        typer.Option("--own", help="Only return apps owned by your org"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 50,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Search for iOS apps eligible for advertising.

    Returns each app's adamId — the identifier you need to create campaigns.

    Examples:
        asa apps search "weather"
        asa apps search "my app" --own
        asa apps search photo --limit 10 --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Searching App Store..."):
                results = client.apps.search(query=query, return_own_apps=own, limit=limit)

            if not results:
                print_warning("No apps found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "adam_id": app_info.adam_id,
                    "app_name": app_info.app_name,
                    "developer_name": app_info.developer_name or "-",
                    "countries": ", ".join(app_info.country_or_region_codes or []) or "-",
                }
                for app_info in results
            ]

            output_data(
                rows,
                APP_COLUMNS,
                format,
                title=f"App search: {query}",
                column_labels={"adam_id": "adam ID"},
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
