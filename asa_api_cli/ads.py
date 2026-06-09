"""Ad (creative) read-only CLI commands."""

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
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="View ads (creatives) within ad groups")

AD_COLUMNS = ["id", "name", "creative_type", "status", "serving_status", "creative_id"]


@app.command("list")
def list_ads(
    campaign_id: Annotated[
        int,
        typer.Option("--campaign", "-c", help="Campaign ID"),
    ],
    ad_group_id: Annotated[
        int,
        typer.Option("--ad-group", "-a", help="Ad group ID"),
    ],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List ads in an ad group.

    Examples:
        asa ads list --campaign 123 --ad-group 456
        asa ads list -c 123 -a 456 --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching ads..."):
                result = client.campaigns(campaign_id).ad_groups(ad_group_id).ads.list(limit=limit)

            if not result.data:
                print_warning("No ads found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": ad.id,
                    "name": ad.name,
                    "creative_type": enum_value(ad.creative_type),
                    "status": enum_value(ad.status),
                    "serving_status": enum_value(ad.serving_status),
                    "creative_id": ad.creative_id or "-",
                }
                for ad in result.data
            ]

            output_data(rows, AD_COLUMNS, format, title=f"Ads in ad group {ad_group_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_ad(
    ad_id: Annotated[
        int,
        typer.Argument(help="Ad ID"),
    ],
    campaign_id: Annotated[
        int,
        typer.Option("--campaign", "-c", help="Campaign ID"),
    ],
    ad_group_id: Annotated[
        int,
        typer.Option("--ad-group", "-a", help="Ad group ID"),
    ],
) -> None:
    """Show details for a single ad.

    Example:
        asa ads get 789 --campaign 123 --ad-group 456
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching ad..."):
                ad = client.campaigns(campaign_id).ad_groups(ad_group_id).ads.get(ad_id)

            print_result_panel(
                f"Ad {ad.id}",
                {
                    "Name": ad.name,
                    "Creative type": enum_value(ad.creative_type),
                    "Status": enum_value(ad.status),
                    "Serving status": enum_value(ad.serving_status),
                    "Creative ID": str(ad.creative_id) if ad.creative_id else "-",
                    "Campaign ID": str(ad.campaign_id),
                    "Ad group ID": str(ad.ad_group_id),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
