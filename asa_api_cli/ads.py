"""Ad (creative) CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.models import AdCreate, AdStatus, AdUpdate, CreativeType

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    confirm_action,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="View and manage ads (creatives) within ad groups")

AD_COLUMNS = ["id", "name", "creative_type", "status", "serving_status", "creative_id"]


def _validate_create(creative_type: CreativeType, product_page_id: str | None) -> None:
    """Validate the creative-type / product-page combination before any API call.

    Raises typer.Exit(EXIT_USAGE) with a friendly message on an invalid combination.
    """
    if creative_type == CreativeType.CREATIVE_SET:
        print_error(
            "Unsupported creative type",
            "CREATIVE_SET ads require selecting creative set assets, which this command "
            "does not support yet. Use CUSTOM_PRODUCT_PAGE or DEFAULT_PRODUCT_PAGE.",
        )
        raise typer.Exit(EXIT_USAGE)
    if creative_type == CreativeType.CUSTOM_PRODUCT_PAGE and not product_page_id:
        print_error(
            "Missing product page",
            "CUSTOM_PRODUCT_PAGE ads require --product-page <id>. Find ids with 'asa product-pages list'.",
        )
        raise typer.Exit(EXIT_USAGE)
    if creative_type == CreativeType.DEFAULT_PRODUCT_PAGE and product_page_id:
        print_error(
            "Unexpected product page",
            "DEFAULT_PRODUCT_PAGE ads must not specify --product-page.",
        )
        raise typer.Exit(EXIT_USAGE)


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


@app.command("create")
def create_ad(
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Ad name"),
    ],
    campaign_id: Annotated[
        int,
        typer.Option("--campaign", "-c", help="Campaign ID"),
    ],
    ad_group_id: Annotated[
        int,
        typer.Option("--ad-group", "-a", help="Ad group ID"),
    ],
    creative_type: Annotated[
        CreativeType,
        typer.Option("--creative-type", "-t", help="Creative type"),
    ] = CreativeType.CUSTOM_PRODUCT_PAGE,
    product_page_id: Annotated[
        str | None,
        typer.Option("--product-page", "-p", help="Custom product page ID (required for CUSTOM_PRODUCT_PAGE)"),
    ] = None,
    status: Annotated[
        AdStatus,
        typer.Option("--status", "-s", help="Initial status"),
    ] = AdStatus.ENABLED,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the ad payload without creating it"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Create an ad in an ad group.

    CUSTOM_PRODUCT_PAGE ads need a --product-page id (see 'asa product-pages list').
    DEFAULT_PRODUCT_PAGE ads use the app's default page and take no product page.

    Examples:
        asa ads create -c 123 -a 456 -n "Holiday CPP" -p <product-page-id>
        asa ads create -c 123 -a 456 -n "Default ad" -t DEFAULT_PRODUCT_PAGE
        asa ads create -c 123 -a 456 -n "Preview" -p <id> --dry-run
    """
    _validate_create(creative_type, product_page_id)

    payload = AdCreate(
        name=name,
        creative_type=creative_type,
        status=status,
        product_page_id=product_page_id,
    )

    preview = {
        "Name": name,
        "Creative type": enum_value(creative_type),
        "Status": enum_value(status),
        "Product page": product_page_id or "-",
        "Campaign ID": str(campaign_id),
        "Ad group ID": str(ad_group_id),
    }

    if dry_run:
        print_info("Dry run - no ad created")
        print_result_panel("Ad payload", preview)
        return

    if not yes and not confirm_action(f"Create ad '{name}' in ad group {ad_group_id}?", default=True):
        print_info("Cancelled")
        return

    client = get_client()

    try:
        with client:
            with spinner("Creating ad..."):
                ad = client.campaigns(campaign_id).ad_groups(ad_group_id).ads.create(payload)

            print_success(f"Created ad {ad.id}: {ad.name}")
            print_result_panel(
                f"Ad {ad.id}",
                {
                    "Name": ad.name,
                    "Creative type": enum_value(ad.creative_type),
                    "Status": enum_value(ad.status),
                    "Serving status": enum_value(ad.serving_status),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("update")
def update_ad(
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
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="New ad name"),
    ] = None,
    status: Annotated[
        AdStatus | None,
        typer.Option("--status", "-s", help="New status (ENABLED or PAUSED)"),
    ] = None,
) -> None:
    """Update an ad's name and/or status.

    At least one of --name or --status must be provided.

    Examples:
        asa ads update 789 -c 123 -a 456 --status PAUSED
        asa ads update 789 -c 123 -a 456 --name "Renamed ad"
    """
    if name is None and status is None:
        print_error("Nothing to update", "Provide at least one of --name or --status.")
        raise typer.Exit(EXIT_USAGE)

    payload = AdUpdate(name=name, status=status)

    client = get_client()

    try:
        with client:
            with spinner("Updating ad..."):
                ad = client.campaigns(campaign_id).ad_groups(ad_group_id).ads.update(ad_id, payload)

            print_success(f"Updated ad {ad.id}")
            print_result_panel(
                f"Ad {ad.id}",
                {
                    "Name": ad.name,
                    "Status": enum_value(ad.status),
                    "Serving status": enum_value(ad.serving_status),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_ad(
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
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt"),
    ] = False,
) -> None:
    """Delete an ad.

    Example:
        asa ads delete 789 -c 123 -a 456
    """
    if not yes and not confirm_action(f"Delete ad {ad_id}? This cannot be undone.", default=False):
        print_info("Cancelled")
        return

    client = get_client()

    try:
        with client:
            with spinner("Deleting ad..."):
                client.campaigns(campaign_id).ad_groups(ad_group_id).ads.delete(ad_id)

            print_success(f"Deleted ad {ad_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
