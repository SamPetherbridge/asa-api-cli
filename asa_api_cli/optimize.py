"""Optimization CLI commands for campaign management."""

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, TypeVar

import typer
from asa_api_client.exceptions import AppleSearchAdsError, NotFoundError
from asa_api_client.models import (
    AdGroupCreate,
    AdGroupUpdate,
    CampaignCreate,
    CampaignStatus,
    CampaignSupplySource,
    GranularityType,
    KeywordCreate,
    KeywordMatchType,
    KeywordUpdate,
    Money,
    NegativeKeywordCreate,
    Selector,
)
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    console,
    enum_value,
    get_client,
    handle_api_error,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="Optimization commands for campaigns")

T = TypeVar("T")


def wait_for_resource(
    check_fn: Callable[[], T],
    max_attempts: int = 10,
    delay: float = 0.5,
) -> T:
    """Wait for a resource to become available by polling.

    Args:
        check_fn: Function that returns the resource or raises NotFoundError.
        max_attempts: Maximum number of attempts before giving up.
        delay: Delay in seconds between attempts.

    Returns:
        The resource once available.

    Raises:
        NotFoundError: If resource not available after max_attempts.
    """
    for attempt in range(max_attempts):
        try:
            return check_fn()
        except NotFoundError:
            if attempt < max_attempts - 1:
                time.sleep(delay)
            else:
                raise
    # Should never reach here, but satisfy type checker
    raise NotFoundError("Resource not found after maximum attempts")


@dataclass
class BidDiscrepancy:
    """Represents a bid discrepancy between ad group and keywords."""

    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    ad_group_bid: Decimal
    keyword_avg_bid: Decimal
    keyword_min_bid: Decimal
    keyword_max_bid: Decimal
    keyword_count: int
    currency: str

    @property
    def difference_pct(self) -> float:
        """Percentage difference between keyword avg and ad group bid."""
        if self.ad_group_bid == 0:
            return 0.0
        return float((self.keyword_avg_bid - self.ad_group_bid) / self.ad_group_bid * 100)


def _format_bid(amount: Decimal, currency: str) -> str:
    """Format a bid amount with currency."""
    return f"{amount:.2f} {currency}"


@app.command("bid-check")
def check_bid_discrepancies(
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            "-t",
            help="Minimum percentage difference to flag (default: 20%)",
        ),
    ] = 20.0,
    auto_fix: Annotated[
        bool,
        typer.Option(
            "--auto-fix",
            help="Automatically apply suggested changes without prompting",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Show what would be changed without making changes",
        ),
    ] = False,
) -> None:
    """Check for bid discrepancies between ad group and keyword levels.

    Scans all enabled campaigns and ad groups, comparing the ad group default
    bid to the average of keyword-level bids. Flags cases where keywords have
    materially higher bids than the ad group default.

    This is useful because when keyword bids are much higher than the ad group
    default, it may indicate the ad group bid should be raised to improve
    competitiveness for new keywords that inherit the default bid.

    Examples:
        asa optimize bid-check                    # Check with 20% threshold
        asa optimize bid-check --threshold 50    # Flag only >50% differences
        asa optimize bid-check --dry-run         # Preview without changes
        asa optimize bid-check --auto-fix        # Apply all suggestions
    """
    client = get_client()
    discrepancies: list[BidDiscrepancy] = []

    try:
        with client:
            # Get all enabled campaigns
            with spinner("Scanning enabled campaigns..."):
                campaigns = list(client.campaigns.find(Selector().where("status", "==", "ENABLED")))

            print_info(f"Found {len(campaigns)} enabled campaigns")
            console.print()

            # Scan each campaign's ad groups
            for campaign in campaigns:
                with spinner(f"Scanning {campaign.name}..."):
                    try:
                        ad_groups = list(
                            client.campaigns(campaign.id).ad_groups.find(Selector().where("status", "==", "ENABLED"))
                        )
                    except AppleSearchAdsError:
                        # Skip campaigns we can't access
                        continue

                    for ag in ad_groups:
                        # Get keywords for this ad group
                        try:
                            keywords = list(client.campaigns(campaign.id).ad_groups(ag.id).keywords.list())
                        except AppleSearchAdsError:
                            continue

                        if not keywords:
                            continue

                        # Calculate keyword bid statistics
                        keyword_bids = [Decimal(kw.bid_amount.amount) for kw in keywords if kw.bid_amount]

                        if not keyword_bids:
                            continue

                        ad_group_bid = Decimal(ag.default_bid_amount.amount)
                        keyword_avg = Decimal(sum(keyword_bids) / len(keyword_bids))
                        keyword_min = min(keyword_bids)
                        keyword_max = max(keyword_bids)
                        currency = ag.default_bid_amount.currency

                        # Check if there's a material discrepancy
                        # (keywords are higher than ad group bid)
                        if ad_group_bid > 0:
                            diff_pct = float((keyword_avg - ad_group_bid) / ad_group_bid * 100)
                            if diff_pct >= threshold:
                                discrepancies.append(
                                    BidDiscrepancy(
                                        campaign_id=campaign.id,
                                        campaign_name=campaign.name,
                                        ad_group_id=ag.id,
                                        ad_group_name=ag.name,
                                        ad_group_bid=ad_group_bid,
                                        keyword_avg_bid=keyword_avg,
                                        keyword_min_bid=keyword_min,
                                        keyword_max_bid=keyword_max,
                                        keyword_count=len(keyword_bids),
                                        currency=currency,
                                    )
                                )

            if not discrepancies:
                print_success(f"No bid discrepancies found above {threshold}% threshold")
                return

            # Sort by difference percentage descending
            discrepancies.sort(key=lambda d: d.difference_pct, reverse=True)

            # Display summary table
            console.print()
            table = Table(
                title=f"Bid Discrepancies Found ({len(discrepancies)} ad groups)",
                show_header=True,
                header_style="header",
            )
            table.add_column("Campaign", style="cyan")
            table.add_column("Ad Group", style="cyan")
            table.add_column("Ad Group Bid", justify="right")
            table.add_column("Keyword Avg", justify="right", style="yellow")
            table.add_column("Diff %", justify="right", style="red")
            table.add_column("Keywords", justify="center")

            for d in discrepancies:
                table.add_row(
                    d.campaign_name[:25] + ("..." if len(d.campaign_name) > 25 else ""),
                    d.ad_group_name[:20] + ("..." if len(d.ad_group_name) > 20 else ""),
                    _format_bid(d.ad_group_bid, d.currency),
                    _format_bid(d.keyword_avg_bid, d.currency),
                    f"+{d.difference_pct:.0f}%",
                    str(d.keyword_count),
                )

            console.print(table)
            console.print()

            if dry_run:
                print_info("Dry run mode - no changes will be made")
                return

            # Interactive mode - process each discrepancy
            changes_made = 0
            for i, d in enumerate(discrepancies, 1):
                console.rule(f"[bold]{i}/{len(discrepancies)}")
                console.print()

                # Show details
                console.print(f"[bold]Campaign:[/bold] {d.campaign_name}")
                console.print(f"[bold]Ad Group:[/bold] {d.ad_group_name}")
                console.print()
                console.print(f"  Current ad group bid:  [dim]{_format_bid(d.ad_group_bid, d.currency)}[/dim]")
                console.print(f"  Keyword average bid:   [yellow]{_format_bid(d.keyword_avg_bid, d.currency)}[/yellow]")
                min_bid = _format_bid(d.keyword_min_bid, d.currency)
                max_bid = _format_bid(d.keyword_max_bid, d.currency)
                console.print(f"  Keyword range:         {min_bid} - {max_bid}")
                console.print(f"  Difference:            [red]+{d.difference_pct:.0f}%[/red]")
                console.print()

                suggested_bid = round(d.keyword_avg_bid, 2)

                if auto_fix:
                    # Apply suggested change without prompting
                    new_bid = suggested_bid
                    action = "apply"
                else:
                    # Interactive prompt
                    console.print(f"[bold]Suggested new bid:[/bold] {_format_bid(suggested_bid, d.currency)}")
                    console.print()

                    action = typer.prompt(
                        "Action",
                        type=str,
                        default="apply",
                        show_default=True,
                        prompt_suffix=" [apply/custom/skip/quit]: ",
                    ).lower()

                    if action == "quit" or action == "q":
                        print_info("Quitting...")
                        break
                    elif action == "skip" or action == "s":
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue
                    elif action == "custom" or action == "c":
                        new_bid_str = typer.prompt(
                            f"Enter new bid ({d.currency})",
                            type=str,
                        )
                        try:
                            new_bid = Decimal(new_bid_str)
                        except Exception:
                            print_warning("Invalid bid amount, skipping")
                            continue
                    elif action == "apply" or action == "a":
                        new_bid = suggested_bid
                    else:
                        print_warning(f"Unknown action '{action}', skipping")
                        continue

                # Apply the change
                with spinner("Updating ad group bid..."):
                    client.campaigns(d.campaign_id).ad_groups.update(
                        d.ad_group_id,
                        data=AdGroupUpdate(
                            default_bid_amount=Money(
                                amount=str(new_bid),
                                currency=d.currency,
                            )
                        ),
                    )

                print_success(
                    f"Updated bid: {_format_bid(d.ad_group_bid, d.currency)} → {_format_bid(new_bid, d.currency)}"
                )
                changes_made += 1
                console.print()

            # Summary
            console.print()
            if changes_made > 0:
                print_result_panel(
                    "Optimization Complete",
                    {
                        "Discrepancies found": str(len(discrepancies)),
                        "Changes made": str(changes_made),
                    },
                )
            else:
                print_info("No changes made")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@dataclass
class KeywordPlan:
    """Planned keyword for the new campaign."""

    text: str
    bid: Decimal
    currency: str
    source_count: int  # Number of source campaigns this keyword appeared in
    impressions: int = 0  # Total impressions in last 90 days
    source_bids: list[Decimal] = field(default_factory=list)


@dataclass
class AdGroupPlan:
    """Planned ad group for the new campaign."""

    name: str
    keyword: KeywordPlan
    negatives: list[str] = field(default_factory=list)


@dataclass
class CampaignPlan:
    """Plan for the new campaign."""

    name: str
    country: str
    adam_id: int
    daily_budget: Decimal
    currency: str
    ad_groups: list[AdGroupPlan] = field(default_factory=list)


@dataclass
class CampaignNameParts:
    """Parsed campaign name parts."""

    app_name: str
    country: str
    campaign_type: str  # Generic, Competitor, Brand
    match_type: str  # EM (Exact Match), BM (Broad Match)
    original: str

    @classmethod
    def parse(cls, name: str) -> "CampaignNameParts | None":
        """Parse campaign name in format: App Name - Country - Type - Match.

        Examples:
            'Chippy Tools - US - Generic - Exact Match' -> parts
            'Concrete Tools - AU - Competitor - EM' -> parts
        """
        parts = [p.strip() for p in name.split(" - ")]
        if len(parts) < 4:
            return None

        # Last part is match type
        match_type_raw = parts[-1].upper()
        if match_type_raw in ("EM", "EXACT MATCH"):
            match_type = "EM"
        elif match_type_raw in ("BM", "BROAD MATCH", "SM", "SEARCH MATCH"):
            match_type = "BM"
        else:
            return None

        # Second to last is campaign type
        campaign_type = parts[-2]

        # Second part is country (could be multi-letter code)
        country = parts[1].upper()

        # Everything before country is app name
        app_name = parts[0]

        return cls(
            app_name=app_name,
            country=country,
            campaign_type=campaign_type,
            match_type=match_type,
            original=name,
        )

    def with_country(self, new_country: str) -> str:
        """Generate new campaign name with different country."""
        match_type_full = "Exact Match" if self.match_type == "EM" else "Broad Match"
        return f"{self.app_name} - {new_country} - {self.campaign_type} - {match_type_full}"


def _select_campaigns_interactive(
    campaigns: list[Any],
    campaign_type_filter: str | None = None,
    match_type_filter: str | None = None,
) -> list[Any]:
    """Interactive campaign selection with checkboxes.

    Args:
        campaigns: List of Campaign objects
        campaign_type_filter: Filter by type (Generic, Competitor, Brand)
        match_type_filter: Filter by match type (EM, BM)

    Returns:
        List of selected Campaign objects
    """
    # Parse and filter campaigns
    parsed_campaigns: list[tuple[Any, CampaignNameParts]] = []

    for c in campaigns:
        parsed = CampaignNameParts.parse(c.name)
        if parsed:
            # Apply filters
            if campaign_type_filter and parsed.campaign_type.lower() != campaign_type_filter.lower():
                continue
            if match_type_filter and parsed.match_type != match_type_filter.upper():
                continue
            parsed_campaigns.append((c, parsed))

    if not parsed_campaigns:
        return []

    # Group by app name for easier selection
    apps: dict[str, list[tuple[Any, CampaignNameParts]]] = defaultdict(list)
    for c, parsed in parsed_campaigns:
        apps[parsed.app_name].append((c, parsed))

    # Display selection table
    console.print()
    console.print("[bold]Available campaigns:[/bold]")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("App")
    table.add_column("Country")
    table.add_column("Type")
    table.add_column("Match")
    table.add_column("Status")
    table.add_column("ID", style="dim")

    campaign_list: list[tuple[Any, CampaignNameParts]] = []
    idx = 1
    sorted_apps = sorted(apps.keys())
    for app_idx, app_name in enumerate(sorted_apps):
        app_campaigns = sorted(apps[app_name], key=lambda x: (x[1].campaign_type, x[1].country))
        last_type = None
        for _camp_idx, (c, parsed) in enumerate(app_campaigns):
            # Add a dim divider row between campaign types (within same app)
            if last_type is not None and parsed.campaign_type != last_type:
                table.add_row(
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    "[dim]·[/dim]",
                    style="dim",
                )

            campaign_list.append((c, parsed))
            status_color = "green" if enum_value(c.status) == "ENABLED" else "yellow"

            table.add_row(
                str(idx),
                parsed.app_name[:20],
                parsed.country,
                parsed.campaign_type,
                parsed.match_type,
                f"[{status_color}]{enum_value(c.status)}[/{status_color}]",
                str(c.id),
            )
            idx += 1
            last_type = parsed.campaign_type
        # Add section divider after each app group (except the last one)
        if app_idx < len(sorted_apps) - 1:
            table.add_section()

    console.print(table)
    console.print()

    # Get selection
    console.print("[dim]Enter campaign numbers separated by commas, ranges (1-3), or 'all'[/dim]")
    selection = typer.prompt("Select campaigns", default="all")

    if selection.lower() == "all":
        return [c for c, _ in campaign_list]

    # Parse selection (e.g., "1,2,5-7,10")
    selected_indices: set[int] = set()
    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-")
                selected_indices.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                selected_indices.add(int(part))
            except ValueError:
                continue

    return [c for i, (c, _) in enumerate(campaign_list, 1) if i in selected_indices]


@app.command("expand")
def expand_campaign(
    source_campaigns: Annotated[
        list[int] | None,
        typer.Argument(help="Campaign IDs to use as source (interactive if omitted)"),
    ] = None,
    target_country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Target country code (e.g., CA, DE, FR)"),
    ] = None,
    campaign_type: Annotated[
        str | None,
        typer.Option("--type", "-t", help="Filter by campaign type (Generic, Competitor, Brand)"),
    ] = None,
    match_type: Annotated[
        str | None,
        typer.Option("--match", "-m", help="Filter by match type (EM or BM)"),
    ] = None,
    campaign_name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Name for the new campaign (auto-generated if not provided)"),
    ] = None,
    daily_budget: Annotated[
        float | None,
        typer.Option("--budget", "-b", help="Daily budget (copies from source if not provided)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the plan without creating anything"),
    ] = False,
    skip_negatives: Annotated[
        bool,
        typer.Option("--skip-negatives", help="Skip creating cross-negative keywords"),
    ] = False,
    paused: Annotated[
        bool,
        typer.Option("--paused", "-p", help="Create campaign in PAUSED state"),
    ] = False,
) -> None:
    """Expand campaigns to a new market.

    Creates a new SKAG (Single Keyword Ad Group) campaign in a target market
    based on one or more source campaigns. Keywords are extracted from all
    source campaigns and bids are averaged if a keyword appears in multiple sources.

    Campaign naming follows: App Name - Country - Type - Match Type
    Where Match Type is EM (Exact Match) or BM (Broad Match).

    Structure created:
    - One ad group per keyword (named "Exact - {keyword}")
    - Each ad group contains one exact match keyword
    - Cross-negatives: each keyword is added as exact negative to all other ad groups

    Examples:
        # Interactive mode - select campaigns and enter country
        asa optimize expand

        # Interactive with filters
        asa optimize expand --type Generic --match EM

        # Non-interactive with campaign IDs
        asa optimize expand 123456789 --country CA

        # Multiple campaigns (bids will be averaged)
        asa optimize expand 123 456 789 --country CA

        # Custom name and budget
        asa optimize expand 123 --country DE --name "My App - DE - Generic" --budget 50

        # Preview without creating
        asa optimize expand --country CA --dry-run

        # Create paused (for review before enabling)
        asa optimize expand --country CA --paused
    """
    client = get_client()

    try:
        with client:
            # Step 1: Get source campaigns (interactive or from arguments)
            source_campaign_data = []

            if source_campaigns:
                # Non-interactive: load specified campaign IDs
                with spinner("Loading source campaigns..."):
                    for campaign_id in source_campaigns:
                        try:
                            campaign = client.campaigns.get(campaign_id)
                            source_campaign_data.append(campaign)
                        except AppleSearchAdsError as e:
                            print_error("Error", f"Could not load campaign {campaign_id}: {e.message}")
                            raise typer.Exit(EXIT_ERROR) from None
            else:
                # Interactive: show all campaigns and let user select
                with spinner("Loading campaigns..."):
                    all_campaigns = list(client.campaigns.list())

                source_campaign_data = _select_campaigns_interactive(
                    all_campaigns,
                    campaign_type_filter=campaign_type,
                    match_type_filter=match_type,
                )

            if not source_campaign_data:
                print_error("Error", "No campaigns selected")
                raise typer.Exit(EXIT_ERROR)

            # Display selected source campaigns
            console.print()
            print_info(f"Selected source campaigns ({len(source_campaign_data)}):")
            for c in source_campaign_data:
                countries = ", ".join(c.countries_or_regions[:3])
                if len(c.countries_or_regions) > 3:
                    countries += "..."
                console.print(f"  • {c.name} ({countries})")
            console.print()

            # Step 2: Get target country (interactive if not provided)
            if not target_country:
                target_country = typer.prompt("Target country code (e.g., CA, DE, FR)")
            target_country = target_country.upper() if target_country else ""

            # Get adam_id and currency from first source
            adam_id = source_campaign_data[0].adam_id
            currency = (
                source_campaign_data[0].daily_budget_amount.currency
                if source_campaign_data[0].daily_budget_amount
                else "USD"
            )

            # Step 2: Extract keywords with impressions from last 90 days
            # Use reports API to get keywords that have actually had impressions
            keyword_bids: dict[str, list[Decimal]] = defaultdict(list)
            keyword_impressions: dict[str, int] = defaultdict(int)

            end_date = date.today()
            start_date = end_date - timedelta(days=90)

            for campaign in source_campaign_data:
                with spinner(f"Fetching keyword performance for {campaign.name} (90 days)..."):
                    try:
                        # Get keyword report for last 90 days
                        report = client.reports.keywords(
                            campaign_id=campaign.id,
                            start_date=start_date,
                            end_date=end_date,
                            granularity=GranularityType.DAILY,
                        )

                        for row in report.row:
                            if row.metadata.keyword and row.total:
                                kw_text = row.metadata.keyword.lower()
                                impressions = row.total.impressions

                                # Only include keywords with impressions
                                if impressions > 0:
                                    keyword_impressions[kw_text] += impressions

                                    # Get bid from report metadata or use a default
                                    if row.metadata.bid_amount:
                                        keyword_bids[kw_text].append(Decimal(row.metadata.bid_amount.amount))

                    except AppleSearchAdsError as e:
                        print_warning(f"Could not get report for {campaign.name}: {e.message}")
                        continue

            # Filter to only keywords that had impressions
            active_keywords = set(keyword_impressions.keys())
            keyword_bids = {k: v for k, v in keyword_bids.items() if k in active_keywords}

            if not keyword_bids:
                print_error("Error", "No keywords with impressions found in last 90 days")
                raise typer.Exit(EXIT_ERROR)

            total_impressions = sum(keyword_impressions.values())
            print_info(f"Found {len(keyword_bids)} keywords with {total_impressions:,} impressions in last 90 days")

            # Step 3: Calculate average bids
            keyword_plans: list[KeywordPlan] = []
            for text, bids in keyword_bids.items():
                avg_bid = Decimal(sum(bids) / len(bids))
                keyword_plans.append(
                    KeywordPlan(
                        text=text,
                        bid=round(avg_bid, 2),
                        currency=currency,
                        source_count=len(bids),
                        impressions=keyword_impressions[text],
                        source_bids=bids,
                    )
                )

            # Sort by impressions descending (most popular keywords first)
            keyword_plans.sort(key=lambda k: k.impressions, reverse=True)
            console.print()

            # Step 4: Build campaign plan
            # Generate campaign name using parsed naming convention
            if campaign_name:
                plan_name = campaign_name
            else:
                # Try to parse first source campaign name and generate new name
                parsed = CampaignNameParts.parse(source_campaign_data[0].name)
                if parsed:
                    plan_name = parsed.with_country(target_country)
                else:
                    # Fallback for campaigns that don't match the pattern
                    plan_name = f"{source_campaign_data[0].name} - {target_country}"

            # Use provided budget or average from sources
            if daily_budget is not None:
                plan_budget = Decimal(str(daily_budget))
            else:
                source_budgets = [
                    Decimal(c.daily_budget_amount.amount) for c in source_campaign_data if c.daily_budget_amount
                ]
                if source_budgets:
                    plan_budget = Decimal(sum(source_budgets) / len(source_budgets))
                else:
                    plan_budget = Decimal("100")

            # Create ad group plans (SKAG structure)
            ad_group_plans: list[AdGroupPlan] = []
            all_keywords = [kp.text for kp in keyword_plans]

            for kp in keyword_plans:
                # Ad group name: "Exact - {keyword}"
                ag_name = f"Exact - {kp.text.title()}"
                if len(ag_name) > 200:
                    ag_name = ag_name[:197] + "..."

                # Cross-negatives: all other keywords
                negatives = [k for k in all_keywords if k != kp.text] if not skip_negatives else []

                ad_group_plans.append(
                    AdGroupPlan(
                        name=ag_name,
                        keyword=kp,
                        negatives=negatives,
                    )
                )

            campaign_plan = CampaignPlan(
                name=plan_name,
                country=target_country.upper(),
                adam_id=adam_id,
                daily_budget=plan_budget,
                currency=currency,
                ad_groups=ad_group_plans,
            )

            # Step 5: Display plan
            console.print()
            console.rule("[bold]Campaign Plan")
            console.print()

            # Campaign overview
            table = Table(show_header=False, box=None)
            table.add_column("Label", style="bold")
            table.add_column("Value")
            table.add_row("Campaign Name", campaign_plan.name)
            table.add_row("Target Country", campaign_plan.country)
            table.add_row("Daily Budget", f"{campaign_plan.daily_budget:.2f} {campaign_plan.currency}")
            table.add_row("Ad Groups", str(len(campaign_plan.ad_groups)))
            table.add_row("Status", "PAUSED" if paused else "ENABLED")
            if not skip_negatives:
                total_negatives = sum(len(ag.negatives) for ag in campaign_plan.ad_groups)
                table.add_row("Cross-Negatives", str(total_negatives))
            console.print(table)
            console.print()

            # Ad group details
            console.print("[bold]Ad Groups & Keywords (sorted by impressions):[/bold]")
            console.print()

            ag_table = Table(show_header=True, header_style="bold")
            ag_table.add_column("#", justify="right", style="dim")
            ag_table.add_column("Ad Group")
            ag_table.add_column("Keyword")
            ag_table.add_column("Bid", justify="right")
            ag_table.add_column("Impr (90d)", justify="right")

            for i, ag in enumerate(campaign_plan.ad_groups[:20], 1):  # Show first 20
                ag_table.add_row(
                    str(i),
                    ag.name[:30] + ("..." if len(ag.name) > 30 else ""),
                    ag.keyword.text,
                    f"{ag.keyword.bid:.2f} {ag.keyword.currency}",
                    f"{ag.keyword.impressions:,}",
                )

            if len(campaign_plan.ad_groups) > 20:
                ag_table.add_row(
                    "...",
                    f"[dim]... and {len(campaign_plan.ad_groups) - 20} more[/dim]",
                    "",
                    "",
                    "",
                )

            console.print(ag_table)
            console.print()

            if dry_run:
                print_info("Dry run mode - no changes will be made")
                return

            # Step 6: Confirm and create
            if not typer.confirm("Create this campaign?", default=True):
                print_info("Cancelled")
                return

            console.print()

            # Create campaign
            with spinner("Creating campaign..."):
                new_campaign = client.campaigns.create(
                    CampaignCreate(
                        name=campaign_plan.name,
                        adam_id=campaign_plan.adam_id,
                        countries_or_regions=[campaign_plan.country],
                        daily_budget_amount=Money(
                            amount=str(campaign_plan.daily_budget),
                            currency=campaign_plan.currency,
                        ),
                        supply_sources=[CampaignSupplySource.APPSTORE_SEARCH_RESULTS],
                        status=CampaignStatus.PAUSED if paused else CampaignStatus.ENABLED,
                    )
                )

            print_success(f"Created campaign: {new_campaign.name} (ID: {new_campaign.id})")

            # Wait for campaign to be available
            wait_for_resource(
                lambda: client.campaigns.get(new_campaign.id),
                max_attempts=10,
                delay=0.5,
            )

            # Create ad groups with keywords and negatives
            created_ad_groups = 0
            created_keywords = 0
            created_negatives = 0

            print_info(f"Creating {len(campaign_plan.ad_groups)} ad groups...")

            for i, ag_plan in enumerate(campaign_plan.ad_groups, 1):
                # Create ad group
                with spinner(f"[{i}/{len(campaign_plan.ad_groups)}] Creating ad group: {ag_plan.name[:30]}..."):
                    new_ag = client.campaigns(new_campaign.id).ad_groups.create(
                        AdGroupCreate(
                            name=ag_plan.name,
                            default_bid_amount=Money(
                                amount=str(ag_plan.keyword.bid),
                                currency=ag_plan.keyword.currency,
                            ),
                            automated_keywords_opt_in=False,
                        )
                    )
                    created_ad_groups += 1

                print_success(f"[{i}/{len(campaign_plan.ad_groups)}] Created ad group: {new_ag.name} (ID: {new_ag.id})")

                # Create keyword (must use bulk endpoint)
                client.campaigns(new_campaign.id).ad_groups(new_ag.id).keywords.create_bulk(
                    [
                        KeywordCreate(
                            text=ag_plan.keyword.text,
                            match_type=KeywordMatchType.EXACT,
                            bid_amount=Money(
                                amount=str(ag_plan.keyword.bid),
                                currency=ag_plan.keyword.currency,
                            ),
                        )
                    ]
                )
                created_keywords += 1

                # Create negative keywords (bulk for efficiency)
                if ag_plan.negatives:
                    try:
                        neg_keywords = [
                            NegativeKeywordCreate(
                                text=neg_text,
                                match_type=KeywordMatchType.EXACT,
                            )
                            for neg_text in ag_plan.negatives
                        ]
                        neg_resource = client.campaigns(new_campaign.id).ad_groups(new_ag.id).negative_keywords
                        result = neg_resource.create_bulk(neg_keywords)
                        created_negatives += len(result.data)
                    except AppleSearchAdsError:
                        # Skip if negative keyword creation fails
                        pass

            # Summary
            console.print()
            print_result_panel(
                "Campaign Created Successfully",
                {
                    "Campaign ID": str(new_campaign.id),
                    "Campaign Name": new_campaign.name,
                    "Target Country": campaign_plan.country,
                    "Ad Groups": str(created_ad_groups),
                    "Keywords": str(created_keywords),
                    "Negative Keywords": str(created_negatives),
                    "Status": "PAUSED" if paused else "ENABLED",
                },
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@dataclass
class KeywordBidAnalysis:
    """Analysis of a keyword's bid performance."""

    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    keyword_id: int
    keyword_text: str
    current_bid: Decimal
    currency: str
    impressions: int
    taps: int
    conversions: int
    spend: Decimal
    avg_cpt: Decimal | None  # Average cost per tap
    ttr: float | None  # Tap-through rate
    cr: float | None  # Conversion rate
    country: str

    @property
    def bid_strength(self) -> str:
        """Estimate bid strength based on performance metrics.

        Since Apple doesn't expose bidStrength via API, we estimate:
        - STRONG: High impressions, good TTR
        - MODERATE: Decent impressions, average TTR
        - WEAK: Low impressions or poor TTR
        """
        if self.impressions == 0:
            return "UNKNOWN"

        # Calculate TTR if we have data
        ttr = self.ttr or 0

        if self.impressions >= 1000 and ttr >= 0.05:
            return "STRONG"
        elif self.impressions >= 100 and ttr >= 0.02:
            return "MODERATE"
        elif self.impressions > 0:
            return "WEAK"
        return "UNKNOWN"

    @property
    def recommendation(self) -> str:
        """Suggest bid adjustment based on performance."""
        strength = self.bid_strength
        if strength == "STRONG":
            return "Consider increase for more volume"
        elif strength == "MODERATE":
            return "Monitor performance"
        elif strength == "WEAK":
            return "Increase bid or review keyword"
        return "Need more data"


@app.command("bid-review")
def review_keyword_bids(
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code"),
    ] = None,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Days of performance data to analyze"),
    ] = 30,
    weak_only: Annotated[
        bool,
        typer.Option("--weak", "-w", help="Only show keywords with weak bid strength"),
    ] = False,
    min_impressions: Annotated[
        int,
        typer.Option("--min-impressions", help="Minimum impressions to include"),
    ] = 0,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Max keywords to display"),
    ] = 50,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Export to CSV file"),
    ] = None,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Interactive mode to adjust bids"),
    ] = False,
) -> None:
    """Review keyword bids and their performance.

    Analyzes keyword performance metrics (impressions, taps, conversions)
    to estimate bid strength and suggest optimizations.

    Since Apple doesn't expose bidStrength via API, this command estimates
    it based on:
    - Impression volume (higher = stronger bid)
    - Tap-through rate (higher = better relevance/position)
    - Conversion metrics

    Examples:
        asa optimize bid-review --country US
        asa optimize bid-review --country AU --weak  # Focus on weak performers
        asa optimize bid-review --days 14 --min-impressions 100
        asa optimize bid-review --output keywords.csv
        asa optimize bid-review --weak --interactive  # Interactively increase weak bids
    """
    client = get_client()

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    try:
        with client:
            # Get enabled campaigns
            with spinner("Loading campaigns..."):
                campaigns = list(client.campaigns.find(Selector().where("status", "==", "ENABLED")))

            if country:
                country = country.upper()
                campaigns = [c for c in campaigns if country in [cc.upper() for cc in c.countries_or_regions]]

            if not campaigns:
                print_warning("No enabled campaigns found" + (f" for {country}" if country else ""))
                return

            print_info(f"Analyzing {len(campaigns)} campaigns...")

            # Collect keyword performance data
            keyword_analyses: list[KeywordBidAnalysis] = []

            for campaign in campaigns:
                campaign_country = campaign.countries_or_regions[0] if campaign.countries_or_regions else "?"

                with spinner(f"Fetching keyword data for {campaign.name[:30]}..."):
                    try:
                        # Get keyword report
                        report = client.reports.keywords(
                            campaign_id=campaign.id,
                            start_date=start_date,
                            end_date=end_date,
                            granularity=GranularityType.DAILY,
                        )

                        for row in report.row:
                            if not row.metadata.keyword or not row.total:
                                continue
                            # Skip if no ad_group_id (needed for updates)
                            if not row.metadata.ad_group_id:
                                continue
                            # Skip paused/deleted keywords and ad groups
                            if row.metadata.keyword_status and enum_value(row.metadata.keyword_status) != "ACTIVE":
                                continue
                            if row.metadata.ad_group_status and enum_value(row.metadata.ad_group_status) != "ENABLED":
                                continue

                            impressions = row.total.impressions or 0
                            taps = row.total.taps or 0
                            conversions = row.total.installs or 0
                            spend_amount = row.total.local_spend.amount if row.total.local_spend else "0"
                            spend = Decimal(str(spend_amount))
                            currency = row.total.local_spend.currency if row.total.local_spend else "USD"

                            # Calculate metrics
                            avg_cpt = spend / taps if taps > 0 else None
                            ttr = taps / impressions if impressions > 0 else None
                            cr = conversions / taps if taps > 0 else None

                            # Get current bid from metadata
                            bid_amount = row.metadata.bid_amount.amount if row.metadata.bid_amount else "0"
                            bid = Decimal(str(bid_amount))

                            keyword_analyses.append(
                                KeywordBidAnalysis(
                                    campaign_id=campaign.id,
                                    campaign_name=campaign.name,
                                    ad_group_id=row.metadata.ad_group_id or 0,
                                    ad_group_name=row.metadata.ad_group_name or "",
                                    keyword_id=row.metadata.keyword_id or 0,
                                    keyword_text=row.metadata.keyword,
                                    current_bid=bid,
                                    currency=currency,
                                    impressions=impressions,
                                    taps=taps,
                                    conversions=conversions,
                                    spend=spend,
                                    avg_cpt=avg_cpt,
                                    ttr=ttr,
                                    cr=cr,
                                    country=campaign_country,
                                )
                            )

                    except AppleSearchAdsError:
                        continue

            if not keyword_analyses:
                print_warning("No keyword data found")
                return

            # Apply filters
            # Filter out keywords with no data (UNKNOWN strength)
            keyword_analyses = [k for k in keyword_analyses if k.bid_strength != "UNKNOWN"]

            if min_impressions > 0:
                keyword_analyses = [k for k in keyword_analyses if k.impressions >= min_impressions]

            if weak_only:
                keyword_analyses = [k for k in keyword_analyses if k.bid_strength == "WEAK"]

            if not keyword_analyses:
                print_warning("No keywords match the specified filters")
                return

            # Sort by impressions (most first)
            keyword_analyses.sort(key=lambda k: k.impressions, reverse=True)

            # Export to CSV if requested
            if output:
                try:
                    import csv

                    with open(output, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(
                            [
                                "campaign_name",
                                "ad_group_name",
                                "keyword",
                                "country",
                                "current_bid",
                                "currency",
                                "impressions",
                                "taps",
                                "conversions",
                                "spend",
                                "avg_cpt",
                                "ttr",
                                "cr",
                                "bid_strength",
                                "recommendation",
                            ]
                        )
                        for k in keyword_analyses:
                            writer.writerow(
                                [
                                    k.campaign_name,
                                    k.ad_group_name,
                                    k.keyword_text,
                                    k.country,
                                    k.current_bid,
                                    k.currency,
                                    k.impressions,
                                    k.taps,
                                    k.conversions,
                                    k.spend,
                                    k.avg_cpt,
                                    f"{k.ttr:.4f}" if k.ttr else "",
                                    f"{k.cr:.4f}" if k.cr else "",
                                    k.bid_strength,
                                    k.recommendation,
                                ]
                            )
                    print_success(f"Exported {len(keyword_analyses)} keywords to {output}")
                except Exception as e:
                    print_error("Export failed", str(e))

            # Display table
            display_count = min(limit, len(keyword_analyses))

            table = Table(title=f"Keyword Bid Review ({days} days)")
            table.add_column("Keyword", style="cyan", max_width=25)
            table.add_column("Campaign", style="magenta", max_width=20)
            table.add_column("Country", width=4)
            table.add_column("Bid", justify="right", width=8)
            table.add_column("Impr", justify="right", width=8)
            table.add_column("TTR", justify="right", width=6)
            table.add_column("Strength", justify="center", width=10)

            for k in keyword_analyses[:display_count]:
                # Color code strength
                strength = k.bid_strength
                if strength == "STRONG":
                    strength_display = "[green]STRONG[/green]"
                elif strength == "MODERATE":
                    strength_display = "[yellow]MODERATE[/yellow]"
                elif strength == "WEAK":
                    strength_display = "[red]WEAK[/red]"
                else:
                    strength_display = "[dim]?[/dim]"

                ttr_display = f"{k.ttr * 100:.1f}%" if k.ttr else "—"

                table.add_row(
                    k.keyword_text[:25],
                    k.campaign_name[:20],
                    k.country,
                    f"{k.current_bid:.2f}",
                    f"{k.impressions:,}",
                    ttr_display,
                    strength_display,
                )

            console.print(table)

            if len(keyword_analyses) > display_count:
                print_info(f"Showing {display_count} of {len(keyword_analyses)} keywords. Use --limit to see more.")

            # Summary
            strong = sum(1 for k in keyword_analyses if k.bid_strength == "STRONG")
            moderate = sum(1 for k in keyword_analyses if k.bid_strength == "MODERATE")
            weak = sum(1 for k in keyword_analyses if k.bid_strength == "WEAK")

            console.print("\n[dim]Bid Strength Summary:[/dim]")
            console.print(f"  [green]Strong:[/green] {strong}")
            console.print(f"  [yellow]Moderate:[/yellow] {moderate}")
            console.print(f"  [red]Weak:[/red] {weak}")

            if weak > 0:
                console.print(
                    f"\n[yellow]💡 {weak} keywords have weak bid strength - consider increasing bids[/yellow]"
                )

            # Interactive mode for bid adjustments
            if interactive:
                # Filter to weak/moderate keywords for interactive adjustment
                adjustable = [k for k in keyword_analyses if k.bid_strength in ("WEAK", "MODERATE")]

                if not adjustable:
                    print_info("No weak or moderate keywords to adjust")
                    return

                console.print()
                console.rule("[bold]Interactive Bid Adjustment")
                console.print()
                console.print("[dim]For each keyword, choose an action:[/dim]")
                console.print("[dim]  • Enter a number to set new bid (e.g., '2.50')[/dim]")
                console.print("[dim]  • +N or +N% to increase (e.g., '+0.50' or '+20%')[/dim]")
                console.print("[dim]  • 's' to skip, 'q' to quit[/dim]")
                console.print()

                changes_made = 0
                for i, kw in enumerate(adjustable, 1):
                    # Skip if missing required IDs
                    if not kw.ad_group_id or kw.ad_group_id == 0:
                        continue

                    console.rule(f"[bold]{i}/{len(adjustable)}")
                    console.print()

                    # Show keyword details
                    strength_color = "red" if kw.bid_strength == "WEAK" else "yellow"
                    console.print(f"[bold]Keyword:[/bold] {kw.keyword_text} [dim]ID: {kw.keyword_id}[/dim]")
                    console.print(
                        f"[bold]Campaign:[/bold] {kw.campaign_name} ({kw.country}) [dim]ID: {kw.campaign_id}[/dim]"
                    )
                    console.print(f"[bold]Ad Group:[/bold] {kw.ad_group_name} [dim]ID: {kw.ad_group_id}[/dim]")
                    console.print()
                    console.print(
                        f"  Current bid:     [{strength_color}]{kw.current_bid:.2f} {kw.currency}[/{strength_color}]"
                    )
                    console.print(f"  Impressions:     {kw.impressions:,}")
                    ttr_display = f"{kw.ttr * 100:.1f}%" if kw.ttr else "—"
                    console.print(f"  TTR:             {ttr_display}")
                    console.print(f"  Strength:        [{strength_color}]{kw.bid_strength}[/{strength_color}]")
                    if kw.avg_cpt:
                        console.print(f"  Avg CPT:         {kw.avg_cpt:.2f} {kw.currency}")
                    console.print()

                    # Suggest a new bid (20% increase for weak, 10% for moderate)
                    if kw.bid_strength == "WEAK":
                        suggested = round(kw.current_bid * Decimal("1.20"), 2)
                    else:
                        suggested = round(kw.current_bid * Decimal("1.10"), 2)

                    console.print(f"[bold]Suggested:[/bold] {suggested:.2f} {kw.currency}")
                    console.print()

                    action = (
                        typer.prompt(
                            "New bid",
                            default=str(suggested),
                            show_default=True,
                        )
                        .strip()
                        .lower()
                    )

                    if action in ("q", "quit"):
                        print_info("Quitting...")
                        break
                    elif action in ("s", "skip", ""):
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue

                    # Parse the new bid
                    try:
                        if action.startswith("+"):
                            # Relative increase
                            if action.endswith("%"):
                                pct = Decimal(action[1:-1]) / 100
                                new_bid = round(kw.current_bid * (1 + pct), 2)
                            else:
                                new_bid = kw.current_bid + Decimal(action[1:])
                        else:
                            new_bid = Decimal(action)

                        if new_bid <= 0:
                            print_warning("Invalid bid (must be positive), skipping")
                            continue

                    except Exception:
                        print_warning(f"Invalid input '{action}', skipping")
                        continue

                    # Apply the change
                    # Fetch keywords from ad group to find matching keyword by text
                    try:
                        with spinner("Fetching keywords from ad group..."):
                            keywords = list(
                                client.campaigns(kw.campaign_id).ad_groups(kw.ad_group_id).keywords.list(limit=200)
                            )
                    except AppleSearchAdsError as e:
                        print_error(
                            "Failed to fetch keywords",
                            f"{e.message} (campaign={kw.campaign_id}, adgroup={kw.ad_group_id})",
                        )
                        continue

                    # Find matching ACTIVE keyword by text
                    actual_keyword = next(
                        (
                            k
                            for k in keywords
                            if k.text.lower() == kw.keyword_text.lower() and enum_value(k.status) == "ACTIVE"
                        ),
                        None,
                    )
                    if not actual_keyword:
                        active_keywords = [k.text for k in keywords if enum_value(k.status) == "ACTIVE"]
                        sample = active_keywords[:10]
                        ellipsis = "..." if len(active_keywords) > 10 else ""
                        print_warning(
                            f"Keyword '{kw.keyword_text}' not found in ad group "
                            f"(found {len(active_keywords)} active keywords: {sample}{ellipsis})"
                        )
                        continue

                    try:
                        with spinner(f"Updating keyword {actual_keyword.id}..."):
                            # Must use bulk endpoint - Apple API doesn't support single keyword PUT
                            client.campaigns(kw.campaign_id).ad_groups(kw.ad_group_id).keywords.update_bulk(
                                [
                                    (
                                        actual_keyword.id,
                                        KeywordUpdate(
                                            bid_amount=Money(
                                                amount=str(new_bid),
                                                currency=kw.currency,
                                            )
                                        ),
                                    )
                                ]
                            )
                    except AppleSearchAdsError as e:
                        print_error("Update failed", f"{e.message} (keyword_id={actual_keyword.id})")
                        continue
                    except Exception as e:
                        print_error("Update failed", str(e))
                        continue

                    print_success(f"Updated: {kw.current_bid:.2f} → {new_bid:.2f} {kw.currency}")
                    changes_made += 1
                    console.print()

                # Summary
                console.print()
                if changes_made > 0:
                    print_result_panel(
                        "Bid Review Complete",
                        {
                            "Keywords reviewed": str(len(adjustable)),
                            "Bids updated": str(changes_made),
                        },
                    )
                else:
                    print_info("No changes made")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@dataclass
class CpaCapAnalysis:
    """Analysis of CPA cap impact on ad group delivery."""

    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    cpa_goal: Decimal
    avg_cpa: Decimal | None
    currency: str
    impressions_7d: int
    impressions_30d: int
    taps_7d: int
    taps_30d: int
    installs_30d: int
    spend_30d: Decimal
    country: str

    @property
    def cpa_utilization(self) -> float | None:
        """Percentage of CPA goal utilized (avg_cpa / cpa_goal * 100)."""
        if self.avg_cpa is None or self.cpa_goal == 0:
            return None
        return float(self.avg_cpa / self.cpa_goal * 100)

    @property
    def impact_level(self) -> str:
        """Estimate impact level of CPA cap on delivery.

        - BLOCKED: No impressions at all - CPA goal likely blocking delivery
        - CRITICAL: CPA >= 95% of goal - likely severely limited
        - HIGH: CPA 85-95% of goal - probably limited
        - MODERATE: CPA 70-85% of goal - may be slightly limited
        - LOW: CPA < 70% of goal - unlikely to be limited
        """
        # If no impressions in 30 days, CPA goal is likely blocking delivery
        if self.impressions_30d == 0:
            return "BLOCKED"

        util = self.cpa_utilization
        if util is None:
            return "UNKNOWN"

        if util >= 95:
            return "CRITICAL"
        elif util >= 85:
            return "HIGH"
        elif util >= 70:
            return "MODERATE"
        else:
            return "LOW"

    @property
    def recommendation(self) -> str:
        """Suggest action based on CPA cap impact."""
        level = self.impact_level
        if level == "BLOCKED":
            return "Remove CPA goal - blocking all delivery"
        elif level == "CRITICAL":
            return "Increase CPA goal or reduce bids"
        elif level == "HIGH":
            return "Consider increasing CPA goal"
        elif level == "MODERATE":
            return "Monitor - may become limited"
        elif level == "LOW":
            return "CPA cap not limiting delivery"
        return "Need more conversion data"


@app.command("cpa-impact")
def analyze_cpa_cap_impact(
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code"),
    ] = None,
    min_installs: Annotated[
        int,
        typer.Option("--min-installs", help="Minimum installs to include in analysis"),
    ] = 0,
    critical_only: Annotated[
        bool,
        typer.Option("--critical", help="Only show ad groups with critical/high/blocked CPA impact"),
    ] = False,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Interactive mode to remove CPA goals"),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Export to CSV file"),
    ] = None,
) -> None:
    """Analyze whether CPA caps are limiting ad group delivery.

    Compares each ad group's actual average CPA against its CPA goal to identify
    where CPA caps may be causing reduced delivery. Shows both 7-day and 30-day
    impression data to help identify delivery issues.

    Impact levels:
    - BLOCKED: No impressions - CPA goal is completely blocking delivery
    - CRITICAL (≥95% of goal): Likely severely limited by CPA cap
    - HIGH (85-95%): Probably experiencing delivery limitations
    - MODERATE (70-85%): May be slightly limited during peak times
    - LOW (<70%): CPA cap unlikely to be limiting delivery

    Examples:
        asa optimize cpa-impact                    # Analyze all ad groups
        asa optimize cpa-impact --country US       # Filter to US campaigns
        asa optimize cpa-impact --critical         # Only show problem ad groups
        asa optimize cpa-impact --interactive      # Interactively remove CPA goals
        asa optimize cpa-impact -o cpa-analysis.csv
    """
    client = get_client()

    end_date = date.today() - timedelta(days=1)
    start_date_30d = end_date - timedelta(days=30)
    start_date_7d = end_date - timedelta(days=7)

    try:
        with client:
            # Get enabled campaigns
            with spinner("Loading campaigns..."):
                campaigns = list(client.campaigns.find(Selector().where("status", "==", "ENABLED")))

            if country:
                country = country.upper()
                campaigns = [c for c in campaigns if country in [cc.upper() for cc in c.countries_or_regions]]

            if not campaigns:
                print_warning("No enabled campaigns found" + (f" for {country}" if country else ""))
                return

            print_info(f"Analyzing {len(campaigns)} campaigns for CPA cap impact...")

            # Collect ad groups with CPA goals and their performance
            cpa_analyses: list[CpaCapAnalysis] = []
            ad_groups_with_goals = 0
            ad_groups_without_goals = 0

            for campaign in campaigns:
                campaign_country = campaign.countries_or_regions[0] if campaign.countries_or_regions else "?"

                with spinner(f"Analyzing {campaign.name[:40]}..."):
                    try:
                        # Get ad groups for this campaign
                        ad_groups = list(
                            client.campaigns(campaign.id).ad_groups.find(Selector().where("status", "==", "ENABLED"))
                        )

                        # Filter to ad groups with CPA goals
                        ad_groups_with_cpa = []
                        for ag in ad_groups:
                            if ag.cpa_goal and ag.cpa_goal.amount:
                                ad_groups_with_cpa.append(ag)
                                ad_groups_with_goals += 1
                            else:
                                ad_groups_without_goals += 1

                        if not ad_groups_with_cpa:
                            continue

                        # Get 30-day ad group performance report
                        report_30d = client.reports.ad_groups(
                            campaign_id=campaign.id,
                            start_date=start_date_30d,
                            end_date=end_date,
                            granularity=GranularityType.DAILY,
                        )

                        # Get 7-day ad group performance report
                        report_7d = client.reports.ad_groups(
                            campaign_id=campaign.id,
                            start_date=start_date_7d,
                            end_date=end_date,
                            granularity=GranularityType.DAILY,
                        )

                        # Build maps of ad group performance for both periods
                        def aggregate_report(report: Any) -> dict[int, dict[str, Any]]:
                            perf: dict[int, dict[str, Any]] = {}
                            for row in report.row:
                                if row.metadata.ad_group_id and row.total:
                                    ag_id = row.metadata.ad_group_id
                                    if ag_id not in perf:
                                        perf[ag_id] = {
                                            "impressions": 0,
                                            "taps": 0,
                                            "installs": 0,
                                            "spend": Decimal("0"),
                                            "currency": "USD",
                                        }
                                    perf[ag_id]["impressions"] += row.total.impressions or 0
                                    perf[ag_id]["taps"] += row.total.taps or 0
                                    perf[ag_id]["installs"] += row.total.installs or 0
                                    if row.total.local_spend:
                                        perf[ag_id]["spend"] += Decimal(str(row.total.local_spend.amount))
                                        perf[ag_id]["currency"] = row.total.local_spend.currency
                            return perf

                        ag_perf_30d = aggregate_report(report_30d)
                        ag_perf_7d = aggregate_report(report_7d)

                        # Analyze each ad group with a CPA goal
                        for ag in ad_groups_with_cpa:
                            perf_30d = ag_perf_30d.get(ag.id, {})
                            perf_7d = ag_perf_7d.get(ag.id, {})

                            impressions_30d = perf_30d.get("impressions", 0)
                            impressions_7d = perf_7d.get("impressions", 0)
                            taps_30d = perf_30d.get("taps", 0)
                            taps_7d = perf_7d.get("taps", 0)
                            installs_30d = perf_30d.get("installs", 0)
                            spend_30d = perf_30d.get("spend", Decimal("0"))
                            currency = perf_30d.get("currency", "USD")

                            # Parse CPA goal
                            cpa_goal_amount = ag.cpa_goal.amount
                            if hasattr(cpa_goal_amount, "amount"):
                                # Money object
                                cpa_goal = Decimal(str(cpa_goal_amount.amount))
                            else:
                                # Direct string/number
                                cpa_goal = Decimal(str(cpa_goal_amount))

                            # Calculate actual CPA
                            avg_cpa = None
                            if installs_30d > 0 and spend_30d > 0:
                                avg_cpa = spend_30d / installs_30d

                            cpa_analyses.append(
                                CpaCapAnalysis(
                                    campaign_id=campaign.id,
                                    campaign_name=campaign.name,
                                    ad_group_id=ag.id,
                                    ad_group_name=ag.name,
                                    cpa_goal=cpa_goal,
                                    avg_cpa=avg_cpa,
                                    currency=currency,
                                    impressions_7d=impressions_7d,
                                    impressions_30d=impressions_30d,
                                    taps_7d=taps_7d,
                                    taps_30d=taps_30d,
                                    installs_30d=installs_30d,
                                    spend_30d=spend_30d,
                                    country=campaign_country,
                                )
                            )

                    except AppleSearchAdsError:
                        continue

            if not cpa_analyses:
                print_warning("No ad groups with CPA goals found")
                console.print(f"\n[dim]Ad groups scanned: {ad_groups_without_goals} (none had CPA goals set)[/dim]")
                return

            # Filter by minimum installs
            if min_installs > 0:
                cpa_analyses = [a for a in cpa_analyses if a.installs_30d >= min_installs]

                if not cpa_analyses:
                    print_warning(f"No ad groups with CPA goals have at least {min_installs} installs")
                    return

            # Filter to critical/high/blocked only if requested
            if critical_only:
                cpa_analyses = [a for a in cpa_analyses if a.impact_level in ("BLOCKED", "CRITICAL", "HIGH")]
                if not cpa_analyses:
                    print_success("No ad groups with critical, high, or blocked CPA cap impact")
                    return

            # Sort by impact level (BLOCKED first, then by CPA utilization)
            impact_order = {"BLOCKED": 0, "CRITICAL": 1, "HIGH": 2, "UNKNOWN": 3, "MODERATE": 4, "LOW": 5}
            cpa_analyses.sort(key=lambda a: (impact_order.get(a.impact_level, 99), -(a.cpa_utilization or 0)))

            # Export to CSV if requested
            if output:
                try:
                    import csv

                    with open(output, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(
                            [
                                "campaign_name",
                                "ad_group_name",
                                "country",
                                "cpa_goal",
                                "avg_cpa",
                                "cpa_utilization_pct",
                                "impact_level",
                                "impressions_7d",
                                "impressions_30d",
                                "taps_7d",
                                "taps_30d",
                                "installs_30d",
                                "spend_30d",
                                "currency",
                                "recommendation",
                            ]
                        )
                        for a in cpa_analyses:
                            writer.writerow(
                                [
                                    a.campaign_name,
                                    a.ad_group_name,
                                    a.country,
                                    f"{a.cpa_goal:.2f}",
                                    f"{a.avg_cpa:.2f}" if a.avg_cpa else "",
                                    f"{a.cpa_utilization:.1f}" if a.cpa_utilization else "",
                                    a.impact_level,
                                    a.impressions_7d,
                                    a.impressions_30d,
                                    a.taps_7d,
                                    a.taps_30d,
                                    a.installs_30d,
                                    f"{a.spend_30d:.2f}",
                                    a.currency,
                                    a.recommendation,
                                ]
                            )
                    print_success(f"Exported {len(cpa_analyses)} ad groups to {output}")
                except Exception as e:
                    print_error("Export failed", str(e))

            # Display results table
            console.print()
            table = Table(title="CPA Cap Impact Analysis")
            table.add_column("Ad Group", style="cyan", max_width=22)
            table.add_column("Campaign", style="magenta", max_width=18)
            table.add_column("CPA Goal", justify="right", width=8)
            table.add_column("Avg CPA", justify="right", width=8)
            table.add_column("Impr 7d", justify="right", width=8)
            table.add_column("Impr 30d", justify="right", width=9)
            table.add_column("Taps 30d", justify="right", width=8)
            table.add_column("Impact", justify="center", width=10)

            for a in cpa_analyses[:50]:  # Limit display
                # Color code impact level
                impact = a.impact_level
                if impact == "BLOCKED":
                    impact_display = "[red bold]BLOCKED[/red bold]"
                elif impact == "CRITICAL":
                    impact_display = "[red bold]CRITICAL[/red bold]"
                elif impact == "HIGH":
                    impact_display = "[red]HIGH[/red]"
                elif impact == "MODERATE":
                    impact_display = "[yellow]MODERATE[/yellow]"
                elif impact == "LOW":
                    impact_display = "[green]LOW[/green]"
                else:
                    impact_display = "[dim]?[/dim]"

                avg_cpa_display = f"{a.avg_cpa:.2f}" if a.avg_cpa else "—"

                # Color code impressions (red if zero)
                impr_7d = "[red]0[/red]" if a.impressions_7d == 0 else f"{a.impressions_7d:,}"
                impr_30d = "[red]0[/red]" if a.impressions_30d == 0 else f"{a.impressions_30d:,}"

                table.add_row(
                    a.ad_group_name[:22],
                    a.campaign_name[:18],
                    f"{a.cpa_goal:.2f}",
                    avg_cpa_display,
                    impr_7d,
                    impr_30d,
                    f"{a.taps_30d:,}",
                    impact_display,
                )

            console.print(table)

            if len(cpa_analyses) > 50:
                print_info(f"Showing 50 of {len(cpa_analyses)} ad groups. Use --output to export all.")

            # Summary
            blocked = sum(1 for a in cpa_analyses if a.impact_level == "BLOCKED")
            critical = sum(1 for a in cpa_analyses if a.impact_level == "CRITICAL")
            high = sum(1 for a in cpa_analyses if a.impact_level == "HIGH")
            moderate = sum(1 for a in cpa_analyses if a.impact_level == "MODERATE")
            low = sum(1 for a in cpa_analyses if a.impact_level == "LOW")
            unknown = sum(1 for a in cpa_analyses if a.impact_level == "UNKNOWN")

            console.print()
            console.print("[bold]CPA Cap Impact Summary:[/bold]")
            if blocked > 0:
                console.print(f"  [red bold]Blocked (0 impressions):[/red bold] {blocked}")
            console.print(f"  [red bold]Critical:[/red bold] {critical}")
            console.print(f"  [red]High:[/red] {high}")
            console.print(f"  [yellow]Moderate:[/yellow] {moderate}")
            console.print(f"  [green]Low:[/green] {low}")
            if unknown > 0:
                console.print(f"  [dim]Unknown (no conversions):[/dim] {unknown}")

            console.print()
            console.print(f"[dim]Ad groups with CPA goals: {ad_groups_with_goals}[/dim]")
            console.print(f"[dim]Ad groups without CPA goals: {ad_groups_without_goals}[/dim]")

            if blocked + critical + high > 0:
                console.print()
                print_warning(f"{blocked + critical + high} ad groups are likely being limited by their CPA caps.")
                if blocked > 0:
                    console.print(
                        f"[yellow]  {blocked} ad groups have 0 impressions - consider removing CPA goals[/yellow]"
                    )

            # Interactive mode to remove CPA goals
            if interactive:
                # Filter to blocked/critical/high ad groups for interactive removal
                problem_groups = [a for a in cpa_analyses if a.impact_level in ("BLOCKED", "CRITICAL", "HIGH")]

                if not problem_groups:
                    print_info("No problem ad groups to process")
                    return

                console.print()
                console.rule("[bold]Interactive CPA Goal Management")
                console.print()
                console.print("[dim]For each ad group, choose an action:[/dim]")
                console.print("[dim]  • 'r' or 'remove' - Remove CPA goal[/dim]")
                console.print("[dim]  • 'i' or 'increase' - Increase CPA goal (enter new value)[/dim]")
                console.print("[dim]  • 's' or 'skip' - Skip this ad group[/dim]")
                console.print("[dim]  • 'q' or 'quit' - Stop processing[/dim]")
                console.print()

                changes_made = 0
                for i, ag in enumerate(problem_groups, 1):
                    console.rule(f"[bold]{i}/{len(problem_groups)}")
                    console.print()

                    # Show ad group details
                    impact_color = "red" if ag.impact_level in ("BLOCKED", "CRITICAL") else "yellow"
                    console.print(f"[bold]Ad Group:[/bold] {ag.ad_group_name}")
                    console.print(f"[bold]Campaign:[/bold] {ag.campaign_name} ({ag.country})")
                    console.print()
                    cpa_str = f"{ag.cpa_goal:.2f} {ag.currency}"
                    console.print(f"  CPA Goal:        [{impact_color}]{cpa_str}[/{impact_color}]")
                    if ag.avg_cpa:
                        console.print(f"  Actual Avg CPA:  {ag.avg_cpa:.2f} {ag.currency}")
                    console.print(f"  Impressions 7d:  {ag.impressions_7d:,}")
                    console.print(f"  Impressions 30d: {ag.impressions_30d:,}")
                    console.print(f"  Taps 30d:        {ag.taps_30d:,}")
                    console.print(f"  Installs 30d:    {ag.installs_30d:,}")
                    console.print(f"  Impact:          [{impact_color}]{ag.impact_level}[/{impact_color}]")
                    console.print()

                    action = (
                        typer.prompt(
                            "Action",
                            default="skip",
                            show_default=True,
                        )
                        .strip()
                        .lower()
                    )

                    if action in ("q", "quit"):
                        print_info("Quitting...")
                        break
                    elif action in ("s", "skip", ""):
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue
                    elif action in ("r", "remove"):
                        # Remove CPA goal by setting it to None
                        try:
                            with spinner("Removing CPA goal..."):
                                # Update ad group with cpa_goal set to remove it
                                # Note: The API may require a specific way to clear the CPA goal
                                client.campaigns(ag.campaign_id).ad_groups.update(
                                    ag.ad_group_id,
                                    AdGroupUpdate(cpa_goal=None),
                                )
                            print_success(f"Removed CPA goal from '{ag.ad_group_name}'")
                            changes_made += 1
                        except AppleSearchAdsError as e:
                            print_error("Failed to remove CPA goal", str(e))
                        console.print()
                    elif action in ("i", "increase"):
                        # Prompt for new CPA goal
                        suggested = ag.cpa_goal * Decimal("1.5")  # Suggest 50% increase
                        new_goal_str = typer.prompt(
                            f"New CPA goal ({ag.currency})",
                            default=f"{suggested:.2f}",
                        )
                        try:
                            new_goal = Decimal(new_goal_str)
                            if new_goal <= 0:
                                print_warning("Invalid CPA goal, skipping")
                                continue

                            with spinner("Updating CPA goal..."):
                                from asa_api_client.models import CpaGoal as CpaGoalModel

                                client.campaigns(ag.campaign_id).ad_groups.update(
                                    ag.ad_group_id,
                                    AdGroupUpdate(
                                        cpa_goal=CpaGoalModel(amount=Money(amount=str(new_goal), currency=ag.currency))
                                    ),
                                )
                            print_success(f"Updated CPA goal: {ag.cpa_goal:.2f} → {new_goal:.2f} {ag.currency}")
                            changes_made += 1
                        except ValueError:
                            print_warning(f"Invalid input '{new_goal_str}', skipping")
                        except AppleSearchAdsError as e:
                            print_error("Failed to update CPA goal", str(e))
                        console.print()
                    else:
                        console.print(f"[dim]Unknown action '{action}', skipping[/dim]")
                        console.print()

                # Summary
                console.print()
                if changes_made > 0:
                    print_result_panel(
                        "CPA Goal Management Complete",
                        {
                            "Ad groups reviewed": str(len(problem_groups)),
                            "Changes made": str(changes_made),
                        },
                    )
                else:
                    print_info("No changes made")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
