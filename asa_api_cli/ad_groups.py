"""Ad Group CLI commands."""

from decimal import Decimal
from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.models import (
    AdGroupCreate,
    AdGroupStatus,
    AdGroupUpdate,
    KeywordCreate,
    KeywordMatchType,
    Money,
    NegativeKeywordCreate,
    Selector,
)
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    confirm_action,
    console,
    enum_value,
    format_money,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_info,
    print_json,
    print_result_panel,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="Manage ad groups")

AD_GROUP_COLUMNS = [
    "id",
    "name",
    "status",
    "serving_status",
    "default_bid",
    "cpa_goal",
    "search_match",
]

AD_GROUP_COLUMN_LABELS = {
    "id": "ID",
    "serving_status": "Serving",
    "default_bid": "Default Bid",
    "cpa_goal": "CPA Goal",
    "search_match": "Search Match",
}


def format_cpa_goal(cpa_goal: object | None) -> str:
    """Format CPA goal for display."""
    if cpa_goal is None:
        return "-"
    # Handle Money object or string
    if hasattr(cpa_goal, "amount"):
        amount = cpa_goal.amount  # type: ignore
        if hasattr(amount, "amount"):
            # Nested Money object
            return format_money(amount.amount, amount.currency)  # type: ignore
        # Direct string amount
        return str(amount)
    return str(cpa_goal)


def ad_group_to_dict(ad_group: object) -> dict[str, Any]:
    """Convert ad group to display dictionary."""
    return {
        "id": ad_group.id,  # type: ignore
        "name": ad_group.name,  # type: ignore
        "status": enum_value(ad_group.status),  # type: ignore
        "serving_status": enum_value(ad_group.serving_status),  # type: ignore
        "default_bid": format_money(
            ad_group.default_bid_amount.amount,  # type: ignore
            ad_group.default_bid_amount.currency,  # type: ignore
        ),
        "cpa_goal": format_cpa_goal(getattr(ad_group, "cpa_goal", None)),
        "search_match": enum_value(ad_group.automated_keywords_opt_in),  # type: ignore
    }


@app.command("list")
def list_ad_groups(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    status: Annotated[
        AdGroupStatus | None,
        typer.Option("--status", "-s", help="Filter by status"),
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
    """List ad groups in a campaign.

    Examples:
        asa ad-groups list 123456789
        asa ad-groups list 123456789 --status ENABLED
        asa ad-groups list 123456789 --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching ad groups..."):
                if status:
                    selector = Selector().where("status", "==", status.value).limit(limit)
                    ad_groups = client.campaigns(campaign_id).ad_groups.find(selector)
                else:
                    ad_groups = client.campaigns(campaign_id).ad_groups.list(limit=limit)

            if not ad_groups.data:
                print_warning("No ad groups found")
                return

            data = [ad_group_to_dict(ag) for ag in ad_groups]
            output_data(
                data,
                AD_GROUP_COLUMNS,
                format,
                title=f"Ad Groups ({ad_groups.total_results} total)",
                column_labels=AD_GROUP_COLUMN_LABELS,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_ad_group(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    ad_group_id: Annotated[int, typer.Argument(help="Ad Group ID")],
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.JSON,
) -> None:
    """Get details for a specific ad group.

    Examples:
        asa ad-groups get 123456789 987654321
        asa ad-groups get 123456789 987654321 --format table
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching ad group..."):
                ad_group = client.campaigns(campaign_id).ad_groups.get(ad_group_id)

            if format == OutputFormat.JSON:
                print_json(ad_group, title=f"Ad Group {ad_group_id}")
            else:
                data = [ad_group_to_dict(ad_group)]
                output_data(data, AD_GROUP_COLUMNS, format, column_labels=AD_GROUP_COLUMN_LABELS)

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("pause")
def pause_ad_group(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    ad_group_id: Annotated[int, typer.Argument(help="Ad Group ID to pause")],
) -> None:
    """Pause an ad group.

    Examples:
        asa ad-groups pause 123456789 987654321
    """
    client = get_client()

    try:
        with client:
            with spinner("Pausing ad group..."):
                ad_group = client.campaigns(campaign_id).ad_groups.update(
                    ad_group_id,
                    data=AdGroupUpdate(status=AdGroupStatus.PAUSED),
                )
            print_success(f"Ad group '{ad_group.name}' paused")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("enable")
def enable_ad_group(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    ad_group_id: Annotated[int, typer.Argument(help="Ad Group ID to enable")],
) -> None:
    """Enable a paused ad group.

    Examples:
        asa ad-groups enable 123456789 987654321
    """
    client = get_client()

    try:
        with client:
            with spinner("Enabling ad group..."):
                ad_group = client.campaigns(campaign_id).ad_groups.update(
                    ad_group_id,
                    data=AdGroupUpdate(status=AdGroupStatus.ENABLED),
                )
            print_success(f"Ad group '{ad_group.name}' enabled")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("set-bid")
def set_default_bid(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    ad_group_id: Annotated[int, typer.Argument(help="Ad Group ID")],
    bid: Annotated[float, typer.Argument(help="New default bid amount")],
    currency: Annotated[
        str,
        typer.Option("--currency", "-c", help="Currency code"),
    ] = "USD",
) -> None:
    """Set the default bid for an ad group.

    Examples:
        asa ad-groups set-bid 123456789 987654321 2.50
        asa ad-groups set-bid 123456789 987654321 2.50 --currency EUR
    """
    client = get_client()

    try:
        with client:
            with spinner("Updating default bid..."):
                ad_group = client.campaigns(campaign_id).ad_groups.update(
                    ad_group_id,
                    data=AdGroupUpdate(default_bid_amount=Money(amount=str(bid), currency=currency)),
                )

            print_result_panel(
                "Default Bid Updated",
                {
                    "Ad Group": ad_group.name,
                    "Default Bid": f"{ad_group.default_bid_amount.amount} {ad_group.default_bid_amount.currency}",
                },
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_ad_group(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    ad_group_id: Annotated[int, typer.Argument(help="Ad Group ID to delete")],
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation"),
    ] = False,
) -> None:
    """Delete an ad group.

    WARNING: This action cannot be undone.

    Examples:
        asa ad-groups delete 123456789 987654321
        asa ad-groups delete 123456789 987654321 --force
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching ad group..."):
                ad_group = client.campaigns(campaign_id).ad_groups.get(ad_group_id)

            if not force:
                if not confirm_action(f"Are you sure you want to delete ad group '{ad_group.name}'?"):
                    print_warning("Cancelled")
                    raise typer.Exit(0)

            with spinner("Deleting ad group..."):
                client.campaigns(campaign_id).ad_groups.delete(ad_group_id)

            print_success(f"Ad group '{ad_group.name}' deleted")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


def _parse_app_name(campaign_name: str) -> str:
    """Extract app name from campaign name."""
    # Try to parse "App Name - Country - Type" format
    parts = campaign_name.split(" - ")
    if parts:
        return parts[0]
    return campaign_name


def _select_campaign_interactive(client: Any) -> tuple[int, str, str]:
    """Interactive campaign selection - first app, then campaign.

    Returns:
        Tuple of (campaign_id, campaign_name, currency).
    """
    with spinner("Loading campaigns..."):
        all_campaigns = client.campaigns.list()

    if not all_campaigns.data:
        print_error("No campaigns", "No campaigns found")
        raise typer.Exit(EXIT_ERROR)

    # Step 1: Group campaigns by app (adam_id)
    apps: dict[int, dict[str, Any]] = {}  # adam_id -> {name, currency, campaigns}
    for camp in all_campaigns.data:
        if not camp.adam_id:
            continue
        if camp.adam_id not in apps:
            apps[camp.adam_id] = {
                "name": _parse_app_name(camp.name),
                "currency": camp.daily_budget_amount.currency if camp.daily_budget_amount else "USD",
                "campaigns": [],
            }
        apps[camp.adam_id]["campaigns"].append(camp)

    if not apps:
        print_error("No apps", "No campaigns with app IDs found")
        raise typer.Exit(EXIT_ERROR)

    # Step 2: Select app
    console.print()
    print_info("Step 1: Select an app")

    table = Table(show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("App Name")
    table.add_column("Campaigns", justify="right")

    app_list = list(apps.items())
    for i, (_adam_id, app_info) in enumerate(app_list, 1):
        enabled_count = sum(1 for c in app_info["campaigns"] if enum_value(c.status) == "ENABLED")
        table.add_row(
            str(i),
            app_info["name"],
            f"{enabled_count} enabled / {len(app_info['campaigns'])} total",
        )

    console.print(table)
    console.print()

    selection = typer.prompt("Select app number", default="1")
    try:
        idx = int(selection) - 1
        if not (0 <= idx < len(app_list)):
            print_error("Invalid selection", f"Please enter 1-{len(app_list)}")
            raise typer.Exit(EXIT_USAGE)
    except ValueError:
        print_error("Invalid selection", "Please enter a number")
        raise typer.Exit(EXIT_USAGE)

    selected_adam_id, selected_app = app_list[idx]
    currency = selected_app["currency"]

    # Step 3: Filter to enabled campaigns for this app
    enabled_campaigns = [c for c in selected_app["campaigns"] if enum_value(c.status) == "ENABLED"]

    if not enabled_campaigns:
        print_warning(f"No enabled campaigns for {selected_app['name']}")
        # Fall back to all campaigns for this app
        enabled_campaigns = selected_app["campaigns"]

    # Step 4: If many campaigns, offer to filter by type or country
    filtered_campaigns = enabled_campaigns
    if len(enabled_campaigns) > 20:
        console.print()
        print_info(f"Found {len(enabled_campaigns)} enabled campaigns. Filter to narrow down:")
        console.print("[dim]Enter a country code (e.g. US, GB), campaign type (e.g. Generic, Brand),[/dim]")
        console.print("[dim]or press Enter to see all campaigns[/dim]")
        console.print()

        filter_input = typer.prompt("Filter", default="").strip()
        if filter_input:
            filter_lower = filter_input.lower()
            filter_upper = filter_input.upper()
            filtered_campaigns = [
                c
                for c in enabled_campaigns
                if filter_lower in c.name.lower() or filter_upper in (c.countries_or_regions or [])
            ]
            if not filtered_campaigns:
                print_warning(f"No campaigns matching '{filter_input}', showing all")
                filtered_campaigns = enabled_campaigns
            else:
                print_info(f"Found {len(filtered_campaigns)} matching campaigns")

    # Step 5: Select campaign
    console.print()
    print_info(f"Step 2: Select a campaign for {selected_app['name']}")

    table = Table(show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Campaign Name")
    table.add_column("Country")
    table.add_column("Status")

    for i, camp in enumerate(filtered_campaigns, 1):
        # Show shorter name (remove app prefix)
        display_name = camp.name
        if display_name.startswith(selected_app["name"] + " - "):
            display_name = display_name[len(selected_app["name"]) + 3 :]
        table.add_row(
            str(i),
            display_name[:40] + ("..." if len(display_name) > 40 else ""),
            ", ".join(camp.countries_or_regions or []),
            enum_value(camp.status),
        )

    console.print(table)
    console.print()

    selection = typer.prompt("Select campaign number", default="1")
    try:
        idx = int(selection) - 1
        if 0 <= idx < len(filtered_campaigns):
            camp = filtered_campaigns[idx]
            return camp.id, camp.name, currency
        else:
            print_error("Invalid selection", f"Please enter 1-{len(filtered_campaigns)}")
            raise typer.Exit(EXIT_USAGE)
    except ValueError:
        print_error("Invalid selection", "Please enter a number")
        raise typer.Exit(EXIT_USAGE)


@app.command("skag")
def create_skag(
    campaign_id: Annotated[
        int | None,
        typer.Argument(help="Campaign ID (optional, will prompt if not provided)"),
    ] = None,
    keyword: Annotated[
        str | None,
        typer.Option("--keyword", "-k", help="Keyword text"),
    ] = None,
    match_type: Annotated[
        KeywordMatchType,
        typer.Option("--match-type", "-m", help="Match type"),
    ] = KeywordMatchType.EXACT,
    bid: Annotated[
        float | None,
        typer.Option("--bid", "-b", help="Bid amount"),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Ad group name (default: match type prefix + keyword)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without creating"),
    ] = False,
) -> None:
    """Create a Single Keyword Ad Group (SKAG).

    Interactive mode:
        asa ad-groups skag

    With campaign ID:
        asa ad-groups skag 123456789

    With all options:
        asa ad-groups skag 123456789 -k "productivity app" -b 2.50

    Creates an ad group with a single keyword, following the SKAG
    (Single Keyword Ad Group) strategy for granular control.
    """
    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            # Step 1: Select campaign
            if campaign_id is None:
                print_info("Select a campaign to add the SKAG to:")
                campaign_id, campaign_name, currency = _select_campaign_interactive(client)
            else:
                with spinner("Loading campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                campaign_name = campaign.name
                currency = campaign.daily_budget_amount.currency if campaign.daily_budget_amount else "USD"

            print_info(f"Campaign: {campaign_name}")

            # Step 2: Get keyword
            if keyword is None:
                console.print()
                keyword = typer.prompt("Enter keyword").strip()

            if not keyword:
                print_error("No keyword", "Keyword cannot be empty")
                raise typer.Exit(EXIT_USAGE)

            # Step 3: Get bid
            ref_bid: Decimal | float | None = None

            # Try to get reference bid from existing ad groups
            with spinner("Checking existing ad groups..."):
                existing_ad_groups = client.campaigns(campaign_id).ad_groups.list(limit=5)
                if existing_ad_groups.data:
                    bids = [
                        Decimal(ag.default_bid_amount.amount) for ag in existing_ad_groups.data if ag.default_bid_amount
                    ]
                    if bids:
                        ref_bid = sum(bids) / len(bids)

            if bid is None:
                default_bid = f"{ref_bid:.2f}" if ref_bid else "1.00"
                console.print()
                bid_input = typer.prompt(f"Bid amount ({currency})", default=default_bid)
                bid = float(bid_input)

            # Step 4: Determine ad group name
            match_prefix = {
                KeywordMatchType.EXACT: "Exact",
                KeywordMatchType.BROAD: "Broad",
            }.get(match_type, match_type.value.title())

            if name is None:
                default_name = f"{match_prefix} - {keyword.title()}"[:200]
                console.print()
                name = typer.prompt("Ad group name", default=default_name)

            # Step 5: Show plan
            console.print()
            console.print(Rule("SKAG Plan"))
            console.print()

            console.print(f"[bold]Campaign:[/bold] {campaign_name}")
            console.print(f"[bold]Ad Group:[/bold] {name}")
            console.print(f"[bold]Keyword:[/bold] '{keyword}'")
            console.print(f"[bold]Match Type:[/bold] {match_type.value}")
            console.print(f"[bold]Bid:[/bold] {bid:.2f} {currency}")
            console.print()

            if dry_run:
                print_info("Dry run - nothing created")
                return

            # Step 6: Confirm and create
            if not typer.confirm("Create this SKAG?", default=True):
                print_info("Cancelled")
                return

            # Create ad group
            with spinner("Creating ad group..."):
                new_ad_group = client.campaigns(campaign_id).ad_groups.create(
                    AdGroupCreate(
                        name=name,
                        default_bid_amount=Money(
                            amount=str(bid),
                            currency=currency,
                        ),
                        automated_keywords_opt_in=False,
                    )
                )

            print_success(f"Created ad group (ID: {new_ad_group.id})")

            # Create keyword
            with spinner("Adding keyword..."):
                client.campaigns(campaign_id).ad_groups(new_ad_group.id).keywords.create_bulk(
                    [
                        KeywordCreate(
                            text=keyword,
                            match_type=match_type,
                            bid_amount=Money(
                                amount=str(bid),
                                currency=currency,
                            ),
                        )
                    ]
                )

            print_success(f"Added keyword '{keyword}'")

            # Summary
            console.print()
            print_result_panel(
                "SKAG Created",
                {
                    "Campaign": campaign_name,
                    "Ad Group": name,
                    "Ad Group ID": str(new_ad_group.id),
                    "Keyword": f"'{keyword}' ({match_type.value})",
                    "Bid": f"{bid:.2f} {currency}",
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
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Review negative keywords across ad groups for keyword funneling.

    Analyzes whether keywords are properly funneled by checking if exact match
    keywords should be added as negatives to broad match ad groups.

    Examples:
        asa ad-groups review-negatives              # Interactive campaign selection
        asa ad-groups review-negatives 123456789    # Specific campaign
    """
    from collections import defaultdict

    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            # Step 1: Get campaign (interactive if not provided)
            if campaign_id is None:
                campaign_id, campaign_name, _ = _select_campaign_interactive(client)
            else:
                # Verify campaign exists
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                    campaign_name = campaign.name

            console.print()
            console.print(Rule(f"Negative Keyword Review: {campaign_name}"))
            console.print()

            # Step 2: Fetch all ad groups
            with spinner("Fetching ad groups..."):
                ad_groups_resp = client.campaigns(campaign_id).ad_groups.find(
                    Selector(conditions=[{"field": "status", "operator": "EQUALS", "values": ["ENABLED"]}])
                )
                ad_groups = ad_groups_resp

            if not ad_groups:
                print_warning("No enabled ad groups found in this campaign")
                return

            console.print(f"Found [bold]{len(ad_groups)}[/bold] enabled ad groups")

            # Step 3: Collect keywords and negatives for each ad group
            # Structure: {ad_group_id: {name, keywords: [{text, match_type}], negatives: [text]}}
            ag_data: dict[int, dict[str, Any]] = {}

            with spinner("Fetching keywords for all ad groups..."):
                for ag in ad_groups:
                    ag_id = ag.id
                    ag_data[ag_id] = {
                        "name": ag.name,
                        "keywords": [],
                        "negatives": set(),
                    }

                    # Get positive keywords
                    try:
                        kw_resp = client.campaigns(campaign_id).ad_groups(ag_id).keywords.list(limit=200)
                        for kw in kw_resp.data:
                            if enum_value(kw.status) == "ACTIVE":
                                ag_data[ag_id]["keywords"].append(
                                    {
                                        "text": kw.text.lower(),
                                        "match_type": enum_value(kw.match_type),
                                    }
                                )
                    except AppleSearchAdsError:
                        pass  # Skip if no access

                    # Get negative keywords (ad group level)
                    try:
                        neg_resp = client.campaigns(campaign_id).ad_groups(ag_id).negative_keywords.list(limit=200)
                        for neg in neg_resp.data:
                            if enum_value(neg.status) == "ACTIVE":
                                ag_data[ag_id]["negatives"].add(neg.text.lower())
                    except AppleSearchAdsError:
                        pass  # Skip if no access

            # Step 4: Analyze funneling issues
            # Build a map of all keywords by text
            all_keywords: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
            # keyword_text -> [(ad_group_id, ad_group_name, match_type)]

            for ag_id, data in ag_data.items():
                for kw in data["keywords"]:
                    all_keywords[kw["text"]].append((ag_id, data["name"], kw["match_type"]))

            # Find missing negatives
            # Rule: If a keyword exists with EXACT match, it should be negative in BROAD match ad groups
            missing_negatives: list[dict[str, Any]] = []

            for keyword_text, locations in all_keywords.items():
                # Find exact match locations
                exact_locations = [(ag_id, name) for ag_id, name, mt in locations if mt == "EXACT"]
                # Find broad match locations
                broad_locations = [(ag_id, name) for ag_id, name, mt in locations if mt == "BROAD"]

                if exact_locations and broad_locations:
                    # This keyword has both exact and broad - check if exact is negative in broad ad groups
                    for broad_ag_id, broad_ag_name in broad_locations:
                        if keyword_text not in ag_data[broad_ag_id]["negatives"]:
                            missing_negatives.append(
                                {
                                    "keyword": keyword_text,
                                    "broad_ag_id": broad_ag_id,
                                    "broad_ag_name": broad_ag_name,
                                    "exact_ag_names": [name for _, name in exact_locations],
                                }
                            )

            # Also check for keywords that exist in multiple broad ad groups without negatives
            # (potential overlap issues)
            overlapping_broad: list[dict[str, Any]] = []
            for keyword_text, locations in all_keywords.items():
                broad_locs = [(ag_id, name) for ag_id, name, mt in locations if mt == "BROAD"]
                if len(broad_locs) > 1:
                    overlapping_broad.append(
                        {
                            "keyword": keyword_text,
                            "ad_groups": [name for _, name in broad_locs],
                        }
                    )

            # Step 5: Output results
            console.print()

            if format == OutputFormat.JSON:
                import json

                result = {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "ad_group_count": len(ad_groups),
                    "missing_negatives": missing_negatives,
                    "overlapping_broad": overlapping_broad,
                }
                console.print(json.dumps(result, indent=2))
                return

            # Summary stats
            total_keywords = sum(len(d["keywords"]) for d in ag_data.values())
            total_negatives = sum(len(d["negatives"]) for d in ag_data.values())
            console.print(f"Total keywords: [bold]{total_keywords}[/bold]")
            console.print(f"Total ad group negatives: [bold]{total_negatives}[/bold]")
            console.print()

            # Missing negatives table
            if missing_negatives:
                console.print(Rule("Missing Negatives for Keyword Funneling"))
                console.print()
                console.print(
                    "[yellow]These keywords have EXACT match in one ad group but are missing as "
                    "negatives in BROAD match ad groups:[/yellow]"
                )
                console.print()

                table = Table(show_header=True, header_style="bold")
                table.add_column("Keyword", style="cyan")
                table.add_column("Missing In (Broad)")
                table.add_column("Exists In (Exact)")

                for issue in missing_negatives[:50]:  # Limit output
                    table.add_row(
                        issue["keyword"],
                        issue["broad_ag_name"],
                        ", ".join(issue["exact_ag_names"][:2]),
                    )

                console.print(table)

                if len(missing_negatives) > 50:
                    console.print(f"\n... and {len(missing_negatives) - 50} more")

                console.print()
                print_warning(f"Found {len(missing_negatives)} missing negatives")
            else:
                print_success("No missing negatives found - keyword funneling looks good!")

            # Overlapping broad keywords
            if overlapping_broad:
                console.print()
                console.print(Rule("Overlapping Broad Keywords"))
                console.print()
                console.print(
                    "[dim]These keywords exist as BROAD match in multiple ad groups " "(may cause competition):[/dim]"
                )
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

                if len(overlapping_broad) > 20:
                    console.print(f"\n... and {len(overlapping_broad) - 20} more")

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

    SKAGs have exactly one keyword per ad group, allowing for precise bid control
    and better quality scores.

    If non-SKAG ad groups are found, you will be prompted to split them. Splitting:
    - Pauses the original ad group
    - Creates new SKAGs for each keyword with negative keywords for funneling

    Examples:
        asa ad-groups review-skag                    # Review and optionally fix
        asa ad-groups review-skag 123456789          # Specific campaign
        asa ad-groups review-skag --dry-run          # Preview what would change
    """
    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            # Step 1: Get campaign (interactive if not provided)
            if campaign_id is None:
                campaign_id, campaign_name, currency = _select_campaign_interactive(client)
            else:
                # Verify campaign exists
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                    campaign_name = campaign.name

            console.print()
            console.print(Rule(f"SKAG Review: {campaign_name}"))
            console.print()

            # Step 2: Fetch all ad groups
            with spinner("Fetching ad groups..."):
                ad_groups = client.campaigns(campaign_id).ad_groups.find(
                    Selector(conditions=[{"field": "status", "operator": "EQUALS", "values": ["ENABLED"]}])
                )

            if not ad_groups:
                print_warning("No enabled ad groups found in this campaign")
                return

            console.print(f"Found [bold]{len(ad_groups)}[/bold] enabled ad groups")

            # Step 3: Analyze each ad group
            skag_count = 0
            non_skag_groups: list[dict[str, Any]] = []

            with spinner("Analyzing ad groups..."):
                for ag in ad_groups:
                    # Get keywords for this ad group
                    try:
                        kw_resp = client.campaigns(campaign_id).ad_groups(ag.id).keywords.list(limit=50)
                        active_keywords = [kw for kw in kw_resp.data if enum_value(kw.status) == "ACTIVE"]
                        keyword_count = len(active_keywords)

                        if keyword_count == 1:
                            skag_count += 1
                        elif keyword_count > 1:
                            non_skag_groups.append(
                                {
                                    "id": ag.id,
                                    "name": ag.name,
                                    "keyword_count": keyword_count,
                                    "keywords": active_keywords,
                                    "default_bid": ag.default_bid_amount,
                                }
                            )
                        # keyword_count == 0 is ignored (empty ad groups)

                    except AppleSearchAdsError:
                        pass  # Skip if no access

            # Step 4: Output results
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

            # Summary stats
            total_analyzed = skag_count + len(non_skag_groups)
            skag_pct = (skag_count / total_analyzed * 100) if total_analyzed > 0 else 0

            console.print(f"SKAG ad groups: [bold green]{skag_count}[/bold green] ({skag_pct:.0f}%)")
            console.print(f"Non-SKAG ad groups: [bold yellow]{len(non_skag_groups)}[/bold yellow]")
            console.print()

            if not non_skag_groups:
                print_success("All ad groups are SKAGs!")
                return

            # Show non-SKAG groups
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

            if len(non_skag_groups) > 30:
                console.print(f"\n... and {len(non_skag_groups) - 30} more")

            # Step 5: Show what will be created
            console.print()
            total_new_ad_groups = sum(g["keyword_count"] for g in non_skag_groups)

            console.print("[bold]Split non-SKAGs into SKAGs?[/bold]")
            console.print()
            console.print("This will:")
            console.print(f"  - [dim]Rename[/dim] {len(non_skag_groups)} original ad group(s) to (Legacy)")
            console.print(f"  - [yellow]Pause[/yellow] {len(non_skag_groups)} original ad group(s)")
            console.print(f"  - [green]Create[/green] {total_new_ad_groups} new SKAG ad group(s)")
            console.print("  - Add [cyan]negative keywords[/cyan] to each new SKAG for funneling")

            # Show detailed breakdown of what will be created
            for group in non_skag_groups[:5]:
                console.print(f"\n[dim]Rename:[/dim] {group['name']} → [dim](Legacy)[/dim]")
                console.print(f"[yellow]Pause:[/yellow] {group['name']} (Legacy)")
                console.print()
                for kw in group["keywords"]:
                    match_prefix = enum_value(kw.match_type).title()
                    new_name = f"{match_prefix} - {kw.text.title()}"[:200]
                    other_keywords = [k.text for k in group["keywords"] if k.id != kw.id]

                    console.print(f"  [green]New SKAG:[/green] {new_name}")
                    console.print(f"    [dim]Keyword:[/dim] {kw.text} ({enum_value(kw.match_type)})")
                    if other_keywords:
                        console.print("    [cyan]Negatives:[/cyan]")
                        for neg in other_keywords:
                            console.print(f"      - {neg}")
                    console.print()

            if len(non_skag_groups) > 5:
                remaining = len(non_skag_groups) - 5
                remaining_keywords = sum(g["keyword_count"] for g in non_skag_groups[5:])
                console.print(f"... and {remaining} more ad groups ({remaining_keywords} SKAGs) to create")

            console.print()

            if dry_run:
                print_info("Dry run - no changes made")
                return

            # Confirm before making changes
            if not typer.confirm("Proceed with split?", default=False):
                print_info("Cancelled")
                return

            console.print()
            console.print(Rule("Splitting Non-SKAG Ad Groups"))
            console.print()

            # Perform the split
            created_count = 0
            paused_count = 0
            errors: list[str] = []

            for group in non_skag_groups:
                all_keywords = group["keywords"]

                # First, rename the original ad group to "(Legacy)" to free up potential name conflicts
                legacy_name = f"{group['name']} (Legacy)"[:200]
                try:
                    with spinner(f"Renaming to legacy: {group['name'][:30]}..."):
                        client.campaigns(campaign_id).ad_groups.update(
                            group["id"],
                            AdGroupUpdate(name=legacy_name),
                        )
                    console.print(f"[dim]📝[/dim] Renamed: {group['name'][:40]} → (Legacy)")
                except AppleSearchAdsError as e:
                    error_msg = f"Failed to rename ad group '{group['name']}': {e}"
                    errors.append(error_msg)
                    console.print(f"[red]✗[/red] {error_msg}")
                    continue  # Skip this group if we can't rename it

                # Create new SKAGs for ALL keywords in the group
                for kw in all_keywords:
                    try:
                        # Determine new ad group name
                        match_prefix = {
                            "EXACT": "Exact",
                            "BROAD": "Broad",
                        }.get(enum_value(kw.match_type), enum_value(kw.match_type).title())
                        new_name = f"{match_prefix} - {kw.text.title()}"[:200]

                        # Get bid from keyword or default bid
                        bid = kw.bid_amount or group["default_bid"]

                        with spinner(f"Creating: {new_name[:40]}..."):
                            # Create new ad group
                            new_ag = client.campaigns(campaign_id).ad_groups.create(
                                AdGroupCreate(
                                    name=new_name,
                                    default_bid_amount=bid,
                                    automated_keywords_opt_in=False,
                                )
                            )

                            # Add the keyword to new ad group
                            client.campaigns(campaign_id).ad_groups(new_ag.id).keywords.create_bulk(
                                [
                                    KeywordCreate(
                                        text=kw.text,
                                        match_type=kw.match_type,
                                        bid_amount=bid,
                                    )
                                ]
                            )

                            # Add negative keywords for funneling (all other keywords in the group)
                            other_keywords = [k for k in all_keywords if k.id != kw.id]
                            if other_keywords:
                                negatives = [
                                    NegativeKeywordCreate(
                                        text=other_kw.text,
                                        match_type=KeywordMatchType.EXACT,
                                    )
                                    for other_kw in other_keywords
                                ]
                                client.campaigns(campaign_id).ad_groups(new_ag.id).negative_keywords.create_bulk(
                                    negatives
                                )

                        created_count += 1
                        neg_count = len(other_keywords) if other_keywords else 0
                        console.print(f"[green]✓[/green] Created: {new_name} ({neg_count} negatives)")

                    except AppleSearchAdsError as e:
                        error_msg = f"Failed to create SKAG for '{kw.text}': {e}"
                        errors.append(error_msg)
                        console.print(f"[red]✗[/red] {error_msg}")

                # Pause the original ad group (now renamed to Legacy)
                try:
                    with spinner(f"Pausing: {legacy_name[:40]}..."):
                        client.campaigns(campaign_id).ad_groups.update(
                            group["id"],
                            AdGroupUpdate(status=AdGroupStatus.PAUSED),
                        )
                    paused_count += 1
                    console.print(f"[yellow]⏸[/yellow] Paused: {legacy_name[:50]}")
                except AppleSearchAdsError as e:
                    error_msg = f"Failed to pause ad group '{group['name']}': {e}"
                    errors.append(error_msg)
                    console.print(f"[red]✗[/red] {error_msg}")

            # Summary
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
