"""Optimization CLI commands for Apple Ads Platform API v1."""

import csv
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import typer
from asa_api_client.exceptions import AppleSearchAdsError, NotFoundError
from asa_api_client.v1.models.ad_groups import AdGroupCreate, AdGroupStatus, AdGroupUpdate, PricingModel
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.bulk import BulkKeywordCreate, BulkNegativeKeywordCreate
from asa_api_client.v1.models.campaigns import (
    BillingEvent,
    Campaign,
    CampaignCreate,
    CampaignStatus,
    CampaignTargeting,
    CampaignUpdate,
    DailyBudget,
    PromotedObjectType,
    SupplyPlacement,
    SupplySource,
)
from asa_api_client.v1.models.keywords import (
    Keyword,
    KeywordMatchType,
    KeywordStatus,
    KeywordUpdate,
    NegativeKeywordStatus,
)
from asa_api_client.v1.models.recommendations import (
    ApplyTargetCpaRecommendation,
    RecommendationFilterCondition,
    RecommendationFilterOperator,
    RecommendationPromotedObjectType,
    RecommendationQueryRequest,
    RecommendationState,
    TargetCpaRecommendation,
)
from asa_api_client.v1.models.reports import (
    AppsReportingRequest,
    ReportFilter,
    ReportFilterOperator,
    ReportGranularity,
    ReportTimeRange,
    ReportTimeZone,
)
from asa_api_client.v1.models.shared import BidStrategyType, TargetingData
from asa_api_client.v1.query import Query
from pydantic import BaseModel, Field
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    ad_group_bid_money,
    campaign_adam_id,
    campaign_countries,
    campaign_currency,
    campaign_daily_budget,
    console,
    enum_value,
    get_client,
    handle_api_error,
    manual_cpt_bid_strategy,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    require_id,
    spinner,
    target_cpa_bid_strategy,
)

app = typer.Typer(help="Optimization commands for campaigns")

T = TypeVar("T")


def wait_for_resource(
    check_fn: Callable[[], T],
    max_attempts: int = 10,
    delay: float = 0.5,
) -> T:
    """Wait for a resource to become available by polling."""
    for attempt in range(max_attempts):
        try:
            return check_fn()
        except NotFoundError:
            if attempt < max_attempts - 1:
                time.sleep(delay)
            else:
                raise
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
    """Check for bid discrepancies between MANUAL_CPT ad groups and keyword levels."""
    client = get_client()
    discrepancies: list[BidDiscrepancy] = []

    try:
        with client:
            with spinner("Scanning enabled campaigns..."):
                campaigns = list(client.campaigns.iter_all(Query().where("status", "EQUALS", "ENABLED")))

            print_info(f"Found {len(campaigns)} enabled campaigns")
            console.print()

            for campaign in campaigns:
                if campaign.id is None:
                    continue
                camp_name = campaign.name or str(campaign.id)
                with spinner(f"Scanning {camp_name}..."):
                    try:
                        ad_groups = list(
                            client.ad_groups.iter_all(
                                Query().where("campaignId", "EQUALS", campaign.id).where("status", "EQUALS", "ENABLED")
                            )
                        )
                        kw_query = Query().where("campaignId", "EQUALS", campaign.id)
                        all_keywords = list(client.keywords.iter_all(kw_query))
                    except AppleSearchAdsError:
                        continue

                    keywords_by_ag: dict[int, list[Keyword]] = defaultdict(list)
                    for kw in all_keywords:
                        if kw.ad_group_id is not None:
                            keywords_by_ag[kw.ad_group_id].append(kw)

                    for ag in ad_groups:
                        if ag.id is None:
                            continue
                        if ag.bid_strategy and enum_value(ag.bid_strategy.bid_strategy_type) not in (
                            "",
                            BidStrategyType.MANUAL_CPT.value,
                        ):
                            continue
                        ag_bid_money = ad_group_bid_money(ag)
                        if not ag_bid_money or ag_bid_money.amount is None:
                            continue

                        keywords = keywords_by_ag.get(ag.id, [])
                        if not keywords:
                            continue

                        keyword_bids = [
                            Decimal(kw.bid.amount)
                            for kw in keywords
                            if kw.bid and kw.bid.amount is not None and Decimal(kw.bid.amount) > 0
                        ]
                        if not keyword_bids:
                            continue

                        ad_group_bid = Decimal(ag_bid_money.amount)
                        keyword_avg = Decimal(sum(keyword_bids) / len(keyword_bids))
                        keyword_min = min(keyword_bids)
                        keyword_max = max(keyword_bids)
                        currency = ag_bid_money.currency or campaign_currency(campaign)

                        if ad_group_bid > 0:
                            diff_pct = float((keyword_avg - ad_group_bid) / ad_group_bid * 100)
                            if diff_pct >= threshold:
                                discrepancies.append(
                                    BidDiscrepancy(
                                        campaign_id=campaign.id,
                                        campaign_name=camp_name,
                                        ad_group_id=ag.id,
                                        ad_group_name=ag.name or str(ag.id),
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

            discrepancies.sort(key=lambda d: d.difference_pct, reverse=True)

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

            changes_made = 0
            for i, d in enumerate(discrepancies, 1):
                console.rule(f"[bold]{i}/{len(discrepancies)}")
                console.print()

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
                    new_bid = suggested_bid
                else:
                    console.print(f"[bold]Suggested new bid:[/bold] {_format_bid(suggested_bid, d.currency)}")
                    console.print()

                    action = typer.prompt(
                        "Action",
                        type=str,
                        default="apply",
                        show_default=True,
                        prompt_suffix=" [apply/custom/skip/quit]: ",
                    ).lower()

                    if action in ("quit", "q"):
                        print_info("Quitting...")
                        break
                    if action in ("skip", "s"):
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue
                    if action in ("custom", "c"):
                        new_bid_str = typer.prompt(f"Enter new bid ({d.currency})", type=str)
                        try:
                            new_bid = Decimal(new_bid_str)
                        except Exception:
                            print_warning("Invalid bid amount, skipping")
                            continue
                    elif action in ("apply", "a"):
                        new_bid = suggested_bid
                    else:
                        print_warning(f"Unknown action '{action}', skipping")
                        continue

                with spinner("Updating ad group bid..."):
                    client.ad_groups.update(
                        d.ad_group_id,
                        AdGroupUpdate(bid_strategy=manual_cpt_bid_strategy(new_bid, d.currency)),
                    )

                print_success(
                    f"Updated bid: {_format_bid(d.ad_group_bid, d.currency)} → {_format_bid(new_bid, d.currency)}"
                )
                changes_made += 1
                console.print()

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
    source_count: int
    impressions: int = 0
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
    campaign_type: str
    match_type: str
    original: str

    @classmethod
    def parse(cls, name: str) -> "CampaignNameParts | None":
        """Parse campaign name in format: App Name - Country - Type - Match."""
        parts = [p.strip() for p in name.split(" - ")]
        if len(parts) < 4:
            return None

        match_type_raw = parts[-1].upper()
        if match_type_raw in ("EM", "EXACT MATCH"):
            match_type = "EM"
        elif match_type_raw in ("BM", "BROAD MATCH", "SM", "SEARCH MATCH"):
            match_type = "BM"
        else:
            return None

        campaign_type = parts[-2]
        country = parts[1].upper()
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
    campaigns: list[Campaign],
    campaign_type_filter: str | None = None,
    match_type_filter: str | None = None,
) -> list[Campaign]:
    """Interactive campaign selection."""
    parsed_campaigns: list[tuple[Campaign, CampaignNameParts]] = []

    for c in campaigns:
        if not c.name:
            continue
        parsed = CampaignNameParts.parse(c.name)
        if parsed:
            if campaign_type_filter and parsed.campaign_type.lower() != campaign_type_filter.lower():
                continue
            if match_type_filter and parsed.match_type != match_type_filter.upper():
                continue
            parsed_campaigns.append((c, parsed))

    if not parsed_campaigns:
        return []

    apps: dict[str, list[tuple[Campaign, CampaignNameParts]]] = defaultdict(list)
    for c, parsed in parsed_campaigns:
        apps[parsed.app_name].append((c, parsed))

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

    campaign_list: list[tuple[Campaign, CampaignNameParts]] = []
    idx = 1
    sorted_apps = sorted(apps.keys())
    for app_idx, app_name in enumerate(sorted_apps):
        app_campaigns = sorted(apps[app_name], key=lambda x: (x[1].campaign_type, x[1].country))
        last_type = None
        for c, parsed in app_campaigns:
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
            status_str = enum_value(c.status)
            status_color = "green" if status_str == "ENABLED" else "yellow"

            table.add_row(
                str(idx),
                parsed.app_name[:20],
                parsed.country,
                parsed.campaign_type,
                parsed.match_type,
                f"[{status_color}]{status_str}[/{status_color}]",
                str(c.id),
            )
            idx += 1
            last_type = parsed.campaign_type
        if app_idx < len(sorted_apps) - 1:
            table.add_section()

    console.print(table)
    console.print()

    console.print("[dim]Enter campaign numbers separated by commas, ranges (1-3), or 'all'[/dim]")
    selection = typer.prompt("Select campaigns", default="all")

    if selection.lower() == "all":
        return [c for c, _ in campaign_list]

    selected_indices: set[int] = set()
    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start_s, end_s = part.split("-")
                selected_indices.update(range(int(start_s), int(end_s) + 1))
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
    """Expand SKAG campaigns to a new market."""
    client = get_client()

    try:
        with client:
            ad_account_id = int(client.ad_account_id or client.resolve_ad_account())
            source_campaign_data: list[Campaign] = []

            if source_campaigns:
                with spinner("Loading source campaigns..."):
                    for cid in source_campaigns:
                        try:
                            campaign = client.campaigns.get(cid)
                            source_campaign_data.append(campaign)
                        except AppleSearchAdsError as e:
                            print_error("Error", f"Could not load campaign {cid}: {e.message}")
                            raise typer.Exit(EXIT_ERROR) from None
            else:
                with spinner("Loading campaigns..."):
                    all_campaigns = list(client.campaigns.iter_all())

                source_campaign_data = _select_campaigns_interactive(
                    all_campaigns,
                    campaign_type_filter=campaign_type,
                    match_type_filter=match_type,
                )

            if not source_campaign_data:
                print_error("Error", "No campaigns selected")
                raise typer.Exit(EXIT_ERROR)

            console.print()
            print_info(f"Selected source campaigns ({len(source_campaign_data)}):")
            for c in source_campaign_data:
                ctries = campaign_countries(c)
                countries_str = ", ".join(ctries[:3])
                if len(ctries) > 3:
                    countries_str += "..."
                console.print(f"  • {c.name} ({countries_str})")
            console.print()

            if not target_country:
                target_country = typer.prompt("Target country code (e.g., CA, DE, FR)")
            target_country = target_country.upper() if target_country else ""

            adam_id = campaign_adam_id(source_campaign_data[0])
            if adam_id is None:
                print_error("Error", "Source campaign does not promote an App Store app (adamId missing)")
                raise typer.Exit(EXIT_ERROR)
            currency = campaign_currency(source_campaign_data[0])

            keyword_bids: dict[str, list[Decimal]] = defaultdict(list)
            keyword_impressions: dict[str, int] = defaultdict(int)

            end_date = date.today()
            start_date = end_date - timedelta(days=90)

            for campaign in source_campaign_data:
                if campaign.id is None:
                    continue
                with spinner(f"Fetching keyword performance for {campaign.name} (90 days)..."):
                    try:
                        req = AppsReportingRequest(
                            filters=[
                                ReportFilter(
                                    field="campaignId",
                                    operator=ReportFilterOperator.EQUALS,
                                    value=str(campaign.id),
                                )
                            ],
                            time_range=ReportTimeRange(
                                start=start_date,
                                end=end_date,
                                time_zone=ReportTimeZone.UTC,
                                granularity=ReportGranularity.DAILY,
                            ),
                        )
                        for row in client.reports.iter_keywords(req):
                            if row.metadata and row.metadata.text and row.total_metrics:
                                kw_text = row.metadata.text.lower()
                                impressions = row.total_metrics.impressions or 0
                                if impressions > 0:
                                    keyword_impressions[kw_text] += impressions
                                    if row.metadata.bid and row.metadata.bid.amount is not None:
                                        keyword_bids[kw_text].append(Decimal(row.metadata.bid.amount))
                                    else:
                                        keyword_bids.setdefault(kw_text, [Decimal("1.00")])
                    except AppleSearchAdsError as e:
                        print_warning(f"Could not get report for {campaign.name}: {e.message}")
                        continue

            active_keywords = set(keyword_impressions.keys())
            keyword_bids = {k: v for k, v in keyword_bids.items() if k in active_keywords and v}

            if not keyword_bids:
                print_error("Error", "No keywords with impressions found in last 90 days")
                raise typer.Exit(EXIT_ERROR)

            total_impressions = sum(keyword_impressions.values())
            print_info(f"Found {len(keyword_bids)} keywords with {total_impressions:,} impressions in last 90 days")

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

            keyword_plans.sort(key=lambda k: k.impressions, reverse=True)
            console.print()

            first_name = source_campaign_data[0].name or "Campaign"
            if campaign_name:
                plan_name = campaign_name
            else:
                parsed = CampaignNameParts.parse(first_name)
                plan_name = parsed.with_country(target_country) if parsed else f"{first_name} - {target_country}"

            if daily_budget is not None:
                plan_budget = Decimal(str(daily_budget))
            else:
                source_budgets: list[Decimal] = []
                for c in source_campaign_data:
                    bm = campaign_daily_budget(c)
                    if bm and bm.amount is not None:
                        source_budgets.append(Decimal(bm.amount))
                plan_budget = Decimal(sum(source_budgets) / len(source_budgets)) if source_budgets else Decimal("100")

            ad_group_plans: list[AdGroupPlan] = []
            all_keywords = [kp.text for kp in keyword_plans]

            for kp in keyword_plans:
                ag_name = f"Exact - {kp.text.title()}"
                if len(ag_name) > 200:
                    ag_name = ag_name[:197] + "..."
                negatives = [k for k in all_keywords if k != kp.text] if not skip_negatives else []
                ad_group_plans.append(AdGroupPlan(name=ag_name, keyword=kp, negatives=negatives))

            campaign_plan = CampaignPlan(
                name=plan_name,
                country=target_country.upper(),
                adam_id=adam_id,
                daily_budget=plan_budget,
                currency=currency,
                ad_groups=ad_group_plans,
            )

            console.print()
            console.rule("[bold]Campaign Plan")
            console.print()

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

            console.print("[bold]Ad Groups & Keywords (sorted by impressions):[/bold]")
            console.print()

            ag_table = Table(show_header=True, header_style="bold")
            ag_table.add_column("#", justify="right", style="dim")
            ag_table.add_column("Ad Group")
            ag_table.add_column("Keyword")
            ag_table.add_column("Bid", justify="right")
            ag_table.add_column("Impr (90d)", justify="right")

            for i, ag in enumerate(campaign_plan.ad_groups[:20], 1):
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

            if not typer.confirm("Create this campaign?", default=True):
                print_info("Cancelled")
                return

            console.print()

            with spinner("Creating campaign..."):
                new_campaign = client.campaigns.create(
                    CampaignCreate(
                        ad_account_id=ad_account_id,
                        name=campaign_plan.name,
                        billing_event=BillingEvent.TAPS,
                        promoted_object_type=PromotedObjectType.APPSTORE_APP,
                        promoted_object_id=str(campaign_plan.adam_id),
                        daily_budget=DailyBudget(
                            value=Money(amount=str(campaign_plan.daily_budget), currency=campaign_plan.currency),
                        ),
                        targeting=CampaignTargeting(
                            country_or_region=TargetingData(include=[campaign_plan.country]),
                            supply_source=TargetingData(include=[SupplySource.APPSTORE.value]),
                            supply_placement=TargetingData(include=[SupplyPlacement.APPSTORE_SEARCH_RESULTS.value]),
                        ),
                        status=CampaignStatus.PAUSED if paused else CampaignStatus.ENABLED,
                    )
                )

            new_campaign_id = require_id(new_campaign.id, "Campaign")
            print_success(f"Created campaign: {new_campaign.name} (ID: {new_campaign_id})")

            wait_for_resource(
                lambda: client.campaigns.get(new_campaign_id),
                max_attempts=10,
                delay=0.5,
            )

            created_ad_groups = 0
            created_keywords = 0
            created_negatives = 0

            print_info(f"Creating {len(campaign_plan.ad_groups)} ad groups...")

            for i, ag_plan in enumerate(campaign_plan.ad_groups, 1):
                with spinner(f"[{i}/{len(campaign_plan.ad_groups)}] Creating ad group: {ag_plan.name[:30]}..."):
                    new_ag = client.ad_groups.create(
                        AdGroupCreate(
                            campaign_id=new_campaign_id,
                            name=ag_plan.name,
                            pricing_model=PricingModel.CPT,
                            bid_strategy=manual_cpt_bid_strategy(ag_plan.keyword.bid, ag_plan.keyword.currency),
                            automated_keywords_opt_in=False,
                            status=AdGroupStatus.ENABLED,
                        )
                    )
                    new_ag_id = require_id(new_ag.id, "Ad group")
                    created_ad_groups += 1

                print_success(f"[{i}/{len(campaign_plan.ad_groups)}] Created ad group: {new_ag.name} (ID: {new_ag_id})")

                client.bulk.create_keywords(
                    [
                        BulkKeywordCreate(
                            ad_group_id=new_ag_id,
                            text=ag_plan.keyword.text,
                            match_type=KeywordMatchType.EXACT,
                            bid=Money(amount=str(ag_plan.keyword.bid), currency=ag_plan.keyword.currency),
                            status=KeywordStatus.ENABLED,
                        )
                    ]
                )
                created_keywords += 1

                if ag_plan.negatives:
                    try:
                        neg_items = [
                            BulkNegativeKeywordCreate(
                                campaign_id=new_campaign_id,
                                ad_group_id=new_ag_id,
                                text=neg_text,
                                match_type=KeywordMatchType.EXACT,
                                status=NegativeKeywordStatus.ENABLED,
                            )
                            for neg_text in ag_plan.negatives
                        ]
                        results = client.bulk.create_negative_keywords(neg_items, allow_partial_success=True)
                        created_negatives += sum(1 for r in results if r.success)
                    except AppleSearchAdsError:
                        pass

            console.print()
            print_result_panel(
                "Campaign Created Successfully",
                {
                    "Campaign ID": str(new_campaign_id),
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
    avg_cpt: Decimal | None
    ttr: float | None
    cr: float | None
    country: str
    suggested_bid: Decimal | None = None

    @property
    def bid_strength(self) -> str:
        """Estimate bid strength based on performance metrics."""
        if self.impressions == 0:
            return "UNKNOWN"
        ttr = self.ttr or 0
        if self.impressions >= 1000 and ttr >= 0.05:
            return "STRONG"
        if self.impressions >= 100 and ttr >= 0.02:
            return "MODERATE"
        if self.impressions > 0:
            return "WEAK"
        return "UNKNOWN"

    @property
    def recommendation(self) -> str:
        """Suggest bid adjustment based on performance."""
        strength = self.bid_strength
        if strength == "STRONG":
            return "Consider increase for more volume"
        if strength == "MODERATE":
            return "Monitor performance"
        if strength == "WEAK":
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
    """Review keyword bids and their performance."""
    client = get_client()

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    try:
        with client:
            with spinner("Loading campaigns..."):
                campaigns = list(client.campaigns.iter_all(Query().where("status", "EQUALS", "ENABLED")))

            if country:
                country_upper = country.upper()
                campaigns = [c for c in campaigns if country_upper in [cc.upper() for cc in campaign_countries(c)]]

            if not campaigns:
                print_warning("No enabled campaigns found" + (f" for {country}" if country else ""))
                return

            print_info(f"Analyzing {len(campaigns)} campaigns...")

            keyword_analyses: list[KeywordBidAnalysis] = []

            for campaign in campaigns:
                if campaign.id is None:
                    continue
                ctries = campaign_countries(campaign)
                campaign_country = ctries[0] if ctries else "?"
                camp_name = campaign.name or str(campaign.id)

                with spinner(f"Fetching keyword data for {camp_name[:30]}..."):
                    try:
                        req = AppsReportingRequest(
                            filters=[
                                ReportFilter(
                                    field="campaignId",
                                    operator=ReportFilterOperator.EQUALS,
                                    value=str(campaign.id),
                                )
                            ],
                            time_range=ReportTimeRange(
                                start=start_date,
                                end=end_date,
                                time_zone=ReportTimeZone.UTC,
                                granularity=ReportGranularity.DAILY,
                            ),
                        )
                        for row in client.reports.iter_keywords(req):
                            meta = row.metadata
                            total = row.total_metrics
                            if not meta or not meta.text or not total or not meta.ad_group_id:
                                continue
                            if meta.deleted or (meta.ad_group and meta.ad_group.deleted):
                                continue
                            if meta.status and enum_value(meta.status) not in ("ACTIVE", "ENABLED"):
                                continue

                            impressions = total.impressions or 0
                            taps = total.taps or 0
                            conversions = total.tap_installs or 0
                            spend_amount = (
                                total.local_spend.amount
                                if total.local_spend and total.local_spend.amount is not None
                                else "0"
                            )
                            spend = Decimal(str(spend_amount))
                            currency = (
                                total.local_spend.currency
                                if total.local_spend and total.local_spend.currency
                                else campaign_currency(campaign)
                            )

                            avg_cpt = spend / taps if taps > 0 else None
                            ttr = taps / impressions if impressions > 0 else None
                            cr = conversions / taps if taps > 0 else None

                            bid_amount = meta.bid.amount if meta.bid and meta.bid.amount is not None else "0"
                            bid = Decimal(str(bid_amount))

                            suggested_bid: Decimal | None = None
                            if (
                                row.insights
                                and row.insights.bid_recommendation
                                and row.insights.bid_recommendation.suggested_bid_amount is not None
                            ):
                                suggested_bid = Decimal(str(row.insights.bid_recommendation.suggested_bid_amount))

                            keyword_analyses.append(
                                KeywordBidAnalysis(
                                    campaign_id=campaign.id,
                                    campaign_name=camp_name,
                                    ad_group_id=meta.ad_group_id,
                                    ad_group_name=meta.ad_group.name if meta.ad_group and meta.ad_group.name else "",
                                    keyword_id=meta.id or 0,
                                    keyword_text=meta.text,
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
                                    suggested_bid=suggested_bid,
                                )
                            )
                    except AppleSearchAdsError:
                        continue

            if not keyword_analyses:
                print_warning("No keyword data found")
                return

            keyword_analyses = [k for k in keyword_analyses if k.bid_strength != "UNKNOWN"]

            if min_impressions > 0:
                keyword_analyses = [k for k in keyword_analyses if k.impressions >= min_impressions]

            if weak_only:
                keyword_analyses = [k for k in keyword_analyses if k.bid_strength == "WEAK"]

            if not keyword_analyses:
                print_warning("No keywords match the specified filters")
                return

            keyword_analyses.sort(key=lambda k: k.impressions, reverse=True)

            if output:
                try:
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
                strength = k.bid_strength
                strength_display = {
                    "STRONG": "[green]STRONG[/green]",
                    "MODERATE": "[yellow]MODERATE[/yellow]",
                    "WEAK": "[red]WEAK[/red]",
                }.get(strength, "[dim]?[/dim]")
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

            if interactive:
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
                    if not kw.ad_group_id:
                        continue

                    console.rule(f"[bold]{i}/{len(adjustable)}")
                    console.print()

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

                    if kw.suggested_bid is not None:
                        suggested = round(kw.suggested_bid, 2)
                    elif kw.bid_strength == "WEAK":
                        suggested = round(kw.current_bid * Decimal("1.20"), 2)
                    else:
                        suggested = round(kw.current_bid * Decimal("1.10"), 2)

                    console.print(f"[bold]Suggested:[/bold] {suggested:.2f} {kw.currency}")
                    console.print()

                    action = typer.prompt("New bid", default=str(suggested), show_default=True).strip().lower()

                    if action in ("q", "quit"):
                        print_info("Quitting...")
                        break
                    if action in ("s", "skip", ""):
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue

                    try:
                        if action.startswith("+"):
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

                    target_keyword_id = kw.keyword_id
                    if not target_keyword_id:
                        try:
                            with spinner("Fetching keywords from ad group..."):
                                keywords = list(
                                    client.keywords.iter_all(Query().where("adGroupId", "EQUALS", kw.ad_group_id))
                                )
                            actual_keyword = next(
                                (
                                    k
                                    for k in keywords
                                    if k.text
                                    and k.text.lower() == kw.keyword_text.lower()
                                    and enum_value(k.status) in ("ENABLED", "ACTIVE")
                                    and k.id is not None
                                ),
                                None,
                            )
                            if not actual_keyword or actual_keyword.id is None:
                                print_warning(f"Keyword '{kw.keyword_text}' not found in ad group")
                                continue
                            target_keyword_id = actual_keyword.id
                        except AppleSearchAdsError as e:
                            print_error("Failed to fetch keywords", e.message)
                            continue

                    try:
                        with spinner(f"Updating keyword {target_keyword_id}..."):
                            client.keywords.update(
                                target_keyword_id,
                                KeywordUpdate(bid=Money(amount=str(new_bid), currency=kw.currency)),
                            )
                    except AppleSearchAdsError as e:
                        print_error("Update failed", f"{e.message} (keyword_id={target_keyword_id})")
                        continue

                    print_success(f"Updated: {kw.current_bid:.2f} → {new_bid:.2f} {kw.currency}")
                    changes_made += 1
                    console.print()

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
    """Analysis of Target CPA impact on MAX_CONVERSIONS ad group delivery."""

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
    scope: Literal["ad_group", "campaign"] = "ad_group"
    recommended_target_cpa: Decimal | None = None
    recommendation_id: str | None = None
    promoted_object_id: str | None = None
    promoted_object_type: str | None = None

    @property
    def cpa_utilization(self) -> float | None:
        """Percentage of Target CPA utilized (avg_cpa / cpa_goal * 100)."""
        if self.avg_cpa is None or self.cpa_goal == 0:
            return None
        return float(self.avg_cpa / self.cpa_goal * 100)

    @property
    def impact_level(self) -> str:
        """Estimate impact level of Target CPA on delivery."""
        if self.impressions_30d == 0:
            return "BLOCKED"
        util = self.cpa_utilization
        if util is None:
            return "UNKNOWN"
        if util >= 95:
            return "CRITICAL"
        if util >= 85:
            return "HIGH"
        if util >= 70:
            return "MODERATE"
        return "LOW"

    @property
    def recommendation(self) -> str:
        """Suggest action based on Target CPA impact."""
        if self.recommended_target_cpa is not None:
            return f"Apple recommends Target CPA {self.recommended_target_cpa:.2f} {self.currency}"
        level = self.impact_level
        if level == "BLOCKED":
            return "Remove or raise Target CPA - blocking delivery"
        if level == "CRITICAL":
            return "Increase Target CPA"
        if level == "HIGH":
            return "Consider increasing Target CPA"
        if level == "MODERATE":
            return "Monitor - may become limited"
        if level == "LOW":
            return "Target CPA not limiting delivery"
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
        typer.Option("--critical", help="Only show ad groups with critical/high/blocked Target CPA impact"),
    ] = False,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Interactive mode to adjust or remove Target CPA"),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Export to CSV file"),
    ] = None,
) -> None:
    """Analyze Target CPA impact on Maximize Conversions (MAX_CONVERSIONS) ad groups.

    In Platform API v1, Target CPA lives on MAX_CONVERSIONS ad groups/campaigns
    and Apple surfaces Target CPA recommendations via /v1/recommendations/target-cpas.
    """
    client = get_client()

    end_date = date.today() - timedelta(days=1)
    start_date_30d = end_date - timedelta(days=30)
    start_date_7d = end_date - timedelta(days=7)

    try:
        with client:
            with spinner("Loading campaigns..."):
                campaigns = list(client.campaigns.iter_all(Query().where("status", "EQUALS", "ENABLED")))

            if country:
                country_upper = country.upper()
                campaigns = [c for c in campaigns if country_upper in [cc.upper() for cc in campaign_countries(c)]]

            if not campaigns:
                print_warning("No enabled campaigns found" + (f" for {country}" if country else ""))
                return

            print_info(f"Analyzing {len(campaigns)} campaigns for Target CPA impact...")

            # Fetch AVAILABLE Target CPA recommendations per promoted object
            tcpa_recs_by_campaign: dict[int, TargetCpaRecommendation] = {}
            promoted_objects: set[tuple[str, str]] = set()
            for camp in campaigns:
                if camp.promoted_object_id and camp.promoted_object_type:
                    promoted_objects.add((camp.promoted_object_id, enum_value(camp.promoted_object_type)))

            for pid, ptype in promoted_objects:
                try:
                    rec_query = RecommendationQueryRequest.for_promoted_object(
                        pid,
                        ptype,
                        filters=[
                            RecommendationFilterCondition(
                                field="state",
                                operator=RecommendationFilterOperator.EQUALS,
                                value=[RecommendationState.AVAILABLE.value],
                            )
                        ],
                    )
                    for rec in client.recommendations.iter_target_cpas(rec_query):
                        if rec.campaign_id is not None:
                            tcpa_recs_by_campaign[rec.campaign_id] = rec
                except AppleSearchAdsError:
                    continue

            cpa_analyses: list[CpaCapAnalysis] = []
            ad_groups_with_goals = 0
            ad_groups_without_goals = 0

            for campaign in campaigns:
                if campaign.id is None:
                    continue
                ctries = campaign_countries(campaign)
                campaign_country = ctries[0] if ctries else "?"
                camp_name = campaign.name or str(campaign.id)

                camp_tcpa: Money | None = None
                if (
                    campaign.bid_strategy
                    and enum_value(campaign.bid_strategy.bid_strategy_type) == BidStrategyType.MAX_CONVERSIONS.value
                    and campaign.bid_strategy.bid
                    and campaign.bid_strategy.bid.amount is not None
                    and Decimal(campaign.bid_strategy.bid.amount) > 0
                ):
                    camp_tcpa = campaign.bid_strategy.bid

                with spinner(f"Analyzing {camp_name[:40]}..."):
                    try:
                        ad_groups = list(
                            client.ad_groups.iter_all(
                                Query().where("campaignId", "EQUALS", campaign.id).where("status", "EQUALS", "ENABLED")
                            )
                        )

                        ad_groups_with_cpa: list[tuple[Any, Decimal, str, Literal["ad_group", "campaign"]]] = []
                        for ag in ad_groups:
                            ag_tcpa: Money | None = None
                            scope: Literal["ad_group", "campaign"] = "ad_group"
                            if (
                                ag.bid_strategy
                                and enum_value(ag.bid_strategy.bid_strategy_type)
                                == BidStrategyType.MAX_CONVERSIONS.value
                            ):
                                ag_tcpa = ad_group_bid_money(ag)
                            elif ag.bid_strategy is None and camp_tcpa is not None:
                                ag_tcpa = camp_tcpa
                                scope = "campaign"

                            if ag_tcpa and ag_tcpa.amount is not None and Decimal(ag_tcpa.amount) > 0:
                                ad_groups_with_cpa.append(
                                    (
                                        ag,
                                        Decimal(ag_tcpa.amount),
                                        ag_tcpa.currency or campaign_currency(campaign),
                                        scope,
                                    )
                                )
                                ad_groups_with_goals += 1
                            else:
                                ad_groups_without_goals += 1

                        if not ad_groups_with_cpa:
                            continue

                        camp_id = require_id(campaign.id, "campaign")

                        def fetch_ag_perf(
                            start_d: date,
                            cid: int = camp_id,
                            default_curr: str = campaign_currency(campaign),
                        ) -> dict[int, dict[str, Any]]:
                            perf: dict[int, dict[str, Any]] = {}
                            req = AppsReportingRequest(
                                filters=[
                                    ReportFilter(
                                        field="campaignId",
                                        operator=ReportFilterOperator.EQUALS,
                                        value=str(cid),
                                    )
                                ],
                                time_range=ReportTimeRange(
                                    start=start_d,
                                    end=end_date,
                                    time_zone=ReportTimeZone.UTC,
                                    granularity=ReportGranularity.DAILY,
                                ),
                            )
                            for row in client.reports.iter_ad_groups(req):
                                if row.metadata and row.metadata.id is not None and row.total_metrics:
                                    ag_id = row.metadata.id
                                    tm = row.total_metrics
                                    if ag_id not in perf:
                                        perf[ag_id] = {
                                            "impressions": 0,
                                            "taps": 0,
                                            "installs": 0,
                                            "spend": Decimal("0"),
                                            "currency": default_curr,
                                        }
                                    perf[ag_id]["impressions"] += tm.impressions or 0
                                    perf[ag_id]["taps"] += tm.taps or 0
                                    perf[ag_id]["installs"] += tm.tap_installs or 0
                                    if tm.local_spend and tm.local_spend.amount is not None:
                                        perf[ag_id]["spend"] += Decimal(str(tm.local_spend.amount))
                                        if tm.local_spend.currency:
                                            perf[ag_id]["currency"] = tm.local_spend.currency
                            return perf

                        ag_perf_30d = fetch_ag_perf(start_date_30d)
                        ag_perf_7d = fetch_ag_perf(start_date_7d)

                        camp_rec = tcpa_recs_by_campaign.get(campaign.id)
                        rec_tcpa_dec = (
                            Decimal(camp_rec.recommended_target_cpa.amount)
                            if camp_rec
                            and camp_rec.recommended_target_cpa
                            and camp_rec.recommended_target_cpa.amount is not None
                            else None
                        )

                        for ag, cpa_goal, goal_currency, scope in ad_groups_with_cpa:
                            if ag.id is None:
                                continue
                            perf_30d = ag_perf_30d.get(ag.id, {})
                            perf_7d = ag_perf_7d.get(ag.id, {})

                            impressions_30d = perf_30d.get("impressions", 0)
                            impressions_7d = perf_7d.get("impressions", 0)
                            taps_30d = perf_30d.get("taps", 0)
                            taps_7d = perf_7d.get("taps", 0)
                            installs_30d = perf_30d.get("installs", 0)
                            spend_30d = perf_30d.get("spend", Decimal("0"))
                            currency = perf_30d.get("currency", goal_currency)

                            avg_cpa = (spend_30d / installs_30d) if installs_30d > 0 and spend_30d > 0 else None

                            cpa_analyses.append(
                                CpaCapAnalysis(
                                    campaign_id=campaign.id,
                                    campaign_name=camp_name,
                                    ad_group_id=ag.id,
                                    ad_group_name=ag.name or str(ag.id),
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
                                    scope=scope,
                                    recommended_target_cpa=rec_tcpa_dec,
                                    recommendation_id=camp_rec.id if camp_rec else None,
                                    promoted_object_id=campaign.promoted_object_id,
                                    promoted_object_type=enum_value(campaign.promoted_object_type),
                                )
                            )
                    except AppleSearchAdsError:
                        continue

            if not cpa_analyses:
                print_warning("No MAX_CONVERSIONS ad groups with Target CPA found")
                console.print(
                    f"\n[dim]Ad groups scanned: {ad_groups_without_goals} "
                    "(none had MAX_CONVERSIONS Target CPA set)[/dim]"
                )
                return

            if min_installs > 0:
                cpa_analyses = [a for a in cpa_analyses if a.installs_30d >= min_installs]
                if not cpa_analyses:
                    print_warning(f"No ad groups with Target CPA have at least {min_installs} installs")
                    return

            if critical_only:
                cpa_analyses = [a for a in cpa_analyses if a.impact_level in ("BLOCKED", "CRITICAL", "HIGH")]
                if not cpa_analyses:
                    print_success("No ad groups with critical, high, or blocked Target CPA impact")
                    return

            impact_order = {"BLOCKED": 0, "CRITICAL": 1, "HIGH": 2, "UNKNOWN": 3, "MODERATE": 4, "LOW": 5}
            cpa_analyses.sort(key=lambda a: (impact_order.get(a.impact_level, 99), -(a.cpa_utilization or 0)))

            if output:
                try:
                    with open(output, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(
                            [
                                "campaign_name",
                                "ad_group_name",
                                "country",
                                "target_cpa",
                                "recommended_target_cpa",
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
                                    f"{a.recommended_target_cpa:.2f}" if a.recommended_target_cpa else "",
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

            console.print()
            table = Table(title="Target CPA Impact Analysis (MAX_CONVERSIONS)")
            table.add_column("Ad Group", style="cyan", max_width=22)
            table.add_column("Campaign", style="magenta", max_width=18)
            table.add_column("Target CPA", justify="right", width=10)
            table.add_column("Avg CPA", justify="right", width=8)
            table.add_column("Impr 7d", justify="right", width=8)
            table.add_column("Impr 30d", justify="right", width=9)
            table.add_column("Taps 30d", justify="right", width=8)
            table.add_column("Impact", justify="center", width=10)

            for a in cpa_analyses[:50]:
                impact = a.impact_level
                impact_display = {
                    "BLOCKED": "[red bold]BLOCKED[/red bold]",
                    "CRITICAL": "[red bold]CRITICAL[/red bold]",
                    "HIGH": "[red]HIGH[/red]",
                    "MODERATE": "[yellow]MODERATE[/yellow]",
                    "LOW": "[green]LOW[/green]",
                }.get(impact, "[dim]?[/dim]")

                avg_cpa_display = f"{a.avg_cpa:.2f}" if a.avg_cpa else "—"
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

            blocked = sum(1 for a in cpa_analyses if a.impact_level == "BLOCKED")
            critical = sum(1 for a in cpa_analyses if a.impact_level == "CRITICAL")
            high = sum(1 for a in cpa_analyses if a.impact_level == "HIGH")
            moderate = sum(1 for a in cpa_analyses if a.impact_level == "MODERATE")
            low = sum(1 for a in cpa_analyses if a.impact_level == "LOW")
            unknown = sum(1 for a in cpa_analyses if a.impact_level == "UNKNOWN")

            console.print()
            console.print("[bold]Target CPA Impact Summary:[/bold]")
            if blocked > 0:
                console.print(f"  [red bold]Blocked (0 impressions):[/red bold] {blocked}")
            console.print(f"  [red bold]Critical:[/red bold] {critical}")
            console.print(f"  [red]High:[/red] {high}")
            console.print(f"  [yellow]Moderate:[/yellow] {moderate}")
            console.print(f"  [green]Low:[/green] {low}")
            if unknown > 0:
                console.print(f"  [dim]Unknown (no conversions):[/dim] {unknown}")

            console.print()
            console.print(f"[dim]Ad groups with Target CPA: {ad_groups_with_goals}[/dim]")
            console.print(f"[dim]Ad groups without Target CPA: {ad_groups_without_goals}[/dim]")

            if interactive:
                problem_groups = [a for a in cpa_analyses if a.impact_level in ("BLOCKED", "CRITICAL", "HIGH")]
                if not problem_groups:
                    print_info("No problem ad groups to process")
                    return

                console.print()
                console.rule("[bold]Interactive Target CPA Management")
                console.print()
                console.print("[dim]For each ad group, choose an action:[/dim]")
                console.print("[dim]  • 'r' or 'remove' - Clear Target CPA cap on MAX_CONVERSIONS[/dim]")
                console.print("[dim]  • 'i' or 'increase' - Increase Target CPA (enter new value)[/dim]")
                console.print("[dim]  • 'a' or 'apply' - Apply Apple's recommended Target CPA (if available)[/dim]")
                console.print("[dim]  • 's' or 'skip' - Skip this ad group[/dim]")
                console.print("[dim]  • 'q' or 'quit' - Stop processing[/dim]")
                console.print()

                changes_made = 0
                for i, analysis in enumerate(problem_groups, 1):
                    console.rule(f"[bold]{i}/{len(problem_groups)}")
                    console.print()

                    impact_color = "red" if analysis.impact_level in ("BLOCKED", "CRITICAL") else "yellow"
                    console.print(f"[bold]Ad Group:[/bold] {analysis.ad_group_name} [dim]({analysis.scope})[/dim]")
                    console.print(f"[bold]Campaign:[/bold] {analysis.campaign_name} ({analysis.country})")
                    console.print()
                    cpa_str = f"{analysis.cpa_goal:.2f} {analysis.currency}"
                    console.print(f"  Target CPA:      [{impact_color}]{cpa_str}[/{impact_color}]")
                    if analysis.recommended_target_cpa is not None:
                        console.print(
                            f"  Apple Suggested: [green]{analysis.recommended_target_cpa:.2f} "
                            f"{analysis.currency}[/green]"
                        )
                    if analysis.avg_cpa:
                        console.print(f"  Actual Avg CPA:  {analysis.avg_cpa:.2f} {analysis.currency}")
                    console.print(f"  Impressions 7d:  {analysis.impressions_7d:,}")
                    console.print(f"  Impressions 30d: {analysis.impressions_30d:,}")
                    console.print(f"  Installs 30d:    {analysis.installs_30d:,}")
                    console.print(f"  Impact:          [{impact_color}]{analysis.impact_level}[/{impact_color}]")
                    console.print()

                    action = typer.prompt("Action", default="skip", show_default=True).strip().lower()

                    if action in ("q", "quit"):
                        print_info("Quitting...")
                        break
                    if action in ("s", "skip", ""):
                        console.print("[dim]Skipped[/dim]")
                        console.print()
                        continue
                    if action in ("a", "apply") and analysis.recommendation_id and analysis.promoted_object_id:
                        try:
                            with spinner("Applying Apple Target CPA recommendation..."):
                                ptype_enum = RecommendationPromotedObjectType(
                                    analysis.promoted_object_type or "APPSTORE_APP"
                                )
                                client.recommendations.apply_target_cpas(
                                    [
                                        ApplyTargetCpaRecommendation(
                                            id=analysis.recommendation_id,
                                            promoted_object_id=analysis.promoted_object_id,
                                            promoted_object_type=ptype_enum,
                                        )
                                    ]
                                )
                            print_success(f"Applied recommended Target CPA for '{analysis.campaign_name}'")
                            changes_made += 1
                        except AppleSearchAdsError as e:
                            print_error("Failed to apply recommendation", str(e))
                        console.print()
                    elif action in ("r", "remove"):
                        try:
                            with spinner("Clearing Target CPA cap..."):
                                if analysis.scope == "campaign":
                                    client.campaigns.update(
                                        analysis.campaign_id,
                                        CampaignUpdate(bid_strategy=target_cpa_bid_strategy(None)),
                                    )
                                else:
                                    client.ad_groups.update(
                                        analysis.ad_group_id,
                                        AdGroupUpdate(bid_strategy=target_cpa_bid_strategy(None)),
                                    )
                            print_success(f"Cleared Target CPA cap from '{analysis.ad_group_name}'")
                            changes_made += 1
                        except AppleSearchAdsError as e:
                            print_error("Failed to clear Target CPA", str(e))
                        console.print()
                    elif action in ("i", "increase"):
                        suggested = analysis.recommended_target_cpa or (analysis.cpa_goal * Decimal("1.5"))
                        new_goal_str = typer.prompt(
                            f"New Target CPA ({analysis.currency})",
                            default=f"{suggested:.2f}",
                        )
                        try:
                            new_goal = Decimal(new_goal_str)
                            if new_goal <= 0:
                                print_warning("Invalid Target CPA, skipping")
                                continue

                            with spinner("Updating Target CPA..."):
                                new_bs = target_cpa_bid_strategy(new_goal, analysis.currency)
                                if analysis.scope == "campaign":
                                    client.campaigns.update(
                                        analysis.campaign_id,
                                        CampaignUpdate(bid_strategy=new_bs),
                                    )
                                else:
                                    client.ad_groups.update(
                                        analysis.ad_group_id,
                                        AdGroupUpdate(bid_strategy=new_bs),
                                    )
                            print_success(
                                f"Updated Target CPA: {analysis.cpa_goal:.2f} → {new_goal:.2f} {analysis.currency}"
                            )
                            changes_made += 1
                        except ValueError:
                            print_warning(f"Invalid input '{new_goal_str}', skipping")
                        except AppleSearchAdsError as e:
                            print_error("Failed to update Target CPA", str(e))
                        console.print()
                    else:
                        console.print(f"[dim]Unknown action '{action}', skipping[/dim]")
                        console.print()

                console.print()
                if changes_made > 0:
                    print_result_panel(
                        "Target CPA Management Complete",
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


# ============================================================================
# Negative Keyword Suggestions (AI-Powered)
# ============================================================================


@dataclass
class SearchTermAnalysis:
    """A search term with performance data for negative keyword analysis."""

    search_term: str
    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    impressions: int
    taps: int
    installs: int
    spend: Decimal
    currency: str
    matched_keyword: str | None = None


class NegativeSuggestion(BaseModel):  # type: ignore[misc]
    """AI suggestion for a single search term."""

    search_term: str = Field(description="The search term being evaluated")
    action: Literal["negative", "keep", "review"] = Field(
        description="Whether to add as negative keyword, keep, or flag for manual review"
    )
    reason: str = Field(description="Brief explanation for the recommendation")


class NegativeKeywordSuggestions(BaseModel):  # type: ignore[misc]
    """Batch of negative keyword suggestions from AI analysis."""

    suggestions: list[NegativeSuggestion] = Field(description="List of suggestions for each search term")


NEGATIVE_KEYWORD_SYSTEM_PROMPT = (
    "You are an expert Apple Search Ads optimiser specialising in "
    "negative keyword strategy.\n\n"
    "You will receive a list of search terms from App Store Search Ads "
    "campaigns along with their performance metrics and the app description.\n\n"
    "Your job is to classify each search term as:\n"
    '- "negative": Irrelevant to the app — add as a negative keyword to stop wasting spend.\n'
    '- "keep": Relevant and performing well or worth testing further.\n'
    '- "review": Ambiguous — may be relevant but performance is poor, needs human judgement.\n\n'
    "Guidelines:\n"
    "1. A search term is irrelevant if it clearly targets a different app, product, or intent.\n"
    '2. High spend with zero installs over a meaningful period is a strong signal for "negative".\n'
    "3. Low-volume terms (few impressions/taps) with no installs may just need more data "
    '— prefer "review" over "negative" unless clearly irrelevant.\n'
    "4. Brand names of competitors are usually worth keeping unless spend is excessive.\n"
    '5. Generic terms tangentially related to the app deserve "review", not automatic "negative".\n'
    "6. Consider the app context provided — what the app does and who it serves.\n"
    '7. Be conservative: only recommend "negative" when you are confident the term is irrelevant.'
)


def _get_negative_analysis_agent(
    settings: Any,
    provider: str | None = None,
) -> Any:
    """Create a PydanticAI agent for negative keyword analysis."""
    from pydantic_ai import Agent
    from pydantic_ai.models import Model

    active_provider = provider or settings.translate_provider

    model: Model
    if active_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for AI analysis.\n"
                "Add it to your .env file or set the environment variable."
            )
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key=settings.anthropic_api_key))
    elif active_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is required for AI analysis.\nAdd it to your .env file or set the environment variable."
            )
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        model = GoogleModel("gemini-2.0-flash", provider=GoogleProvider(api_key=settings.gemini_api_key))
    else:
        raise ValueError(f"Unknown provider: {active_provider}")

    return Agent(
        model,
        output_type=NegativeKeywordSuggestions,
        system_prompt=NEGATIVE_KEYWORD_SYSTEM_PROMPT,
    )


async def _analyse_search_terms(
    terms: list[SearchTermAnalysis],
    app_description: str,
    settings: Any,
    provider: str | None = None,
) -> NegativeKeywordSuggestions:
    """Send search terms to AI for negative keyword classification."""
    agent = _get_negative_analysis_agent(settings, provider)

    lines = []
    for t in terms:
        cpt = t.spend / t.taps if t.taps > 0 else Decimal("0")
        lines.append(
            f'- "{t.search_term}" | matched: "{t.matched_keyword or "?"}" '
            f"| impr: {t.impressions} | taps: {t.taps} | installs: {t.installs} "
            f"| spend: {t.spend:.2f} {t.currency} | CPT: {cpt:.2f}"
        )

    prompt = f"""App context: {app_description}

Analyse these search terms and recommend which should be added as negative keywords:

{chr(10).join(lines)}

Classify each search term as "negative", "keep", or "review" with a brief reason."""

    result = await agent.run(prompt)
    return result.output  # type: ignore[no-any-return]


@app.command("negatives")
def negatives(
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign-id", "-c", help="Analyse a single campaign (default: all campaigns)"),
    ] = None,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Days of search term data to analyse"),
    ] = 30,
    min_spend: Annotated[
        float,
        typer.Option("--min-spend", help="Minimum spend to include a search term"),
    ] = 0.0,
    min_impressions: Annotated[
        int,
        typer.Option("--min-impressions", help="Minimum impressions to include a search term"),
    ] = 0,
    country: Annotated[
        str | None,
        typer.Option("--country", help="Filter campaigns by country code (e.g. US, GB)"),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="AI provider: anthropic or gemini"),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file with API keys"),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply suggested negatives (campaign-level exact match)"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show suggestions without applying changes"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Export suggestions to CSV file"),
    ] = None,
) -> None:
    """Analyse search terms with AI and suggest negative keywords."""
    import asyncio

    from asa_api_cli.translate import get_translate_settings

    try:
        settings = get_translate_settings(env_file)
    except Exception as e:
        print_error("Configuration Error", str(e))
        raise typer.Exit(EXIT_ERROR) from None

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    client = get_client()

    try:
        with client:
            if campaign_id:
                with spinner("Fetching campaign..."):
                    campaign = client.campaigns.get(campaign_id)
                target_campaigns = [campaign]
            else:
                with spinner("Loading campaigns..."):
                    target_campaigns = list(client.campaigns.iter_all(Query().where("status", "EQUALS", "ENABLED")))

            if country:
                country_upper = country.upper()
                target_campaigns = [
                    c for c in target_campaigns if country_upper in [cc.upper() for cc in campaign_countries(c)]
                ]

            if not target_campaigns:
                print_warning("No campaigns found matching filters")
                return

            print_info(f"Analysing search terms from {len(target_campaigns)} campaign(s) ({start_date} to {end_date})")

            all_terms: list[SearchTermAnalysis] = []

            with spinner("Pulling search term reports..."):
                for camp in target_campaigns:
                    if camp.id is None:
                        continue
                    camp_name = camp.name or str(camp.id)
                    try:
                        req = AppsReportingRequest(
                            filters=[
                                ReportFilter(
                                    field="campaignId",
                                    operator=ReportFilterOperator.EQUALS,
                                    value=str(camp.id),
                                )
                            ],
                            time_range=ReportTimeRange(
                                start=start_date,
                                end=end_date,
                                time_zone=ReportTimeZone.ORTZ,
                                granularity=ReportGranularity.DAILY,
                            ),
                        )

                        agg: dict[tuple[str, int], SearchTermAnalysis] = {}
                        for row in client.reports.iter_search_terms(req):
                            meta = row.metadata
                            tm = row.total_metrics
                            if not tm or not meta or not meta.search_term_text:
                                continue

                            key = (meta.search_term_text, meta.ad_group_id or 0)
                            if key not in agg:
                                spend_amt = (
                                    Decimal(str(tm.local_spend.amount))
                                    if tm.local_spend and tm.local_spend.amount is not None
                                    else Decimal("0")
                                )
                                currency = (
                                    tm.local_spend.currency
                                    if tm.local_spend and tm.local_spend.currency
                                    else campaign_currency(camp)
                                )
                                agg[key] = SearchTermAnalysis(
                                    search_term=meta.search_term_text,
                                    campaign_id=camp.id,
                                    campaign_name=camp_name,
                                    ad_group_id=meta.ad_group_id or 0,
                                    ad_group_name=meta.ad_group.name if meta.ad_group and meta.ad_group.name else "",
                                    impressions=tm.impressions or 0,
                                    taps=tm.taps or 0,
                                    installs=tm.tap_installs or 0,
                                    spend=spend_amt,
                                    currency=currency,
                                    matched_keyword=meta.keyword.text if meta.keyword else None,
                                )
                            else:
                                existing = agg[key]
                                existing.impressions += tm.impressions or 0
                                existing.taps += tm.taps or 0
                                existing.installs += tm.tap_installs or 0
                                if tm.local_spend and tm.local_spend.amount is not None:
                                    existing.spend += Decimal(str(tm.local_spend.amount))

                        all_terms.extend(agg.values())
                    except AppleSearchAdsError:
                        continue

            if not all_terms:
                print_warning("No search term data found for the specified period")
                return

            if min_spend > 0:
                all_terms = [t for t in all_terms if t.spend >= Decimal(str(min_spend))]
            if min_impressions > 0:
                all_terms = [t for t in all_terms if t.impressions >= min_impressions]

            if not all_terms:
                print_warning("No search terms match the specified thresholds")
                return

            all_terms.sort(key=lambda t: t.spend, reverse=True)
            print_info(f"Found {len(all_terms)} search terms to analyse")

            app_names = {(c.name or "").split(" - ")[0].strip() for c in target_campaigns if c.name}
            app_description = f"App: {', '.join(sorted(app_names))}" if app_names else "Unknown app"

            batch_size = 100
            all_suggestions: list[tuple[SearchTermAnalysis, NegativeSuggestion]] = []

            for i in range(0, len(all_terms), batch_size):
                batch = all_terms[i : i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (len(all_terms) + batch_size - 1) // batch_size

                with spinner(f"AI analysis batch {batch_num}/{total_batches}..."):
                    try:
                        result = asyncio.run(_analyse_search_terms(batch, app_description, settings, provider))
                    except RuntimeError as e:
                        print_error("AI Error", str(e))
                        raise typer.Exit(EXIT_ERROR) from None

                suggestion_map = {s.search_term: s for s in result.suggestions}
                for term in batch:
                    suggestion = suggestion_map.get(term.search_term)
                    if suggestion:
                        all_suggestions.append((term, suggestion))

            if not all_suggestions:
                print_warning("AI returned no suggestions")
                return

            negatives_list = [(t, s) for t, s in all_suggestions if s.action == "negative"]
            review_list = [(t, s) for t, s in all_suggestions if s.action == "review"]
            keep_list = [(t, s) for t, s in all_suggestions if s.action == "keep"]

            console.print()

            if negatives_list:
                table = Table(title=f"Suggested Negatives ({len(negatives_list)})")
                table.add_column("Search Term", style="red", max_width=30)
                table.add_column("Campaign", style="cyan", max_width=20)
                table.add_column("Impr", justify="right")
                table.add_column("Taps", justify="right")
                table.add_column("Installs", justify="right")
                table.add_column("Spend", justify="right")
                table.add_column("Reason", max_width=35)

                for term, suggestion in negatives_list:
                    table.add_row(
                        term.search_term,
                        term.campaign_name[:20],
                        f"{term.impressions:,}",
                        f"{term.taps:,}",
                        f"{term.installs:,}",
                        f"{term.spend:.2f} {term.currency}",
                        suggestion.reason,
                    )

                console.print(table)
                console.print()

            if review_list:
                table = Table(title=f"Needs Review ({len(review_list)})")
                table.add_column("Search Term", style="yellow", max_width=30)
                table.add_column("Campaign", style="cyan", max_width=20)
                table.add_column("Impr", justify="right")
                table.add_column("Taps", justify="right")
                table.add_column("Installs", justify="right")
                table.add_column("Spend", justify="right")
                table.add_column("Reason", max_width=35)

                for term, suggestion in review_list:
                    table.add_row(
                        term.search_term,
                        term.campaign_name[:20],
                        f"{term.impressions:,}",
                        f"{term.taps:,}",
                        f"{term.installs:,}",
                        f"{term.spend:.2f} {term.currency}",
                        suggestion.reason,
                    )

                console.print(table)
                console.print()

            total_negative_spend = sum(t.spend for t, _ in negatives_list)
            print_result_panel(
                "Analysis Summary",
                {
                    "Search terms analysed": str(len(all_suggestions)),
                    "Suggested negatives": str(len(negatives_list)),
                    "Needs review": str(len(review_list)),
                    "Keep": str(len(keep_list)),
                    "Wasted spend (negatives)": f"{total_negative_spend:.2f}",
                },
            )

            if output:
                try:
                    with open(output, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(
                            [
                                "search_term",
                                "action",
                                "reason",
                                "campaign_name",
                                "ad_group_name",
                                "matched_keyword",
                                "impressions",
                                "taps",
                                "installs",
                                "spend",
                                "currency",
                            ]
                        )
                        for term, suggestion in all_suggestions:
                            writer.writerow(
                                [
                                    term.search_term,
                                    suggestion.action,
                                    suggestion.reason,
                                    term.campaign_name,
                                    term.ad_group_name,
                                    term.matched_keyword or "",
                                    term.impressions,
                                    term.taps,
                                    term.installs,
                                    f"{term.spend:.2f}",
                                    term.currency,
                                ]
                            )
                    print_success(f"Exported {len(all_suggestions)} suggestions to {output}")
                except Exception as e:
                    print_error("Export failed", str(e))

            if apply and not dry_run and negatives_list:
                by_campaign: dict[int, list[tuple[SearchTermAnalysis, NegativeSuggestion]]] = defaultdict(list)
                for term, suggestion in negatives_list:
                    by_campaign[term.campaign_id].append((term, suggestion))

                total_added = 0
                for camp_id, items in by_campaign.items():
                    camp_name = items[0][0].campaign_name
                    neg_creates = [
                        BulkNegativeKeywordCreate(
                            campaign_id=camp_id,
                            text=term.search_term,
                            match_type=KeywordMatchType.EXACT,
                            status=NegativeKeywordStatus.ENABLED,
                        )
                        for term, _ in items
                    ]

                    try:
                        with spinner(f"Adding {len(neg_creates)} negatives to '{camp_name}'..."):
                            results = client.bulk.create_negative_keywords(neg_creates, allow_partial_success=True)
                        total_added += sum(1 for r in results if r.success)
                    except AppleSearchAdsError as e:
                        print_error(f"Failed to add negatives to '{camp_name}'", e.message)

                if total_added > 0:
                    print_success(f"Added {total_added} negative keywords across {len(by_campaign)} campaign(s)")

            elif apply and dry_run:
                print_info("Dry run — no changes applied. Remove --dry-run to apply negatives.")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
