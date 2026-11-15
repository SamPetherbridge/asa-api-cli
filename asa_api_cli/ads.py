"""Ad and creative management CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.ads import AdCreate, AdStatus, AdUpdate
from asa_api_client.v1.models.creatives import (
    Creative,
    CreativeCreate,
    CreativeSpec,
    CreativeType,
    DestinationCreate,
    DestinationParameter,
    DestinationType,
)
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
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    require_id,
    spinner,
)

app = typer.Typer(help="Manage ads and creatives")

AD_COLUMNS = ["id", "name", "status", "serving_status", "creative_id", "creative_type"]
CREATIVE_COLUMNS = ["id", "name", "creative_type", "system_status", "adam_id", "product_page_id"]


def _resolve_creative_type(raw_type: str) -> CreativeType:
    """Validate a creative type string and reject retired v5 CREATIVE_SET."""
    normalized = raw_type.strip().upper()
    if normalized == "CREATIVE_SET":
        print_error(
            "Unsupported Creative Type",
            "CREATIVE_SET was retired in Apple Ads Platform API v1.",
            hint="Use CUSTOM_PRODUCT_PAGE (with --product-page) or DEFAULT_PRODUCT_PAGE.",
        )
        raise typer.Exit(EXIT_USAGE)
    try:
        return CreativeType(normalized)
    except ValueError:
        valid = ", ".join(ct.value for ct in CreativeType)
        print_error(
            "Invalid Creative Type",
            f"'{raw_type}' is not a valid creative type.",
            hint=f"Valid values: {valid}",
        )
        raise typer.Exit(EXIT_USAGE) from None


def _validate_ad_create_inputs(
    creative_id: int | None,
    creative_type: str,
    product_page_id: str | None,
) -> CreativeType | None:
    """Validate creative arguments before making any network call."""
    if creative_id is not None:
        if product_page_id is not None:
            print_error(
                "Invalid Option Combination",
                "--product-page cannot be combined with --creative-id (destination is fixed on the creative).",
            )
            raise typer.Exit(EXIT_USAGE)
        return None

    parsed_type = _resolve_creative_type(creative_type)
    if parsed_type == CreativeType.CUSTOM_PRODUCT_PAGE and not product_page_id:
        print_error(
            "Missing Product Page",
            "--product-page is required when --creative-type is CUSTOM_PRODUCT_PAGE.",
            hint="List product pages with 'asa product-pages list --adam-id <id>'.",
        )
        raise typer.Exit(EXIT_USAGE)
    if parsed_type == CreativeType.DEFAULT_PRODUCT_PAGE and product_page_id:
        print_error(
            "Invalid Option Combination",
            "--product-page cannot be set when --creative-type is DEFAULT_PRODUCT_PAGE.",
            hint="Use --creative-type CUSTOM_PRODUCT_PAGE to attach a custom product page.",
        )
        raise typer.Exit(EXIT_USAGE)
    return parsed_type


@app.command("list")
def list_ads(
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID"),
    ] = None,
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID"),
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
    """List ads within an ad group or campaign.

    Examples:
        asa ads list --ad-group 456
        asa ads list --campaign 123 --ad-group 456
        asa ads list -c 123 --format json
    """
    if ad_group_id is None and campaign_id is None:
        print_error(
            "Missing Option",
            "Specify --ad-group or --campaign to query ads.",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            with spinner("Fetching ads..."):
                q = Query().page(size=limit, fetch_total_count=True)
                if ad_group_id is not None:
                    q = q.where("adGroupId", "EQUALS", ad_group_id)
                if campaign_id is not None:
                    q = q.where("campaignId", "EQUALS", campaign_id)
                page = client.ads.query(q)

                creatives_by_id: dict[int, Creative] = {}
                creative_ids = sorted({ad.creative_id for ad in page.result if ad.creative_id is not None})
                if creative_ids:
                    try:
                        c_page = client.creatives.query(
                            Query().where("id", "IN", creative_ids).page(size=max(len(creative_ids), 20))
                        )
                        for cr in c_page.result:
                            if cr.id is not None:
                                creatives_by_id[cr.id] = cr
                    except AppleSearchAdsError:
                        pass

            if not page.result:
                print_warning("No ads found")
                return

            rows: list[dict[str, Any]] = []
            for ad in page.result:
                matched_cr: Creative | None = (
                    creatives_by_id.get(ad.creative_id) if ad.creative_id is not None else None
                )
                ctype = enum_value(matched_cr.creative_type) if matched_cr and matched_cr.creative_type else "-"
                serving = enum_value(ad.display_status) or enum_value(ad.system_status) or "-"
                rows.append(
                    {
                        "id": ad.id,
                        "name": ad.name or "-",
                        "status": enum_value(ad.status) if ad.status else "-",
                        "serving_status": serving,
                        "creative_id": ad.creative_id if ad.creative_id is not None else "-",
                        "creative_type": ctype,
                    }
                )

            output_data(rows, AD_COLUMNS, format, title="Ads")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_ad(
    ad_id: Annotated[int, typer.Argument(help="Ad ID")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
) -> None:
    """Show details for a single ad.

    Example:
        asa ads get 789
    """
    del campaign_id, ad_group_id
    client = get_client()

    try:
        with client:
            with spinner("Fetching ad..."):
                ad = client.ads.get(ad_id)
                cr: Creative | None = None
                if ad.creative_id is not None:
                    try:
                        cr = client.creatives.get(ad.creative_id)
                    except AppleSearchAdsError:
                        cr = None

            product_page_id = "-"
            adam_id = "-"
            if cr and cr.destination and cr.destination.parameters:
                product_page_id = cr.destination.parameters.product_page_id or "-"
                adam_id = cr.destination.parameters.adam_id or "-"

            print_result_panel(
                f"Ad {ad.id}",
                {
                    "Name": ad.name or "-",
                    "Campaign ID": ad.campaign_id,
                    "Ad Group ID": ad.ad_group_id,
                    "Status": enum_value(ad.status) if ad.status else "-",
                    "Display Status": enum_value(ad.display_status) or "-",
                    "System Status": enum_value(ad.system_status) or "-",
                    "Creative ID": ad.creative_id if ad.creative_id is not None else "-",
                    "Creative Type": enum_value(cr.creative_type) if cr and cr.creative_type else "-",
                    "Adam ID": adam_id,
                    "Product Page ID": product_page_id,
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("create")
def create_ad(
    ad_group_id: Annotated[
        int,
        typer.Option("--ad-group", "-a", help="Ad Group ID"),
    ],
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Ad name"),
    ],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional; looked up from ad group if omitted)"),
    ] = None,
    creative_id: Annotated[
        int | None,
        typer.Option("--creative-id", help="Existing Creative ID (skips creating a new creative)"),
    ] = None,
    creative_type: Annotated[
        str,
        typer.Option(
            "--creative-type",
            "--type",
            "-t",
            help="Creative type (DEFAULT_PRODUCT_PAGE or CUSTOM_PRODUCT_PAGE)",
        ),
    ] = CreativeType.DEFAULT_PRODUCT_PAGE.value,
    product_page_id: Annotated[
        str | None,
        typer.Option("--product-page", "-p", help="Custom product page ID (required for CUSTOM_PRODUCT_PAGE)"),
    ] = None,
    adam_id: Annotated[
        str | None,
        typer.Option("--adam-id", help="App Store Adam ID (auto-detected from campaign if omitted)"),
    ] = None,
    status: Annotated[
        AdStatus,
        typer.Option("--status", "-s", help="Initial status"),
    ] = AdStatus.ENABLED,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the ad without creating it"),
    ] = False,
) -> None:
    """Create an ad in an ad group (creating a creative first if ``--creative-id`` is omitted).

    Examples:
        asa ads create -a 456 -n "Holiday CPP" -t CUSTOM_PRODUCT_PAGE -p 45812c9b-...
        asa ads create -a 456 -n "Default page ad" -t DEFAULT_PRODUCT_PAGE
        asa ads create -a 456 -n "Existing creative ad" --creative-id 999
    """
    parsed_type = _validate_ad_create_inputs(creative_id, creative_type, product_page_id)

    if dry_run:
        fields: dict[str, Any] = {
            "Ad Group": str(ad_group_id),
            "Name": name,
            "Status": enum_value(status),
        }
        if campaign_id is not None:
            fields["Campaign"] = str(campaign_id)
        if creative_id is not None:
            fields["Creative ID"] = str(creative_id)
        else:
            fields["Creative Type"] = enum_value(parsed_type)
            fields["Product Page ID"] = product_page_id or "-"
            if adam_id:
                fields["Adam ID"] = adam_id
        print_result_panel("Dry run — ad not created", fields, style="yellow")
        return

    client = get_client()

    try:
        with client:
            resolved_creative_id = creative_id
            resolved_creative_type = enum_value(parsed_type) if parsed_type else "-"

            if resolved_creative_id is None:
                assert parsed_type is not None
                resolved_adam_id = adam_id
                if not resolved_adam_id:
                    cid = campaign_id
                    if cid is None:
                        ag = client.ad_groups.get(ad_group_id)
                        cid = ag.campaign_id
                    if cid is not None:
                        camp = client.campaigns.get(cid)
                        resolved_adam_id = camp.promoted_object_id
                if not resolved_adam_id:
                    print_error(
                        "Missing Adam ID",
                        "Could not determine promoted App Store Adam ID for creative destination.",
                        hint="Pass --adam-id <id> explicitly.",
                    )
                    raise typer.Exit(EXIT_USAGE)

                creative_payload = CreativeCreate(
                    name=name,
                    creative_type=parsed_type,
                    creative_spec=CreativeSpec(),
                    destination=DestinationCreate(
                        destination_type=DestinationType.APP_STORE_PRODUCT_PAGE,
                        parameters=DestinationParameter(
                            adam_id=str(resolved_adam_id),
                            product_page_id=product_page_id,
                        ),
                    ),
                )
                with spinner("Creating creative..."):
                    created_creative = client.creatives.create(creative_payload)
                resolved_creative_id = require_id(created_creative.id, "creative")
                resolved_creative_type = enum_value(created_creative.creative_type)

            ad_payload = AdCreate(
                ad_group_id=ad_group_id,
                creative_id=resolved_creative_id,
                name=name,
                status=status,
            )
            with spinner("Creating ad..."):
                created = client.ads.create(ad_payload)

            print_result_panel(
                f"Created ad {created.id}",
                {
                    "Name": created.name or "-",
                    "Status": enum_value(created.status) if created.status else "-",
                    "Creative ID": created.creative_id if created.creative_id is not None else "-",
                    "Creative Type": resolved_creative_type,
                    "Product Page ID": product_page_id or "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("update")
def update_ad(
    ad_id: Annotated[int, typer.Argument(help="Ad ID to update")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="New ad name"),
    ] = None,
    status: Annotated[
        AdStatus | None,
        typer.Option("--status", "-s", help="New status (ENABLED or PAUSED)"),
    ] = None,
) -> None:
    """Update an ad's name or status.

    Examples:
        asa ads update 789 --status PAUSED
        asa ads update 789 --name "Renamed" --status ENABLED
    """
    del campaign_id, ad_group_id
    if name is None and status is None:
        print_error(
            "Nothing to Update",
            "Provide at least one of --name or --status.",
        )
        raise typer.Exit(EXIT_USAGE)

    payload = AdUpdate(name=name, status=status)
    client = get_client()

    try:
        with client:
            with spinner("Updating ad..."):
                updated = client.ads.update(ad_id, payload)

            print_result_panel(
                f"Updated ad {updated.id}",
                {
                    "Name": updated.name or "-",
                    "Status": enum_value(updated.status) if updated.status else "-",
                    "Display Status": enum_value(updated.display_status) or "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_ad(
    ad_id: Annotated[int, typer.Argument(help="Ad ID to delete")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete an ad.

    Example:
        asa ads delete 789 --yes
    """
    del campaign_id, ad_group_id
    if not yes:
        confirmed = typer.confirm(f"Delete ad {ad_id}?")
        if not confirmed:
            print_info("Cancelled")
            return

    client = get_client()

    try:
        with client:
            with spinner("Deleting ad..."):
                client.ads.delete(ad_id)
            print_success(f"Deleted ad {ad_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("creatives")
def list_creatives(
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List creatives in the ad account.

    Examples:
        asa ads creatives
        asa ads creatives --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching creatives..."):
                page = client.creatives.query(Query().page(size=limit, fetch_total_count=True))

            if not page.result:
                print_warning("No creatives found")
                return

            rows: list[dict[str, Any]] = []
            for cr in page.result:
                params = cr.destination.parameters if cr.destination else None
                rows.append(
                    {
                        "id": cr.id,
                        "name": cr.name or "-",
                        "creative_type": enum_value(cr.creative_type) or "-",
                        "system_status": enum_value(cr.system_status) or "-",
                        "adam_id": params.adam_id if params and params.adam_id else "-",
                        "product_page_id": params.product_page_id if params and params.product_page_id else "-",
                    }
                )

            output_data(rows, CREATIVE_COLUMNS, format, title="Creatives")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
