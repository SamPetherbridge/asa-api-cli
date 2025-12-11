"""Impression Share CLI commands for bid optimization."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

import typer
from asa_api_client.models.reports import (
    GranularityType,
    ImpressionShareReport,
)
from rich.prompt import Prompt
from rich.table import Table

from asa_api_cli.utils import (
    console,
    get_client,
    handle_api_error,
    print_error,
    print_info,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="Impression share analysis and bid optimization")


@dataclass
class KeywordShareData:
    """Impression share data for a keyword with bid info."""

    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    keyword_id: int
    keyword: str
    country: str
    current_bid: Decimal
    currency: str
    low_share: float | None
    high_share: float | None
    rank: int | None
    search_popularity: int | None

    @property
    def share_range(self) -> str:
        """Format impression share as range string."""
        if self.low_share is None and self.high_share is None:
            return "N/A"
        low = f"{int(self.low_share * 100)}" if self.low_share else "0"
        high = f"{int(self.high_share * 100)}" if self.high_share else "?"
        return f"{low}-{high}%"

    @property
    def rank_str(self) -> str:
        """Format rank for display."""
        return str(self.rank) if self.rank else "N/A"

    @property
    def bid_str(self) -> str:
        """Format bid for display."""
        return f"{self.current_bid:.2f} {self.currency}"

    @property
    def suggested_bid(self) -> Decimal:
        """Suggest a bid based on impression share.

        Logic:
        - 0-10% share: Suggest 50% increase
        - 10-30% share: Suggest 25% increase
        - 30-50% share: Suggest 10% increase
        - 50%+ share: Keep current bid
        """
        if self.high_share is None or self.high_share >= 0.5:
            return self.current_bid

        high_pct = self.high_share * 100
        if high_pct <= 10:
            return self.current_bid * Decimal("1.50")
        elif high_pct <= 30:
            return self.current_bid * Decimal("1.25")
        elif high_pct <= 50:
            return self.current_bid * Decimal("1.10")
        return self.current_bid

    @property
    def suggested_bid_str(self) -> str:
        """Format suggested bid for display."""
        suggested = self.suggested_bid
        if suggested == self.current_bid:
            return "-"
        return f"{suggested:.2f} {self.currency}"


def _parse_report_data(
    report: ImpressionShareReport,
    keywords_by_id: dict[int, dict[str, Any]],
) -> list[KeywordShareData]:
    """Parse impression share report into structured data."""
    results: list[KeywordShareData] = []

    for row in report.row:
        meta = row.metadata
        if not meta.keyword_id or not meta.keyword:
            continue

        # Get keyword bid info
        kw_info = keywords_by_id.get(meta.keyword_id, {})
        bid_amount = kw_info.get("bid", Decimal("0"))
        currency = kw_info.get("currency", "USD")

        results.append(
            KeywordShareData(
                campaign_id=meta.campaign_id or 0,
                campaign_name=meta.campaign_name or "",
                ad_group_id=meta.ad_group_id or 0,
                ad_group_name=meta.ad_group_name or "",
                keyword_id=meta.keyword_id,
                keyword=meta.keyword,
                country=meta.country_or_region or "",
                current_bid=bid_amount,
                currency=currency,
                low_share=row.low_impression_share,
                high_share=row.high_impression_share,
                rank=row.rank,
                search_popularity=row.search_popularity,
            )
        )

    return results


def _display_share_table(data: list[KeywordShareData], show_suggestions: bool = True) -> None:
    """Display impression share data in a rich table."""
    table = Table(title="Impression Share Analysis", show_lines=True)

    table.add_column("Campaign", style="cyan", no_wrap=True)
    table.add_column("Keyword", style="white")
    table.add_column("Country", style="dim")
    table.add_column("Share", justify="right", style="green")
    table.add_column("Rank", justify="center")
    table.add_column("Current Bid", justify="right")
    if show_suggestions:
        table.add_column("Suggested", justify="right", style="yellow")

    for row in data:
        row_values = [
            row.campaign_name[:30],
            row.keyword,
            row.country,
            row.share_range,
            row.rank_str,
            row.bid_str,
        ]
        if show_suggestions:
            row_values.append(row.suggested_bid_str)
        table.add_row(*row_values)

    console.print(table)


@app.command("analyze")
def analyze_impression_share(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code (e.g., US, GB)"),
    ] = None,
    min_share: Annotated[
        float | None,
        typer.Option("--min-share", help="Only show keywords with share below this %"),
    ] = None,
    campaign: Annotated[
        str | None,
        typer.Option("--campaign", help="Filter by campaign name pattern"),
    ] = None,
    suggest: Annotated[
        bool,
        typer.Option("--suggest/--no-suggest", help="Show bid suggestions"),
    ] = True,
) -> None:
    """Analyze impression share for all keywords.

    Shows your impression share (percentage of available impressions
    you're winning) for each keyword, along with current bids and
    suggested bid increases for better exposure.

    Examples:
        asa impression-share analyze --days 14
        asa impression-share analyze --country US --min-share 30
        asa impression-share analyze --campaign "Brand*"
    """
    client = get_client()

    # Validate days - API limits to 30 days max
    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)  # -1 because range is inclusive

    country_codes = [country.upper()] if country else None

    # Fetch impression share report
    with spinner("Creating impression share report..."):
        try:
            report = client.custom_reports.get_impression_share(
                start_date=start_date,
                end_date=end_date,
                granularity=GranularityType.DAILY,
                country_codes=country_codes,
                poll_interval=3.0,
                timeout=120.0,
            )
        except Exception as e:
            handle_api_error(e)
            return

    if not report.row:
        print_warning("No impression share data available for the selected period")
        return

    print_success(f"Retrieved {len(report.row)} keyword records")

    # Get keyword bid information
    keywords_by_id: dict[int, dict[str, Any]] = {}

    with spinner("Fetching keyword bid data..."):
        try:
            # Get all campaigns
            campaigns_resp = client.campaigns.list(limit=1000)
            for camp in campaigns_resp.data:
                if campaign and campaign.replace("*", "") not in camp.name:
                    continue

                # Get ad groups for each campaign
                ad_groups_resp = client.campaigns(camp.id).ad_groups.list(limit=1000)
                for ag in ad_groups_resp.data:
                    # Get keywords for each ad group
                    kws_resp = client.campaigns(camp.id).ad_groups(ag.id).keywords.list(
                        limit=1000
                    )
                    for kw in kws_resp.data:
                        if kw.bid_amount:
                            keywords_by_id[kw.id] = {
                                "bid": Decimal(kw.bid_amount.amount),
                                "currency": kw.bid_amount.currency,
                            }
        except Exception as e:
            handle_api_error(e)
            return

    # Parse and filter data
    data = _parse_report_data(report, keywords_by_id)

    # Apply campaign filter
    if campaign:
        pattern = campaign.replace("*", "").lower()
        data = [d for d in data if pattern in d.campaign_name.lower()]

    # Apply min share filter
    if min_share is not None:
        threshold = min_share / 100.0
        data = [d for d in data if d.high_share is not None and d.high_share < threshold]

    if not data:
        print_warning("No keywords match the specified filters")
        return

    # Sort by impression share (lowest first - most opportunity)
    data.sort(key=lambda x: x.high_share or 0)

    _display_share_table(data, show_suggestions=suggest)

    # Summary
    low_share_count = sum(1 for d in data if d.high_share and d.high_share < 0.3)
    print_info(f"\nTotal keywords: {len(data)}")
    print_info(f"Keywords with <30% share: {low_share_count}")


@app.command("optimize")
def interactive_bid_optimizer(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code"),
    ] = None,
    max_share: Annotated[
        float,
        typer.Option("--max-share", help="Only optimize keywords below this share %"),
    ] = 50.0,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show changes without applying them"),
    ] = False,
) -> None:
    """Interactive bid optimizer based on impression share.

    Walks through keywords with low impression share and offers
    to increase bids to improve exposure. Perfect for SKAG
    campaigns where each keyword-campaign pair is optimized.

    The optimizer suggests bid increases based on current share:
    - 0-10% share: 50% bid increase
    - 10-30% share: 25% bid increase
    - 30-50% share: 10% bid increase

    Examples:
        asa impression-share optimize --country US
        asa impression-share optimize --max-share 30 --dry-run
    """
    client = get_client()

    # Validate days - API limits to 30 days max
    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)  # -1 because range is inclusive

    country_codes = [country.upper()] if country else None

    # Fetch data
    with spinner("Creating impression share report..."):
        try:
            report = client.custom_reports.get_impression_share(
                start_date=start_date,
                end_date=end_date,
                granularity=GranularityType.DAILY,
                country_codes=country_codes,
                poll_interval=3.0,
                timeout=120.0,
            )
        except Exception as e:
            handle_api_error(e)
            return

    if not report.row:
        print_warning("No impression share data available")
        return

    # Get keyword bid information
    keywords_by_id: dict[int, dict[str, Any]] = {}
    keyword_paths: dict[int, tuple[int, int]] = {}  # keyword_id -> (campaign_id, ad_group_id)

    with spinner("Fetching keyword bid data..."):
        try:
            campaigns_resp = client.campaigns.list(limit=1000)
            for camp in campaigns_resp.data:
                ad_groups_resp = client.campaigns(camp.id).ad_groups.list(limit=1000)
                for ag in ad_groups_resp.data:
                    kws_resp = client.campaigns(camp.id).ad_groups(ag.id).keywords.list(
                        limit=1000
                    )
                    for kw in kws_resp.data:
                        if kw.bid_amount:
                            keywords_by_id[kw.id] = {
                                "bid": Decimal(kw.bid_amount.amount),
                                "currency": kw.bid_amount.currency,
                            }
                            keyword_paths[kw.id] = (camp.id, ag.id)
        except Exception as e:
            handle_api_error(e)
            return

    # Parse and filter
    data = _parse_report_data(report, keywords_by_id)
    threshold = max_share / 100.0
    data = [d for d in data if d.high_share is not None and d.high_share < threshold]
    data = [d for d in data if d.suggested_bid > d.current_bid]

    if not data:
        print_success("All keywords have good impression share - no optimization needed!")
        return

    # Sort by share (lowest first)
    data.sort(key=lambda x: x.high_share or 0)

    print_info(f"\nFound {len(data)} keywords that could benefit from bid increases")
    console.print()

    updates_made = 0
    updates_skipped = 0

    for i, kw in enumerate(data, 1):
        console.print(f"\n[bold cyan]Keyword {i}/{len(data)}[/bold cyan]")
        console.print(f"  Campaign:  {kw.campaign_name}")
        console.print(f"  Keyword:   [white]{kw.keyword}[/white]")
        console.print(f"  Country:   {kw.country}")
        console.print(f"  Share:     [green]{kw.share_range}[/green]")
        console.print(f"  Rank:      {kw.rank_str}")
        console.print(f"  Current:   {kw.bid_str}")
        console.print(f"  Suggested: [yellow]{kw.suggested_bid_str}[/yellow]")

        if dry_run:
            console.print("  [dim](dry run - no changes made)[/dim]")
            continue

        # Interactive prompt
        action = Prompt.ask(
            "  Action",
            choices=["y", "n", "c", "q"],
            default="n",
        )

        if action == "q":
            print_info("Stopping optimization")
            break
        elif action == "c":
            # Custom bid
            custom = Prompt.ask("  Enter custom bid")
            try:
                custom_bid = Decimal(custom)
                # Apply custom bid
                if kw.keyword_id in keyword_paths:
                    camp_id, ag_id = keyword_paths[kw.keyword_id]
                    from asa_api_client.models import KeywordUpdate, Money

                    client.campaigns(camp_id).ad_groups(ag_id).keywords.update(
                        kw.keyword_id,
                        KeywordUpdate(
                            bid_amount=Money(amount=str(custom_bid), currency=kw.currency)
                        ),
                    )
                    print_success(f"  Updated bid to {custom_bid:.2f} {kw.currency}")
                    updates_made += 1
            except Exception as e:
                print_error("Update failed", str(e))
        elif action == "y":
            # Apply suggested bid
            if kw.keyword_id in keyword_paths:
                try:
                    camp_id, ag_id = keyword_paths[kw.keyword_id]
                    from asa_api_client.models import KeywordUpdate, Money

                    client.campaigns(camp_id).ad_groups(ag_id).keywords.update(
                        kw.keyword_id,
                        KeywordUpdate(
                            bid_amount=Money(
                                amount=str(kw.suggested_bid), currency=kw.currency
                            )
                        ),
                    )
                    print_success(f"  Updated bid to {kw.suggested_bid:.2f} {kw.currency}")
                    updates_made += 1
                except Exception as e:
                    print_error("Update failed", str(e))
        else:
            updates_skipped += 1

    console.print()
    print_info(f"Summary: {updates_made} bids updated, {updates_skipped} skipped")


@app.command("report")
def generate_share_report(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Save to CSV file"),
    ] = None,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code"),
    ] = None,
) -> None:
    """Generate impression share report.

    Creates a detailed report of impression share across all keywords
    with optional CSV export.

    Examples:
        asa impression-share report --days 14 --output share_report.csv
    """
    client = get_client()

    # API limits to 30 days max
    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)  # -1 because range is inclusive

    country_codes = [country.upper()] if country else None

    with spinner("Generating impression share report..."):
        try:
            report = client.custom_reports.get_impression_share(
                start_date=start_date,
                end_date=end_date,
                granularity=GranularityType.DAILY,
                country_codes=country_codes,
                poll_interval=3.0,
                timeout=120.0,
            )
        except Exception as e:
            handle_api_error(e)
            return

    if not report.row:
        print_warning("No data available for the selected period")
        return

    print_success(f"Retrieved {len(report.row)} records")

    if output:
        # Export to CSV
        try:
            import csv

            with open(output, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "campaign_id",
                    "campaign_name",
                    "ad_group_id",
                    "ad_group_name",
                    "keyword_id",
                    "keyword",
                    "country",
                    "low_impression_share",
                    "high_impression_share",
                    "rank",
                    "search_popularity",
                ])

                for row in report.row:
                    meta = row.metadata
                    writer.writerow([
                        meta.campaign_id,
                        meta.campaign_name,
                        meta.ad_group_id,
                        meta.ad_group_name,
                        meta.keyword_id,
                        meta.keyword,
                        meta.country_or_region,
                        row.low_impression_share,
                        row.high_impression_share,
                        row.rank,
                        row.search_popularity,
                    ])

            print_success(f"Report saved to {output}")
        except Exception as e:
            print_error("Export failed", str(e))
    else:
        # Display summary table
        table = Table(title="Impression Share Summary")
        table.add_column("Campaign", style="cyan")
        table.add_column("Keyword")
        table.add_column("Country", style="dim")
        table.add_column("Share", justify="right", style="green")
        table.add_column("Rank", justify="center")

        for row in report.row[:50]:  # Limit display
            meta = row.metadata
            low = f"{int(row.low_impression_share * 100)}" if row.low_impression_share else "0"
            high = f"{int(row.high_impression_share * 100)}" if row.high_impression_share else "?"
            share_range = f"{low}-{high}%"

            table.add_row(
                (meta.campaign_name or "")[:30],
                meta.keyword or "",
                meta.country_or_region or "",
                share_range,
                str(row.rank) if row.rank else "-",
            )

        console.print(table)

        if len(report.row) > 50:
            print_info(f"\nShowing first 50 of {len(report.row)} records. Use --output to export all.")
