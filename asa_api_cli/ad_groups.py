"""Ad group management CLI commands (Platform API v1)."""

from decimal import Decimal
from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.ad_groups import (
    AdGroupCreate,
    AdGroupStatus,
    AdGroupUpdate,
    PricingModel,
)
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.bulk import (
    BulkKeywordCreate,
    BulkNegativeKeywordCreate,
    BulkRequestItem,
)
from asa_api_client.v1.models.keywords import (
    KeywordMatchType,
    KeywordStatus,
    NegativeKeywordStatus,
)
from asa_api_client.v1.query import Query
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    ad_group_bid_money,
    campaign_countries,
    campaign_currency,
    console,
    enum_value,
    format_bid_strategy,
    format_money_obj,
    get_client,
    handle_api_error,
    manual_cpt_bid_strategy,
    output_data,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    require_id,
    spinner,
    target_cpa_bid_strategy,
)

app = typer.Typer(help="Manage ad groups within campaigns")

AD_GROUP_COLUMNS = [
    "id",
    "campaign_id",
    "name",
    "status",
    "serving_status",
    "pricing_model",
    "default_bid",
    "bid_strategy",
    "search_match",
]


def _resolve_ad_group_id(first_id: int, second_id: int | None) -> int:
    """Support both ``<ad_group_id>`` (v1 flat) and ``<campaign_id> <ad_group_id>`` (legacy)."""
    return second_id if second_id is not None else first_id


@app.command("list")
def list_ad_groups(
    campaign_id: Annotated[
        int | None,
        typer.Argument(help="Campaign ID (omit to list across all campaigns)"),
    ] = None,
    status: Annotated[
        AdGroupStatus | None,
        typer.Option("--status", "-s", help="Filter by status (ENABLED, PAUSED)"),
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
    """List ad groups in a campaign (or across the ad account).

    Examples:
        asa ad-groups list 123456789
        asa ad-groups list 123456789 --status ENABLED
        asa ad-groups list --format json
    """
    client = get_client()

    try:
        with client:
            label = f"Fetching ad groups for campaign {campaign_id}..." if campaign_id else "Fetching ad groups..."
            with spinner(label):
                q = Query().page(size=limit, fetch_total_count=True)
                if campaign_id is not None:
                    q = q.where("campaignId", "EQUALS", campaign_id)
                if status:
                    q = q.where("status", "EQUALS", status.value)
                page = client.ad_groups.query(q)

            data = []
            for ag in page.result:
                bid_money = ad_group_bid_money(ag)
                serving = enum_value(ag.display_status) or enum_value(ag.system_status)
                data.append(
                    {
                        "id": ag.id,
                        "campaign_id": ag.campaign_id,
                        "name": ag.name or "-",
                        "status": enum_value(ag.status),
                        "serving_status": serving,
                        "pricing_model": enum_value(ag.pricing_model) or "-",
                        "default_bid": format_money_obj(bid_money),
                        "bid_strategy": format_bid_strategy(ag.bid_strategy),
                        "search_match": "Yes" if ag.automated_keywords_opt_in else "No",
                    }
                )

            total_count = (
                page.pagination.total_count
                if page.pagination and page.pagination.total_count is not None
                else len(page)
            )
            output_data(
                data,
                AD_GROUP_COLUMNS,
                format,
                title=f"Ad Groups ({total_count})",
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_ad_group(
    first_id: Annotated[int, typer.Argument(help="Ad Group ID (or Campaign ID when two IDs are passed)")],
    second_id: Annotated[int | None, typer.Argument(help="Ad Group ID (optional legacy second arg)")] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Get details for a specific ad group.

    Examples:
        asa ad-groups get 987654321
        asa ad-groups get 123456789 987654321
        asa ad-groups get 987654321 --format json
    """
    ad_group_id = _resolve_ad_group_id(first_id, second_id)
    client = get_client()

    try:
        with client:
            with spinner(f"Fetching ad group {ad_group_id}..."):
                ag = client.ad_groups.get(ad_group_id)

            if format == OutputFormat.JSON:
                from asa_api_cli.utils import output_json

                output_json(ag)
            else:
                print_result_panel(
                    f"Ad Group: {ag.name}",
                    {
                        "ID": ag.id,
                        "Campaign ID": ag.campaign_id,
                        "Status": enum_value(ag.status),
                        "Display Status": enum_value(ag.display_status) or "-",
                        "System Status": enum_value(ag.system_status) or "-",
                        "Pricing Model": enum_value(ag.pricing_model) or "-",
                        "Bid Strategy": format_bid_strategy(ag.bid_strategy),
                        "Search Match": "Enabled" if ag.automated_keywords_opt_in else "Disabled",
                    },
                    style="cyan",
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("pause")
def pause_ad_group(
    first_id: Annotated[int, typer.Argument(help="Ad Group ID (or Campaign ID when two IDs are passed)")],
    second_id: Annotated[int | None, typer.Argument(help="Ad Group ID (optional legacy second arg)")] = None,
) -> None:
    """Pause an ad group.

    Examples:
        asa ad-groups pause 987654321
        asa ad-groups pause 123456789 987654321
    """
    ad_group_id = _resolve_ad_group_id(first_id, second_id)
    client = get_client()

    try:
        with client:
            with spinner(f"Pausing ad group {ad_group_id}..."):
                ag = client.ad_groups.update(
                    ad_group_id,
                    AdGroupUpdate(status=AdGroupStatus.PAUSED),
                )
            print_success(f"Ad group '{ag.name}' ({ag.id}) paused")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("enable")
def enable_ad_group(
    first_id: Annotated[int, typer.Argument(help="Ad Group ID (or Campaign ID when two IDs are passed)")],
    second_id: Annotated[int | None, typer.Argument(help="Ad Group ID (optional legacy second arg)")] = None,
) -> None:
    """Enable a paused ad group.

    Examples:
        asa ad-groups enable 987654321
        asa ad-groups enable 123456789 987654321
    """
    ad_group_id = _resolve_ad_group_id(first_id, second_id)
    client = get_client()

    try:
        with client:
            with spinner(f"Enabling ad group {ad_group_id}..."):
                ag = client.ad_groups.update(
                    ad_group_id,
                    AdGroupUpdate(status=AdGroupStatus.ENABLED),
                )
            print_success(f"Ad group '{ag.name}' ({ag.id}) enabled")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("set-bid")
def set_bid(
    first_id: Annotated[int, typer.Argument(help="Ad Group ID (or Campaign ID when two IDs are passed)")],
    second_id: Annotated[int | None, typer.Argument(help="Ad Group ID (optional legacy second arg)")] = None,
    bid: Annotated[
        float | None,
        typer.Option("--bid", "-b", help="Manual CPT default bid amount (MANUAL_CPT + TAP)"),
    ] = None,
    target_cpa: Annotated[
        float | None,
        typer.Option(
            "--target-cpa",
            "--cpa",
            help="Target CPA amount for Maximize Conversions (MAX_CONVERSIONS + INSTALL)",
        ),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", "-c", help="Currency code (defaults to ad group/campaign currency or USD)"),
    ] = None,
) -> None:
    """Update ad group bid strategy (manual CPT bid or Maximize Conversions Target CPA).

    In Platform API v1, manual CPT bidding (``MANUAL_CPT``) and Target CPA
    (``MAX_CONVERSIONS``) are mutually exclusive bid strategies.

    Examples:
        asa ad-groups set-bid 987654321 --bid 2.50
        asa ad-groups set-bid 987654321 --target-cpa 8.00
        asa ad-groups set-bid 123456789 987654321 --bid 1.75 --currency EUR
    """
    if bid is not None and target_cpa is not None:
        print_error(
            "Mutually Exclusive Options",
            "Specify either --bid (MANUAL_CPT) or --target-cpa (MAX_CONVERSIONS), not both.",
            hint="Platform API v1 bid strategies are mutually exclusive.",
        )
        raise typer.Exit(EXIT_USAGE)

    if bid is None and target_cpa is None:
        print_error(
            "Missing Option",
            "Specify either --bid or --target-cpa.",
            hint="Example: asa ad-groups set-bid 987654321 --bid 2.50",
        )
        raise typer.Exit(EXIT_USAGE)

    ad_group_id = _resolve_ad_group_id(first_id, second_id)
    client = get_client()

    try:
        with client:
            resolved_currency = currency.upper() if currency else None
            if not resolved_currency:
                existing = client.ad_groups.get(ad_group_id)
                existing_bid = ad_group_bid_money(existing)
                if existing_bid and existing_bid.currency:
                    resolved_currency = existing_bid.currency
                elif existing.campaign_id is not None:
                    camp = client.campaigns.get(existing.campaign_id)
                    resolved_currency = campaign_currency(camp)
                else:
                    resolved_currency = "USD"

            if bid is not None:
                strategy = manual_cpt_bid_strategy(f"{bid:.2f}", resolved_currency)
            else:
                assert target_cpa is not None
                strategy = target_cpa_bid_strategy(f"{target_cpa:.2f}", resolved_currency)

            with spinner(f"Updating bid strategy for ad group {ad_group_id}..."):
                ag = client.ad_groups.update(
                    ad_group_id,
                    AdGroupUpdate(bid_strategy=strategy),
                )
            print_result_panel(
                f"Bid Strategy Updated: {ag.name}",
                {
                    "Bid Strategy": format_bid_strategy(ag.bid_strategy),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_ad_group(
    first_id: Annotated[int, typer.Argument(help="Ad Group ID (or Campaign ID when two IDs are passed)")],
    second_id: Annotated[int | None, typer.Argument(help="Ad Group ID (optional legacy second arg)")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete an ad group permanently.

    Examples:
        asa ad-groups delete 987654321
        asa ad-groups delete 987654321 --force
    """
    ad_group_id = _resolve_ad_group_id(first_id, second_id)
    client = get_client()

    try:
        with client:
            ag = client.ad_groups.get(ad_group_id)

            if not force:
                confirm = typer.confirm(f"Delete ad group '{ag.name}' ({ag.id})? This cannot be undone")
                if not confirm:
                    print_info("Cancelled")
                    raise typer.Exit(0)

            with spinner(f"Deleting ad group {ad_group_id}..."):
                client.ad_groups.delete(ad_group_id)

            print_success(f"Ad group '{ag.name}' ({ag.id}) deleted")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


def _select_campaign_interactive(client: AppleAdsClient) -> tuple[int, str, str]:
    """Interactive campaign selection.

    Returns:
        Tuple of (campaign_id, campaign_name, currency)
    """
    with spinner("Fetching campaigns..."):
        campaigns = list(client.campaigns.iter_all())

    if not campaigns:
        print_error("No campaigns", "No campaigns found in account")
        raise typer.Exit(EXIT_ERROR)

    enabled_campaigns = [c for c in campaigns if enum_value(c.status) == "ENABLED" and c.id is not None]
    if not enabled_campaigns:
        print_warning("No enabled campaigns found, showing all campaigns")
        enabled_campaigns = [c for c in campaigns if c.id is not None]

    console.print()
    console.print("[bold]Available campaigns:[/bold]")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Campaign Name")
    table.add_column("Countries")
    table.add_column("Status")
    table.add_column("ID", style="dim")

    for i, c in enumerate(enabled_campaigns, 1):
        status_str = enum_value(c.status)
        status_color = "green" if status_str == "ENABLED" else "yellow"
        countries_list = campaign_countries(c)
        countries = ", ".join(countries_list[:2])
        if len(countries_list) > 2:
            countries += "..."
        cname = c.name or "-"

        table.add_row(
            str(i),
            cname[:40] + ("..." if len(cname) > 40 else ""),
            countries,
            f"[{status_color}]{status_str}[/{status_color}]",
            str(c.id),
        )

    console.print(table)
    console.print()

    selection = typer.prompt("Select campaign number", type=int)

    if selection < 1 or selection > len(enabled_campaigns):
        print_error("Invalid selection", f"Please enter a number between 1 and {len(enabled_campaigns)}")
        raise typer.Exit(EXIT_USAGE)

    selected = enabled_campaigns[selection - 1]
    cid = require_id(selected.id, "campaign")
    currency = campaign_currency(selected)

    return cid, selected.name or str(cid), currency


@app.command("skag")
def create_skag(
    keywords: Annotated[
        list[str] | None,
        typer.Argument(help="Keywords to create SKAGs for (interactive if omitted)"),
    ] = None,
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (interactive if omitted)"),
    ] = None,
    bid: Annotated[
        float | None,
        typer.Option("--bid", "-b", help="Bid amount for keywords (prompts if omitted)"),
    ] = None,
    skip_negatives: Annotated[
        bool,
        typer.Option("--skip-negatives", help="Skip adding cross-negatives to existing ad groups"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview without creating anything"),
    ] = False,
) -> None:
    """Create Single Keyword Ad Groups (SKAGs) in an existing campaign.

    Creates new ad groups following the SKAG pattern:
    - One ad group per keyword, named "Exact - {Keyword}"
    - Each ad group contains one exact match keyword
    - Cross-negatives: adds new keywords as negatives to existing ad groups,
      and existing keywords as negatives to the new ad groups

    Examples:
        asa ad-groups skag
        asa ad-groups skag "rafter calculator" "roof pitch" --campaign 123456789 --bid 2.50
        asa ad-groups skag "decking calculator" --dry-run
    """
    client = get_client()

    try:
        with client:
            if campaign_id is None:
                campaign_id, campaign_name, currency = _select_campaign_interactive(client)
            else:
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                    campaign_name = campaign.name or str(campaign_id)
                    currency = campaign_currency(campaign)

            console.print()
            print_info(f"Target campaign: {campaign_name} (ID: {campaign_id})")

            if not keywords:
                console.print()
                console.print("[dim]Enter keywords separated by commas (e.g., 'rafter calculator, roof pitch')[/dim]")
                keywords_input = typer.prompt("Keywords")
                keywords = [k.strip().lower() for k in keywords_input.split(",") if k.strip()]
            else:
                keywords = [k.strip().lower() for k in keywords if k.strip()]

            if not keywords:
                print_error("No keywords", "Please provide at least one keyword")
                raise typer.Exit(EXIT_USAGE)

            with spinner("Fetching existing ad groups and keywords..."):
                existing_ad_groups = list(client.ad_groups.iter_all(Query().where("campaignId", "EQUALS", campaign_id)))
                existing_keywords: dict[str, int] = {}
                existing_ad_group_ids: list[int] = []

                for ag in existing_ad_groups:
                    if ag.id is None:
                        continue
                    existing_ad_group_ids.append(ag.id)
                    try:
                        ag_kws = list(client.keywords.iter_all(Query().where("adGroupId", "EQUALS", ag.id)))
                        for kw in ag_kws:
                            if kw.text:
                                existing_keywords[kw.text.lower()] = ag.id
                    except AppleSearchAdsError:
                        continue

            new_keywords = []
            skipped_keywords = []
            for kw_text in keywords:
                if kw_text in existing_keywords:
                    skipped_keywords.append(kw_text)
                else:
                    new_keywords.append(kw_text)

            if skipped_keywords:
                print_warning(f"Skipping {len(skipped_keywords)} existing keyword(s): {', '.join(skipped_keywords)}")

            if not new_keywords:
                print_error("No new keywords", "All specified keywords already exist in this campaign")
                raise typer.Exit(EXIT_ERROR)

            if bid is None:
                default_bid = Decimal("2.00")
                if existing_ad_groups:
                    bids = [
                        Decimal(bm.amount)
                        for ag in existing_ad_groups
                        if (bm := ad_group_bid_money(ag)) is not None and bm.amount is not None
                    ]
                    if bids:
                        default_bid = round(Decimal(sum(bids) / len(bids)), 2)

                console.print()
                bid_str = typer.prompt(f"Bid amount ({currency})", default=str(default_bid))
                try:
                    bid_decimal = Decimal(bid_str)
                except Exception:
                    print_error("Invalid bid", f"'{bid_str}' is not a valid number")
                    raise typer.Exit(EXIT_USAGE) from None
            else:
                bid_decimal = Decimal(str(bid))

            console.print()
            console.rule("[bold]SKAG Plan")
            console.print()

            table = Table(show_header=False, box=None)
            table.add_column("Label", style="bold")
            table.add_column("Value")
            table.add_row("Campaign", f"{campaign_name} ({campaign_id})")
            table.add_row("New SKAGs", str(len(new_keywords)))
            table.add_row("Bid", f"{bid_decimal:.2f} {currency}")
            if not skip_negatives:
                neg_to_new = len(new_keywords) * (len(existing_keywords) + len(new_keywords) - 1)
                neg_to_existing = len(existing_ad_group_ids) * len(new_keywords)
                table.add_row("Cross-Negatives (new AGs)", str(neg_to_new))
                table.add_row("Cross-Negatives (existing AGs)", str(neg_to_existing))
            console.print(table)
            console.print()

            console.print("[bold]Ad Groups to Create:[/bold]")
            console.print()

            ag_table = Table(show_header=True, header_style="bold")
            ag_table.add_column("#", justify="right", style="dim")
            ag_table.add_column("Ad Group Name")
            ag_table.add_column("Keyword")
            ag_table.add_column("Bid", justify="right")

            for i, kw_text in enumerate(new_keywords, 1):
                ag_name = f"Exact - {kw_text.title()}"
                if len(ag_name) > 200:
                    ag_name = ag_name[:197] + "..."
                ag_table.add_row(str(i), ag_name, kw_text, f"{bid_decimal:.2f} {currency}")

            console.print(ag_table)
            console.print()

            if dry_run:
                print_info("Dry run mode - no changes will be made")
                return

            if not typer.confirm("Create these SKAGs?", default=True):
                print_info("Cancelled")
                return

            console.print()

            created_ad_groups: list[tuple[int, str, str]] = []
            created_keywords = 0
            created_negatives = 0

            all_keywords_after = list(existing_keywords.keys()) + new_keywords

            for i, kw_text in enumerate(new_keywords, 1):
                ag_name = f"Exact - {kw_text.title()}"
                if len(ag_name) > 200:
                    ag_name = ag_name[:197] + "..."

                with spinner(f"[{i}/{len(new_keywords)}] Creating ad group: {ag_name}..."):
                    new_ag = client.ad_groups.create(
                        AdGroupCreate(
                            campaign_id=campaign_id,
                            name=ag_name,
                            pricing_model=PricingModel.CPT,
                            bid_strategy=manual_cpt_bid_strategy(str(bid_decimal), currency),
                            automated_keywords_opt_in=False,
                            status=AdGroupStatus.ENABLED,
                        )
                    )
                    new_ag_id = require_id(new_ag.id, "ad group")
                    created_ad_groups.append((new_ag_id, new_ag.name or ag_name, kw_text))

                print_success(f"[{i}/{len(new_keywords)}] Created ad group: {new_ag.name} (ID: {new_ag_id})")

                client.bulk.create_keywords(
                    [
                        BulkRequestItem(
                            data=BulkKeywordCreate(
                                ad_group_id=new_ag_id,
                                text=kw_text,
                                match_type=KeywordMatchType.EXACT,
                                bid=Money(amount=str(bid_decimal), currency=currency),
                                status=KeywordStatus.ENABLED,
                            )
                        )
                    ]
                )
                created_keywords += 1

                if not skip_negatives:
                    negatives_for_new = [k for k in all_keywords_after if k != kw_text]
                    if negatives_for_new:
                        try:
                            neg_items: list[BulkRequestItem[BulkNegativeKeywordCreate]] = [
                                BulkRequestItem(
                                    data=BulkNegativeKeywordCreate(
                                        campaign_id=campaign_id,
                                        ad_group_id=new_ag_id,
                                        text=neg_text,
                                        match_type=KeywordMatchType.EXACT,
                                        status=NegativeKeywordStatus.ENABLED,
                                    )
                                )
                                for neg_text in negatives_for_new
                            ]
                            res = client.bulk.create_negative_keywords(neg_items)
                            created_negatives += sum(1 for r in res if r.success)
                        except AppleSearchAdsError:
                            pass

            if not skip_negatives and existing_ad_group_ids:
                with spinner(f"Adding negatives to {len(existing_ad_group_ids)} existing ad groups..."):
                    neg_items = [
                        BulkRequestItem(
                            data=BulkNegativeKeywordCreate(
                                campaign_id=campaign_id,
                                ad_group_id=existing_ag_id,
                                text=kw_text,
                                match_type=KeywordMatchType.EXACT,
                                status=NegativeKeywordStatus.ENABLED,
                            )
                        )
                        for existing_ag_id in existing_ad_group_ids
                        for kw_text in new_keywords
                    ]
                    try:
                        res = client.bulk.create_negative_keywords(neg_items)
                        created_negatives += sum(1 for r in res if r.success)
                    except AppleSearchAdsError:
                        pass

            console.print()
            print_result_panel(
                "SKAGs Created Successfully",
                {
                    "Campaign": f"{campaign_name} ({campaign_id})",
                    "Ad Groups Created": str(len(created_ad_groups)),
                    "Keywords Created": str(created_keywords),
                    "Negatives Added": str(created_negatives),
                },
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("review-negatives")
def review_negatives(
    campaign_id: Annotated[
        int | None,
        typer.Argument(help="Campaign ID (interactive if omitted)"),
    ] = None,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Automatically add missing negative keywords"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be fixed without making changes"),
    ] = False,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Review negative keyword configuration across ad groups.

    Examples:
        asa ad-groups review-negatives
        asa ad-groups review-negatives 123456789
        asa ad-groups review-negatives --fix
        asa ad-groups review-negatives --fix --dry-run
    """
    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            if campaign_id is None:
                campaign_id, campaign_name, _currency = _select_campaign_interactive(client)
            else:
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                    campaign_name = campaign.name or str(campaign_id)

            console.print()
            console.print(Rule(f"Negative Keyword Review: {campaign_name}"))
            console.print()

            with spinner("Fetching ad groups..."):
                ad_groups = list(
                    client.ad_groups.iter_all(
                        Query()
                        .where("campaignId", "EQUALS", campaign_id)
                        .where("status", "EQUALS", AdGroupStatus.ENABLED.value)
                    )
                )

            if not ad_groups:
                print_warning("No enabled ad groups found in this campaign")
                return

            console.print(f"Found [bold]{len(ad_groups)}[/bold] enabled ad groups")

            ad_group_data: dict[int, dict[str, Any]] = {}
            all_exact_keywords: dict[str, list[int]] = {}
            all_broad_keywords: dict[str, list[int]] = {}

            with spinner("Analyzing keywords and negatives..."):
                for ag in ad_groups:
                    if ag.id is None:
                        continue
                    exact_kws: set[str] = set()
                    broad_kws: set[str] = set()
                    negatives: set[str] = set()

                    try:
                        kws = list(client.keywords.iter_all(Query().where("adGroupId", "EQUALS", ag.id)))
                        for kw in kws:
                            if enum_value(kw.status) != "ENABLED" or not kw.text:
                                continue
                            kw_lower = kw.text.lower()
                            if enum_value(kw.match_type) == "EXACT":
                                exact_kws.add(kw_lower)
                                all_exact_keywords.setdefault(kw_lower, []).append(ag.id)
                            else:
                                broad_kws.add(kw_lower)
                                all_broad_keywords.setdefault(kw_lower, []).append(ag.id)
                    except AppleSearchAdsError:
                        pass

                    try:
                        neg_kws = list(client.negative_keywords.iter_all(Query().where("adGroupId", "EQUALS", ag.id)))
                        for neg in neg_kws:
                            if enum_value(neg.status) == "ENABLED" and neg.text:
                                negatives.add(neg.text.lower())
                    except AppleSearchAdsError:
                        pass

                    ad_group_data[ag.id] = {
                        "name": ag.name or str(ag.id),
                        "search_match": bool(ag.automated_keywords_opt_in),
                        "exact": exact_kws,
                        "broad": broad_kws,
                        "negatives": negatives,
                    }

            campaign_negatives: set[str] = set()
            with spinner("Checking campaign-level negatives..."):
                try:
                    camp_negs = list(
                        client.negative_keywords.iter_all(
                            Query().where("adGroupId", "IS_NULL").where("campaignId", "EQUALS", campaign_id)
                        )
                    )
                    for neg in camp_negs:
                        if enum_value(neg.status) == "ENABLED" and neg.text:
                            campaign_negatives.add(neg.text.lower())
                except AppleSearchAdsError:
                    pass

            missing_negatives: list[dict[str, Any]] = []
            overlapping_broad: list[dict[str, Any]] = []

            for ag_id, data in ad_group_data.items():
                is_broad_group = bool(data["broad"]) or data["search_match"]
                if is_broad_group:
                    for exact_kw, source_ag_ids in all_exact_keywords.items():
                        if ag_id in source_ag_ids and len(source_ag_ids) == 1:
                            continue
                        if exact_kw not in data["negatives"] and exact_kw not in campaign_negatives:
                            source_names = [ad_group_data[sid]["name"] for sid in source_ag_ids if sid != ag_id]
                            missing_negatives.append(
                                {
                                    "ad_group_id": ag_id,
                                    "ad_group_name": data["name"],
                                    "keyword": exact_kw,
                                    "reason": "Search Match" if data["search_match"] else "Broad match",
                                    "source_ad_groups": source_names,
                                }
                            )

            for broad_kw, ag_ids in all_broad_keywords.items():
                if len(ag_ids) > 1:
                    ag_names = [ad_group_data[aid]["name"] for aid in ag_ids]
                    overlapping_broad.append(
                        {
                            "keyword": broad_kw,
                            "ad_group_count": len(ag_ids),
                            "ad_groups": ag_names,
                        }
                    )

            console.print()

            if format == OutputFormat.JSON:
                import json

                result = {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "ad_groups_analyzed": len(ad_groups),
                    "campaign_negatives_count": len(campaign_negatives),
                    "missing_negatives": missing_negatives,
                    "overlapping_broad": overlapping_broad,
                }
                console.print(json.dumps(result, indent=2))
                return

            total_exact = sum(len(d["exact"]) for d in ad_group_data.values())
            total_broad = sum(len(d["broad"]) for d in ad_group_data.values())
            total_ag_negatives = sum(len(d["negatives"]) for d in ad_group_data.values())
            search_match_groups = sum(1 for d in ad_group_data.values() if d["search_match"])

            console.print(f"  Exact keywords: [cyan]{total_exact}[/cyan]")
            console.print(f"  Broad keywords: [cyan]{total_broad}[/cyan]")
            console.print(f"  Search Match groups: [cyan]{search_match_groups}[/cyan]")
            console.print(f"  Ad group negatives: [cyan]{total_ag_negatives}[/cyan]")
            console.print(f"  Campaign negatives: [cyan]{len(campaign_negatives)}[/cyan]")
            console.print()

            if missing_negatives:
                console.print(Rule("Missing Negatives (Potential Cannibalization)"))
                console.print()

                table = Table(show_header=True, header_style="bold")
                table.add_column("Ad Group", style="yellow")
                table.add_column("Missing Negative", style="cyan")
                table.add_column("Type")
                table.add_column("Exact In")

                for issue in missing_negatives[:50]:
                    table.add_row(
                        issue["ad_group_name"][:30] + ("..." if len(issue["ad_group_name"]) > 30 else ""),
                        issue["keyword"],
                        issue["reason"],
                        ", ".join(issue["source_ad_groups"][:2])
                        + ("..." if len(issue["source_ad_groups"]) > 2 else ""),
                    )

                console.print(table)
                if len(missing_negatives) > 50:
                    console.print(f"\n... and {len(missing_negatives) - 50} more")

                console.print()
                print_warning(f"Found {len(missing_negatives)} missing negatives")

                if fix:
                    if dry_run:
                        print_info(f"Dry run - would add {len(missing_negatives)} negative keywords")
                    else:
                        neg_items: list[BulkRequestItem[BulkNegativeKeywordCreate]] = [
                            BulkRequestItem(
                                data=BulkNegativeKeywordCreate(
                                    campaign_id=campaign_id,
                                    ad_group_id=issue["ad_group_id"],
                                    text=issue["keyword"],
                                    match_type=KeywordMatchType.EXACT,
                                    status=NegativeKeywordStatus.ENABLED,
                                )
                            )
                            for issue in missing_negatives
                        ]
                        with spinner(f"Adding {len(neg_items)} negative keywords..."):
                            res = client.bulk.create_negative_keywords(neg_items)
                        added = sum(1 for r in res if r.success)
                        print_success(f"Added {added} negative keywords")
            else:
                print_success("No missing negatives found - keyword funneling looks good!")

            if overlapping_broad:
                console.print()
                console.print(Rule("Overlapping Broad Keywords"))
                console.print()
                table = Table(show_header=True, header_style="bold")
                table.add_column("Keyword", style="cyan")
                table.add_column("Ad Groups")
                for issue in overlapping_broad[:20]:
                    table.add_row(
                        issue["keyword"],
                        ", ".join(issue["ad_groups"][:3]) + ("..." if len(issue["ad_groups"]) > 3 else ""),
                    )
                console.print(table)

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("review-skag")
def review_skag(
    campaign_id: Annotated[
        int | None,
        typer.Argument(help="Campaign ID (interactive if omitted)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be done without making changes"),
    ] = False,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Review ad groups to check if they use single keyword ad groups (SKAGs).

    Examples:
        asa ad-groups review-skag
        asa ad-groups review-skag 123456789
        asa ad-groups review-skag --dry-run
    """
    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            if campaign_id is None:
                campaign_id, campaign_name, currency = _select_campaign_interactive(client)
            else:
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                    campaign_name = campaign.name or str(campaign_id)
                    currency = campaign_currency(campaign)

            console.print()
            console.print(Rule(f"SKAG Review: {campaign_name}"))
            console.print()

            with spinner("Fetching ad groups..."):
                ad_groups = list(
                    client.ad_groups.iter_all(
                        Query()
                        .where("campaignId", "EQUALS", campaign_id)
                        .where("status", "EQUALS", AdGroupStatus.ENABLED.value)
                    )
                )

            if not ad_groups:
                print_warning("No enabled ad groups found in this campaign")
                return

            console.print(f"Found [bold]{len(ad_groups)}[/bold] enabled ad groups")

            skag_count = 0
            non_skag_groups: list[dict[str, Any]] = []

            with spinner("Analyzing ad groups..."):
                for ag in ad_groups:
                    if ag.id is None:
                        continue
                    try:
                        kw_page = client.keywords.query(Query().where("adGroupId", "EQUALS", ag.id).page(size=50))
                        active_keywords = [kw for kw in kw_page.result if enum_value(kw.status) == "ENABLED"]
                        keyword_count = len(active_keywords)

                        if keyword_count == 1:
                            skag_count += 1
                        elif keyword_count > 1:
                            non_skag_groups.append(
                                {
                                    "id": ag.id,
                                    "name": ag.name or str(ag.id),
                                    "keyword_count": keyword_count,
                                    "keywords": active_keywords,
                                    "default_bid": ad_group_bid_money(ag),
                                }
                            )
                    except AppleSearchAdsError:
                        pass

            console.print()

            if format == OutputFormat.JSON:
                import json

                result = {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "total_ad_groups": len(ad_groups),
                    "skag_count": skag_count,
                    "non_skag_count": len(non_skag_groups),
                    "non_skag_groups": [
                        {
                            "id": g["id"],
                            "name": g["name"],
                            "keyword_count": g["keyword_count"],
                        }
                        for g in non_skag_groups
                    ],
                }
                console.print(json.dumps(result, indent=2))
                if not dry_run:
                    return

            total_analyzed = skag_count + len(non_skag_groups)
            skag_pct = (skag_count / total_analyzed * 100) if total_analyzed > 0 else 0

            console.print(f"SKAG ad groups: [bold green]{skag_count}[/bold green] ({skag_pct:.0f}%)")
            console.print(f"Non-SKAG ad groups: [bold yellow]{len(non_skag_groups)}[/bold yellow]")
            console.print()

            if not non_skag_groups:
                print_success("All ad groups are SKAGs!")
                return

            console.print(Rule("Non-SKAG Ad Groups"))
            console.print()

            table = Table(show_header=True, header_style="bold")
            table.add_column("ID")
            table.add_column("Name")
            table.add_column("Keywords", justify="right")

            for group in non_skag_groups[:30]:
                table.add_row(
                    str(group["id"]),
                    group["name"][:40] + ("..." if len(group["name"]) > 40 else ""),
                    str(group["keyword_count"]),
                )

            console.print(table)
            console.print()

            if dry_run:
                print_info("Dry run - no changes made")
                return

            if not typer.confirm("Proceed with split?", default=False):
                print_info("Cancelled")
                return

            console.print()
            console.print(Rule("Splitting Non-SKAG Ad Groups"))
            console.print()

            created_count = 0
            paused_count = 0
            errors: list[str] = []

            for group in non_skag_groups:
                all_keywords = group["keywords"]
                legacy_name = f"{group['name']} (Legacy)"[:200]
                try:
                    with spinner(f"Renaming to legacy: {group['name'][:30]}..."):
                        client.ad_groups.update(
                            group["id"],
                            AdGroupUpdate(name=legacy_name),
                        )
                    console.print(f"[dim]📝[/dim] Renamed: {group['name'][:40]} → (Legacy)")
                except AppleSearchAdsError as e:
                    error_msg = f"Failed to rename ad group '{group['name']}': {e}"
                    errors.append(error_msg)
                    console.print(f"[red]✗[/red] {error_msg}")
                    continue

                for kw in all_keywords:
                    if not kw.text:
                        continue
                    try:
                        match_str = enum_value(kw.match_type) or "EXACT"
                        new_name = f"{match_str.title()} - {kw.text.title()}"[:200]
                        bid_money: Money = kw.bid or group["default_bid"] or Money(amount="1.00", currency=currency)
                        bid_amt = bid_money.amount or "1.00"
                        bid_curr = bid_money.currency or currency

                        with spinner(f"Creating: {new_name[:40]}..."):
                            new_ag = client.ad_groups.create(
                                AdGroupCreate(
                                    campaign_id=campaign_id,
                                    name=new_name,
                                    pricing_model=PricingModel.CPT,
                                    bid_strategy=manual_cpt_bid_strategy(bid_amt, bid_curr),
                                    automated_keywords_opt_in=False,
                                    status=AdGroupStatus.ENABLED,
                                )
                            )
                            new_ag_id = require_id(new_ag.id, "ad group")

                            client.bulk.create_keywords(
                                [
                                    BulkRequestItem(
                                        data=BulkKeywordCreate(
                                            ad_group_id=new_ag_id,
                                            text=kw.text,
                                            match_type=KeywordMatchType(match_str),
                                            bid=Money(amount=bid_amt, currency=bid_curr),
                                            status=KeywordStatus.ENABLED,
                                        )
                                    )
                                ]
                            )

                            other_keywords = [k for k in all_keywords if k.id != kw.id and k.text]
                            if other_keywords:
                                split_neg_items: list[BulkRequestItem[BulkNegativeKeywordCreate]] = [
                                    BulkRequestItem(
                                        data=BulkNegativeKeywordCreate(
                                            campaign_id=campaign_id,
                                            ad_group_id=new_ag_id,
                                            text=other_kw.text or "",
                                            match_type=KeywordMatchType.EXACT,
                                            status=NegativeKeywordStatus.ENABLED,
                                        )
                                    )
                                    for other_kw in other_keywords
                                ]
                                client.bulk.create_negative_keywords(split_neg_items)

                        created_count += 1
                        neg_count = len(other_keywords)
                        console.print(f"[green]✓[/green] Created: {new_name} ({neg_count} negatives)")

                    except AppleSearchAdsError as e:
                        error_msg = f"Failed to create SKAG for '{kw.text}': {e}"
                        errors.append(error_msg)
                        console.print(f"[red]✗[/red] {error_msg}")

                try:
                    with spinner(f"Pausing: {legacy_name[:40]}..."):
                        client.ad_groups.update(
                            group["id"],
                            AdGroupUpdate(status=AdGroupStatus.PAUSED),
                        )
                    paused_count += 1
                    console.print(f"[yellow]⏸[/yellow] Paused: {legacy_name[:50]}")
                except AppleSearchAdsError as e:
                    error_msg = f"Failed to pause ad group '{group['name']}': {e}"
                    errors.append(error_msg)
                    console.print(f"[red]✗[/red] {error_msg}")

            console.print()
            if created_count > 0:
                print_success(f"Created {created_count} new SKAG ad groups")
            if paused_count > 0:
                print_info(f"Paused {paused_count} original ad groups")
            if errors:
                print_warning(f"{len(errors)} errors occurred")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
