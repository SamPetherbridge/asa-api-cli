"""Impression Share and Search Term Popularity CLI commands (Platform API v1)."""

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from asa_api_client.v1.client import AppleAdsClient

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.campaigns import PromotedObjectType
from asa_api_client.v1.models.insights import (
    ImpressionShareGranularity,
    ImpressionShareOptions,
    ImpressionShareQueryRequest,
    ImpressionShareReportType,
    ImpressionShareRow,
    ImpressionShareTimeRange,
    InsightsFilter,
    SearchTermPopularityGranularity,
    SearchTermPopularityQueryRequest,
    SearchTermPopularityRow,
    SearchTermPopularityTimeRange,
)
from asa_api_client.v1.models.keywords import Keyword, KeywordUpdate
from asa_api_client.v1.models.reports import (
    AppsReportingRequest,
    ReportFilter,
    ReportFilterOperator,
    ReportGranularity,
    ReportTimeRange,
    ReportTimeZone,
)
from asa_api_client.v1.query import Query
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    campaign_countries,
    cli_state,
    console,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    parse_date,
    print_error,
    print_info,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="Impression share and search term popularity insights")


@dataclass
class SearchTermShareData:
    """Impression share data for a search term."""

    date: str
    app_name: str
    adam_id: str
    country: str
    search_term: str
    low_share: float | None
    high_share: float | None
    rank: int | str | None
    search_popularity: int | None

    @property
    def share_range(self) -> str:
        """Format impression share as range string."""
        if self.low_share is None and self.high_share is None:
            return "N/A"
        low = int(round((self.low_share or 0.0) * 100))
        high = int(round((self.high_share if self.high_share is not None else (self.low_share or 0.0)) * 100))
        if low == high:
            return f"{high}%"
        return f"{low}-{high}%"

    @property
    def rank_display(self) -> str:
        """Format rank for display."""
        if self.rank is None or self.rank == "":
            return "N/A"
        if isinstance(self.rank, int):
            return ">4" if self.rank > 4 else str(self.rank)
        rank_map = {
            "ONE": "1",
            "TWO": "2",
            "THREE": "3",
            "FOUR": "4",
            "GREATER_THAN_FOUR": ">4",
        }
        return rank_map.get(self.rank, self.rank)

    @property
    def popularity_display(self) -> str:
        """Format popularity for display."""
        if self.search_popularity is None:
            return "N/A"
        return str(self.search_popularity)

    @property
    def avg_share(self) -> float:
        """Average of low and high share for sorting."""
        if self.low_share is None and self.high_share is None:
            return 0.0
        low = self.low_share or 0.0
        high = self.high_share if self.high_share is not None else low
        return (low + high) / 2


def _discover_promoted_app_ids(client: "AppleAdsClient", explicit_ids: list[str] | None = None) -> list[str]:
    """Return distinct App Store ``promotedObjectId`` strings to query insights for."""
    if explicit_ids:
        seen: list[str] = []
        for raw in explicit_ids:
            val = str(raw).strip()
            if val and val not in seen:
                seen.append(val)
        return seen

    discovered: list[str] = []
    for camp in client.campaigns.iter_all():
        if (
            enum_value(camp.promoted_object_type) == PromotedObjectType.APPSTORE_APP.value
            and camp.promoted_object_id
            and camp.promoted_object_id not in discovered
        ):
            discovered.append(str(camp.promoted_object_id))

    if not discovered:
        try:
            for app_info in client.apps.search("", return_owned_apps=True, limit=50).result:
                if app_info.adam_id is not None:
                    aid_str = str(app_info.adam_id)
                    if aid_str not in discovered:
                        discovered.append(aid_str)
        except AppleSearchAdsError:
            pass

    return discovered


def _fetch_impression_share_rows(
    client: "AppleAdsClient",
    *,
    start_date: date,
    end_date: date,
    adam_ids: list[str] | None = None,
    country: str | None = None,
    granularity: ImpressionShareGranularity = ImpressionShareGranularity.DAILY,
    report_type: ImpressionShareReportType = ImpressionShareReportType.FIRST_SLOT,
) -> list[ImpressionShareRow]:
    """Fetch impression share rows across all promoted apps via ``client.insights``."""
    promoted_ids = _discover_promoted_app_ids(client, adam_ids)
    if not promoted_ids:
        return []

    rows: list[ImpressionShareRow] = []
    for pid in promoted_ids:
        filters = [InsightsFilter(field="promotedObjectId", operator="EQUALS", value=pid)]
        if country:
            filters.append(InsightsFilter(field="countryOrRegion", operator="EQUALS", value=country.upper()))

        req = ImpressionShareQueryRequest(
            fields=[],
            filters=filters,
            time_range=ImpressionShareTimeRange(
                start=start_date,
                end=end_date,
                granularity=granularity,
            ),
            options=ImpressionShareOptions(impression_share_report_type=report_type),
        )
        rows.extend(client.insights.iter_impression_share(req))

    return rows


def _parse_rows(rows: list[ImpressionShareRow], *, skip_suppressed: bool = True) -> list[SearchTermShareData]:
    """Convert v1 ``ImpressionShareRow`` records into ``SearchTermShareData``."""
    results: list[SearchTermShareData] = []
    for row in rows:
        if skip_suppressed and not row.search_term:
            continue
        date_val = row.day.isoformat() if row.day else (row.week.isoformat() if row.week else "")
        results.append(
            SearchTermShareData(
                date=date_val,
                app_name=row.app_name or "",
                adam_id=row.promoted_object_id or "",
                country=row.country_or_region or "",
                search_term=row.search_term or "",
                low_share=row.low_impression_share,
                high_share=row.high_impression_share,
                rank=row.rank,
                search_popularity=row.search_popularity_1_to_5,
            )
        )
    return results


def _aggregate_by_search_term(
    data: list[SearchTermShareData],
) -> dict[str, SearchTermShareData]:
    """Aggregate data by search term + country + app, keeping latest entry."""
    aggregated: dict[str, SearchTermShareData] = {}

    for item in data:
        key = f"{item.search_term}|{item.country}|{item.adam_id}"
        if key not in aggregated or item.date > aggregated[key].date:
            aggregated[key] = item

    return aggregated


def _display_share_table(data: list[SearchTermShareData], limit: int = 100) -> None:
    """Display impression share data in a rich table."""
    table = Table(title="Impression Share Analysis", show_lines=False)

    table.add_column("App", style="magenta", no_wrap=True, max_width=25)
    table.add_column("Search Term", style="cyan", no_wrap=False, max_width=35)
    table.add_column("Country", style="dim", width=4)
    table.add_column("Share", justify="right", style="green", width=10)
    table.add_column("Rank", justify="center", width=4)
    table.add_column("Pop", justify="center", width=3)
    table.add_column("Date", style="dim", width=10)

    for row in data[:limit]:
        share_style = "green"
        if row.high_share is not None:
            if row.high_share < 0.3:
                share_style = "red"
            elif row.high_share < 0.5:
                share_style = "yellow"

        table.add_row(
            row.app_name[:25] if row.app_name else row.adam_id,
            row.search_term[:35],
            row.country,
            f"[{share_style}]{row.share_range}[/{share_style}]",
            row.rank_display,
            row.popularity_display,
            row.date,
        )

    console.print(table)

    if len(data) > limit:
        print_info(f"Showing {limit} of {len(data)} results. Use --limit or --output to see all.")


@app.command("analyze")
def analyze_impression_share(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    adam_ids: Annotated[
        list[str] | None,
        typer.Option("--adam-id", help="Filter by App Store adamId(s) (auto-discovered if omitted)"),
    ] = None,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code (e.g., US, AU)"),
    ] = None,
    min_share: Annotated[
        float | None,
        typer.Option(
            "--min-share",
            help="Only show search terms with share below this % (e.g., 30)",
        ),
    ] = None,
    search: Annotated[
        str | None,
        typer.Option("--search", "-s", help="Filter by search term (partial match)"),
    ] = None,
    app_filter: Annotated[
        str | None,
        typer.Option("--app", "-a", help="Filter by app name (partial match)"),
    ] = None,
    report_type: Annotated[
        ImpressionShareReportType,
        typer.Option("--report-type", help="Ad slot scope (FIRST_SLOT or ALL_SLOTS)"),
    ] = ImpressionShareReportType.FIRST_SLOT,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Max rows to display (0 for all)"),
    ] = 100,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Export to CSV file"),
    ] = None,
) -> None:
    """Analyze impression share for your search terms via Platform API v1 Insights.

    Examples:
        asa impression-share analyze --days 14
        asa impression-share analyze --country US --min-share 30
        asa impression-share analyze --adam-id 123456789 --search "calculator"
    """
    client = get_client()

    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30
    days = max(days, 1)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    try:
        with client:
            with spinner("Fetching impression share insights..."):
                rows = _fetch_impression_share_rows(
                    client,
                    start_date=start_date,
                    end_date=end_date,
                    adam_ids=adam_ids,
                    country=country,
                    report_type=report_type,
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None

    if not rows:
        print_warning("No impression share data available for the selected period")
        return

    print_success(f"Retrieved {len(rows)} records")

    data = _parse_rows(rows)
    aggregated = _aggregate_by_search_term(data)
    data = list(aggregated.values())

    if app_filter:
        app_lower = app_filter.lower()
        data = [d for d in data if d.app_name and app_lower in d.app_name.lower()]

    if search:
        search_lower = search.lower()
        data = [d for d in data if search_lower in d.search_term.lower()]

    if min_share is not None:
        threshold = min_share / 100.0
        data = [d for d in data if d.high_share is not None and d.high_share < threshold]

    if not data:
        print_warning("No search terms match the specified filters")
        return

    data.sort(key=lambda x: x.avg_share)

    if output:
        try:
            with open(output, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "search_term",
                        "country",
                        "low_share",
                        "high_share",
                        "rank",
                        "popularity",
                        "date",
                        "app_name",
                        "adam_id",
                    ]
                )
                for row in data:
                    writer.writerow(
                        [
                            row.search_term,
                            row.country,
                            row.low_share,
                            row.high_share,
                            row.rank,
                            row.search_popularity,
                            row.date,
                            row.app_name,
                            row.adam_id,
                        ]
                    )
            print_success(f"Exported {len(data)} rows to {output}")
        except Exception as e:
            print_error("Export failed", str(e))

    display_limit = len(data) if limit == 0 else limit
    _display_share_table(data, limit=display_limit)

    low_share_count = sum(1 for d in data if d.high_share is not None and d.high_share < 0.3)
    mid_share_count = sum(1 for d in data if d.high_share is not None and 0.3 <= d.high_share < 0.5)
    high_share_count = sum(1 for d in data if d.high_share is not None and d.high_share >= 0.5)

    console.print(f"\n[dim]Total unique search terms:[/dim] {len(data)}")
    console.print(f"  [dim]Low share (<30%):[/dim] {low_share_count} - [red]bid increase suggested[/red]")
    console.print(f"  [dim]Medium share (30-50%):[/dim] {mid_share_count} - [yellow]consider increase[/yellow]")
    console.print(f"  [dim]High share (50%+):[/dim] {high_share_count} - [green]performing well[/green]")


@app.command("report")
def generate_share_report(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    adam_ids: Annotated[
        list[str] | None,
        typer.Option("--adam-id", help="Filter by App Store adamId(s)"),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Save to CSV file"),
    ] = None,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code"),
    ] = None,
) -> None:
    """Generate a detailed daily impression share report."""
    client = get_client()

    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30
    days = max(days, 1)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    try:
        with client:
            with spinner("Generating impression share report..."):
                rows = _fetch_impression_share_rows(
                    client,
                    start_date=start_date,
                    end_date=end_date,
                    adam_ids=adam_ids,
                    country=country,
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None

    if not rows:
        print_warning("No data available")
        return

    print_success(f"Report generated with {len(rows)} records")
    data = _parse_rows(rows)

    if output:
        try:
            with open(output, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "date",
                        "search_term",
                        "country",
                        "low_share",
                        "high_share",
                        "rank",
                        "popularity",
                        "app_name",
                        "adam_id",
                    ]
                )
                for row in data:
                    writer.writerow(
                        [
                            row.date,
                            row.search_term,
                            row.country,
                            row.low_share,
                            row.high_share,
                            row.rank,
                            row.search_popularity,
                            row.app_name,
                            row.adam_id,
                        ]
                    )
            print_success(f"Exported {len(data)} rows to {output}")
        except Exception as e:
            print_error("Export failed", str(e))
    else:
        _display_share_table(data)


@app.command("summary")
def share_summary(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    adam_ids: Annotated[
        list[str] | None,
        typer.Option("--adam-id", help="Filter by App Store adamId(s)"),
    ] = None,
) -> None:
    """Show impression share summary by app and country."""
    client = get_client()

    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30
    days = max(days, 1)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    try:
        with client:
            with spinner("Generating summary..."):
                rows = _fetch_impression_share_rows(
                    client,
                    start_date=start_date,
                    end_date=end_date,
                    adam_ids=adam_ids,
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None

    if not rows:
        print_warning("No data available")
        return

    data = _parse_rows(rows)
    if not data:
        print_warning("No search term impression share data available")
        return

    by_app: dict[str, dict[str, list[SearchTermShareData]]] = {}
    for item in data:
        app_name = item.app_name or item.adam_id or "Unknown"
        if app_name not in by_app:
            by_app[app_name] = {}
        if item.country not in by_app[app_name]:
            by_app[app_name][item.country] = []
        by_app[app_name][item.country].append(item)

    table = Table(title=f"Impression Share Summary ({days} days)")
    table.add_column("App", style="magenta", no_wrap=True)
    table.add_column("Country", style="cyan")
    table.add_column("Search Terms", justify="right")
    table.add_column("Avg Share", justify="right")
    table.add_column("<30%", justify="right", style="red")
    table.add_column("30-50%", justify="right", style="yellow")
    table.add_column(">50%", justify="right", style="green")

    total_terms = 0
    total_countries: set[str] = set()
    sorted_apps = sorted(by_app.keys())

    for app_name in sorted_apps:
        countries_data = by_app[app_name]
        country_rows = []
        for country_code, items in countries_data.items():
            unique_terms = len({i.search_term for i in items})
            avg_shares = [i.avg_share for i in items if i.avg_share > 0]
            avg_share = sum(avg_shares) / len(avg_shares) if avg_shares else 0

            low_count = sum(1 for i in items if i.high_share is not None and i.high_share < 0.3)
            mid_count = sum(1 for i in items if i.high_share is not None and 0.3 <= i.high_share < 0.5)
            high_count = sum(1 for i in items if i.high_share is not None and i.high_share >= 0.5)

            country_rows.append((country_code, unique_terms, avg_share, low_count, mid_count, high_count))
            total_terms += unique_terms
            total_countries.add(country_code)

        country_rows.sort(key=lambda x: x[1], reverse=True)

        for i, (country_code, terms, avg, low, mid, high) in enumerate(country_rows):
            table.add_row(
                app_name[:25] if i == 0 else "",
                country_code,
                str(terms),
                f"{avg * 100:.0f}%",
                str(low),
                str(mid),
                str(high),
            )

        if app_name != sorted_apps[-1]:
            table.add_row("", "", "", "", "", "", "", end_section=True)

    console.print(table)
    print_info(f"\nTotal: {total_terms} search terms across {len(by_app)} apps and {len(total_countries)} countries")


@dataclass
class CorrelatedSearchTerm:
    """Search term with matched campaign/keyword data."""

    search_term: str
    country: str
    app_name: str
    low_share: float | None
    high_share: float | None
    rank: int | str | None
    search_popularity: int | None
    campaign_id: int | None = None
    campaign_name: str | None = None
    ad_group_id: int | None = None
    ad_group_name: str | None = None
    keyword_id: int | None = None
    keyword_text: str | None = None
    current_bid: Decimal | None = None
    currency: str | None = None
    impressions: int = 0
    taps: int = 0
    ttr: float | None = None

    @property
    def share_range(self) -> str:
        """Format impression share as range string."""
        if self.low_share is None and self.high_share is None:
            return "N/A"
        low = int(round((self.low_share or 0.0) * 100))
        high = int(round((self.high_share if self.high_share is not None else (self.low_share or 0.0)) * 100))
        if low == high:
            return f"{high}%"
        return f"{low}-{high}%"

    @property
    def avg_share(self) -> float:
        """Average of low and high share."""
        if self.low_share is None and self.high_share is None:
            return 0.0
        low = self.low_share or 0.0
        high = self.high_share if self.high_share is not None else low
        return (low + high) / 2

    @property
    def is_matched(self) -> bool:
        """Whether this search term was matched to a keyword."""
        return self.keyword_id is not None

    @property
    def bid_strength(self) -> str:
        """Estimate bid strength based on impressions and TTR."""
        if self.impressions == 0:
            return "UNKNOWN"
        ttr = self.ttr or 0.0
        if self.impressions >= 1000 and ttr >= 0.05:
            return "STRONG"
        if self.impressions >= 100 and ttr >= 0.02:
            return "MODERATE"
        return "WEAK"


@dataclass
class KeywordInfo:
    """Cached keyword information for matching."""

    keyword_id: int
    keyword_text: str
    campaign_id: int
    campaign_name: str
    ad_group_id: int
    ad_group_name: str
    bid_amount: Decimal
    currency: str
    impressions: int = 0
    taps: int = 0
    ttr: float | None = None


def _build_keyword_index_all_countries(
    client: "AppleAdsClient",
    countries: set[str],
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, dict[str, list[KeywordInfo]]]:
    """Build an index of keywords by text for all relevant countries."""
    keyword_indices: dict[str, dict[str, list[KeywordInfo]]] = {c.upper(): {} for c in countries}
    upper_countries = {c.upper() for c in countries}

    campaigns = list(client.campaigns.iter_all(Query().where("status", "EQUALS", "ENABLED")))

    for campaign in campaigns:
        if campaign.id is None:
            continue
        camp_countries = {c.upper() for c in campaign_countries(campaign)}
        relevant_countries = camp_countries & upper_countries
        if not relevant_countries:
            continue

        keyword_performance: dict[int, tuple[int, int, float | None]] = {}
        if start_date and end_date:
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
                    if row.metadata and row.metadata.id is not None and row.total_metrics:
                        impr = row.total_metrics.impressions or 0
                        taps = row.total_metrics.taps or 0
                        ttr = taps / impr if impr > 0 else None
                        keyword_performance[row.metadata.id] = (impr, taps, ttr)
            except AppleSearchAdsError:
                pass

        try:
            ad_groups = list(
                client.ad_groups.iter_all(
                    Query().where("campaignId", "EQUALS", campaign.id).where("status", "EQUALS", "ENABLED")
                )
            )
            all_keywords = list(client.keywords.iter_all(Query().where("campaignId", "EQUALS", campaign.id)))
        except AppleSearchAdsError:
            continue

        keywords_by_ag: dict[int, list[Keyword]] = defaultdict(list)
        for kw in all_keywords:
            if kw.ad_group_id is not None:
                keywords_by_ag[kw.ad_group_id].append(kw)

        for ad_group in ad_groups:
            if ad_group.id is None:
                continue
            for keyword in keywords_by_ag.get(ad_group.id, []):
                if keyword.id is None or not keyword.text:
                    continue
                kw_text = keyword.text.lower()
                perf = keyword_performance.get(keyword.id, (0, 0, None))
                has_bid = keyword.bid and keyword.bid.amount is not None
                bid_amt = Decimal(keyword.bid.amount) if keyword.bid and has_bid else Decimal("0")
                currency = keyword.bid.currency if keyword.bid and keyword.bid.currency else "USD"

                info = KeywordInfo(
                    keyword_id=keyword.id,
                    keyword_text=keyword.text,
                    campaign_id=campaign.id,
                    campaign_name=campaign.name or str(campaign.id),
                    ad_group_id=ad_group.id,
                    ad_group_name=ad_group.name or str(ad_group.id),
                    bid_amount=bid_amt,
                    currency=currency,
                    impressions=perf[0],
                    taps=perf[1],
                    ttr=perf[2],
                )

                for ctry in relevant_countries:
                    if kw_text not in keyword_indices[ctry]:
                        keyword_indices[ctry][kw_text] = []
                    keyword_indices[ctry][kw_text].append(info)

    return keyword_indices


@app.command("correlate")
def correlate_impression_share(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days to analyze (max 30)"),
    ] = 7,
    adam_ids: Annotated[
        list[str] | None,
        typer.Option("--adam-id", help="Filter by App Store adamId(s)"),
    ] = None,
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country code (optional, defaults to all)"),
    ] = None,
    min_share: Annotated[
        float | None,
        typer.Option(
            "--min-share",
            help="Only show search terms with share below this % (e.g., 30)",
        ),
    ] = None,
    unmatched_only: Annotated[
        bool,
        typer.Option("--unmatched", "-u", help="Only show search terms not matched to keywords"),
    ] = False,
    matched_only: Annotated[
        bool,
        typer.Option("--matched", "-m", help="Only show search terms matched to keywords"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Max rows to display (0 for all)"),
    ] = 50,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Export to CSV file"),
    ] = None,
) -> None:
    """Correlate impression share data with your campaigns and keywords."""
    client = get_client()

    if days > 30:
        print_warning("Maximum lookback is 30 days, using 30")
        days = 30
    days = max(days, 1)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    try:
        with client:
            with spinner("Fetching impression share insights..."):
                rows = _fetch_impression_share_rows(
                    client,
                    start_date=start_date,
                    end_date=end_date,
                    adam_ids=adam_ids,
                    country=country,
                )

            if not rows:
                print_warning("No impression share data available")
                return

            print_success(f"Retrieved {len(rows)} impression share records")

            share_data = _parse_rows(rows)
            aggregated = _aggregate_by_search_term(share_data)
            if not aggregated:
                print_warning("No search term impression share data available")
                return

            countries_in_data = {item.country.upper() for item in aggregated.values() if item.country}
            print_info(f"Found data for {len(countries_in_data)} countries: {', '.join(sorted(countries_in_data))}")

            with spinner(f"Building keyword index for {len(countries_in_data)} countries (with performance data)..."):
                keyword_indices = _build_keyword_index_all_countries(client, countries_in_data, start_date, end_date)

            total_keywords = sum(len(v) for idx in keyword_indices.values() for v in idx.values())
            print_info(f"Indexed {total_keywords} keywords across {len(countries_in_data)} countries")

            correlated: list[CorrelatedSearchTerm] = []
            for item in aggregated.values():
                ctry = item.country.upper()
                keyword_index = keyword_indices.get(ctry, {})
                search_term_lower = item.search_term.lower()
                matched_keywords = keyword_index.get(search_term_lower, [])

                if matched_keywords:
                    match = matched_keywords[0]
                    correlated.append(
                        CorrelatedSearchTerm(
                            search_term=item.search_term,
                            country=item.country,
                            app_name=item.app_name,
                            low_share=item.low_share,
                            high_share=item.high_share,
                            rank=item.rank,
                            search_popularity=item.search_popularity,
                            campaign_id=match.campaign_id,
                            campaign_name=match.campaign_name,
                            ad_group_id=match.ad_group_id,
                            ad_group_name=match.ad_group_name,
                            keyword_id=match.keyword_id,
                            keyword_text=match.keyword_text,
                            current_bid=match.bid_amount,
                            currency=match.currency,
                            impressions=match.impressions,
                            taps=match.taps,
                            ttr=match.ttr,
                        )
                    )
                else:
                    correlated.append(
                        CorrelatedSearchTerm(
                            search_term=item.search_term,
                            country=item.country,
                            app_name=item.app_name,
                            low_share=item.low_share,
                            high_share=item.high_share,
                            rank=item.rank,
                            search_popularity=item.search_popularity,
                        )
                    )

            if min_share is not None:
                threshold = min_share / 100.0
                correlated = [c for c in correlated if c.high_share is not None and c.high_share < threshold]

            if unmatched_only:
                correlated = [c for c in correlated if not c.is_matched]
            elif matched_only:
                correlated = [c for c in correlated if c.is_matched]

            if not correlated:
                print_warning("No search terms match the specified filters")
                return

            correlated.sort(key=lambda x: (x.avg_share, x.country, x.search_term))

            matched_count = sum(1 for c in correlated if c.is_matched)
            unmatched_count = len(correlated) - matched_count

            if output:
                try:
                    with open(output, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(
                            [
                                "search_term",
                                "country",
                                "app_name",
                                "low_share",
                                "high_share",
                                "rank",
                                "popularity",
                                "campaign_name",
                                "ad_group_name",
                                "keyword_text",
                                "current_bid",
                                "currency",
                                "impressions",
                                "taps",
                                "ttr",
                                "bid_strength",
                            ]
                        )
                        for row in correlated:
                            writer.writerow(
                                [
                                    row.search_term,
                                    row.country,
                                    row.app_name,
                                    row.low_share,
                                    row.high_share,
                                    row.rank,
                                    row.search_popularity,
                                    row.campaign_name or "",
                                    row.ad_group_name or "",
                                    row.keyword_text or "",
                                    row.current_bid or "",
                                    row.currency or "",
                                    row.impressions if row.is_matched else "",
                                    row.taps if row.is_matched else "",
                                    f"{row.ttr:.4f}" if row.ttr else "",
                                    row.bid_strength if row.is_matched else "",
                                ]
                            )
                    print_success(f"Exported {len(correlated)} rows to {output}")
                except Exception as e:
                    print_error("Export failed", str(e))

            display_limit = len(correlated) if limit == 0 else limit

            title = "Impression Share Correlation"
            if country:
                title += f" - {country.upper()}"
            else:
                title += f" - All Countries ({len(countries_in_data)})"

            table = Table(title=title)
            table.add_column("Search Term", style="cyan", max_width=30)
            table.add_column("Ctry", style="dim", width=4)
            table.add_column("Share", justify="right", width=8)
            table.add_column("Campaign", style="magenta", max_width=20)
            table.add_column("Bid", justify="right", width=8)
            table.add_column("Strength", justify="center", width=8)
            table.add_column("Pop", justify="center", width=3)

            for row in correlated[:display_limit]:
                share_style = "green"
                if row.high_share is not None:
                    if row.high_share < 0.3:
                        share_style = "red"
                    elif row.high_share < 0.5:
                        share_style = "yellow"

                bid_display = f"{row.current_bid:.2f}" if row.current_bid else "[dim]—[/dim]"
                campaign_display = row.campaign_name[:20] if row.campaign_name else "[dim]Not matched[/dim]"

                if row.is_matched:
                    strength = row.bid_strength
                    strength_display = {
                        "STRONG": "[green]STRONG[/green]",
                        "MODERATE": "[yellow]MOD[/yellow]",
                        "WEAK": "[red]WEAK[/red]",
                    }.get(strength, "[dim]—[/dim]")
                else:
                    strength_display = "[dim]—[/dim]"

                table.add_row(
                    row.search_term[:30],
                    row.country,
                    f"[{share_style}]{row.share_range}[/{share_style}]",
                    campaign_display,
                    bid_display,
                    strength_display,
                    str(row.search_popularity) if row.search_popularity else "—",
                )

            console.print(table)

            if len(correlated) > display_limit:
                print_info(f"Showing {display_limit} of {len(correlated)} results. Use --limit 0 to see all.")

            console.print(f"\n[dim]Total search terms:[/dim] {len(correlated)}")
            console.print(f"  [green]Matched to keywords:[/green] {matched_count}")
            console.print(f"  [yellow]Unmatched (opportunities):[/yellow] {unmatched_count}")

            matched_items = [c for c in correlated if c.is_matched]
            if matched_items:
                strong = sum(1 for c in matched_items if c.bid_strength == "STRONG")
                moderate = sum(1 for c in matched_items if c.bid_strength == "MODERATE")
                weak = sum(1 for c in matched_items if c.bid_strength == "WEAK")
                unknown = sum(1 for c in matched_items if c.bid_strength == "UNKNOWN")

                console.print("\n[dim]Bid Strength (matched keywords):[/dim]")
                console.print(f"  [green]Strong:[/green] {strong}")
                console.print(f"  [yellow]Moderate:[/yellow] {moderate}")
                console.print(f"  [red]Weak:[/red] {weak}")
                if unknown > 0:
                    console.print(f"  [dim]No data:[/dim] {unknown}")

            low_share_matched = sum(
                1 for c in correlated if c.is_matched and c.high_share is not None and c.high_share < 0.3
            )
            if low_share_matched > 0:
                console.print(
                    f"\n[red]⚠ {low_share_matched} matched keywords have <30% share - consider bid increases[/red]"
                )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


def _display_bid_item(item: CorrelatedSearchTerm, index: int) -> None:
    """Display a single bid adjustment item with current status."""
    share_color = "green"
    if item.high_share is not None:
        if item.high_share < 0.3:
            share_color = "red"
        elif item.high_share < 0.5:
            share_color = "yellow"

    strength = item.bid_strength
    strength_display = {
        "STRONG": "[green]STRONG[/green]",
        "MODERATE": "[yellow]MOD[/yellow]",
        "WEAK": "[red]WEAK[/red]",
    }.get(strength, "[dim]—[/dim]")

    console.print(f"\n[bold cyan]#{index + 1}[/bold cyan] [bold]{item.search_term}[/bold]")
    console.print(f"  Country: {item.country}  |  Campaign: {item.campaign_name}")
    console.print(
        f"  Share: [{share_color}]{item.share_range}[/{share_color}]  |  "
        f"Strength: {strength_display}  |  "
        f"Popularity: {item.search_popularity or '—'}"
    )
    console.print(f"  [bold]Current Bid: {item.currency or 'USD'} {(item.current_bid or Decimal('0')):.2f}[/bold]")


def _suggest_bid(item: CorrelatedSearchTerm) -> Decimal:
    """Suggest a new bid based on impression share and current bid."""
    if item.current_bid is None:
        return Decimal("1.00")

    current = item.current_bid
    avg_share = item.avg_share

    if avg_share < 0.2:
        return (current * Decimal("1.50")).quantize(Decimal("0.01"))
    if avg_share < 0.4:
        return (current * Decimal("1.25")).quantize(Decimal("0.01"))
    if avg_share < 0.6:
        return (current * Decimal("1.10")).quantize(Decimal("0.01"))
    return (current * Decimal("1.05")).quantize(Decimal("0.01"))


@app.command("bid-adjust")
def bid_adjust(
    days: Annotated[int, typer.Option("--days", "-d", help="Number of days to analyze")] = 7,
    adam_ids: Annotated[
        list[str] | None,
        typer.Option("--adam-id", help="Filter by App Store adamId(s)"),
    ] = None,
    country: Annotated[str | None, typer.Option("--country", "-c", help="Filter by country")] = None,
    min_share: Annotated[
        float | None, typer.Option("--min-share", help="Only show keywords with share below this %")
    ] = 50,
    auto_apply: Annotated[bool, typer.Option("--auto", help="Auto-apply suggested bids without prompting")] = False,
) -> None:
    """Interactively adjust keyword bids based on impression share."""
    client = get_client()
    days = min(max(days, 1), 30)

    try:
        with client:
            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days - 1)

            with spinner("Fetching impression share insights..."):
                rows = _fetch_impression_share_rows(
                    client,
                    start_date=start_date,
                    end_date=end_date,
                    adam_ids=adam_ids,
                    country=country,
                )

            if not rows:
                print_warning("No impression share data found")
                raise typer.Exit(0)

            print_success(f"Retrieved {len(rows)} records")

            raw_data = _parse_rows(rows)
            aggregated_dict = _aggregate_by_search_term(raw_data)
            aggregated = list(aggregated_dict.values())

            if country:
                country_upper = country.upper()
                aggregated = [d for d in aggregated if d.country == country_upper]
                if not aggregated:
                    print_warning(f"No data found for country {country_upper}")
                    raise typer.Exit(0)

            countries_in_data = {d.country for d in aggregated if d.country}

            with spinner(f"Building keyword index for {len(countries_in_data)} countries..."):
                keyword_indices = _build_keyword_index_all_countries(client, countries_in_data, start_date, end_date)

            correlated: list[CorrelatedSearchTerm] = []
            for data in aggregated:
                country_index = keyword_indices.get(data.country.upper(), {})
                search_term_lower = data.search_term.lower()
                matches = country_index.get(search_term_lower, [])

                if matches:
                    best = min(matches, key=lambda k: len(k.keyword_text))
                    correlated.append(
                        CorrelatedSearchTerm(
                            search_term=data.search_term,
                            country=data.country,
                            app_name=data.app_name,
                            low_share=data.low_share,
                            high_share=data.high_share,
                            rank=data.rank,
                            search_popularity=data.search_popularity,
                            campaign_id=best.campaign_id,
                            campaign_name=best.campaign_name,
                            ad_group_id=best.ad_group_id,
                            ad_group_name=best.ad_group_name,
                            keyword_id=best.keyword_id,
                            keyword_text=best.keyword_text,
                            current_bid=best.bid_amount,
                            currency=best.currency,
                            impressions=best.impressions,
                            taps=best.taps,
                            ttr=best.ttr,
                        )
                    )

            if not correlated:
                print_warning("No keywords matched to search terms")
                raise typer.Exit(0)

            if min_share is not None:
                threshold = min_share / 100.0
                candidates = [
                    c for c in correlated if c.is_matched and c.high_share is not None and c.high_share < threshold
                ]
            else:
                candidates = [c for c in correlated if c.is_matched]

            candidates.sort(key=lambda c: c.avg_share)

            if not candidates:
                print_success("No keywords need bid adjustments!")
                raise typer.Exit(0)

            console.print(f"\n[bold]Found {len(candidates)} keywords for bid review[/bold]")
            console.print("[dim]Commands: (y)es, (n)o, (s)kip, (q)uit, or enter custom bid[/dim]\n")

            changes_made: list[tuple[CorrelatedSearchTerm, Decimal, Decimal]] = []

            for i, item in enumerate(candidates):
                _display_bid_item(item, i)

                suggested = _suggest_bid(item)
                console.print(f"  [green]Suggested Bid: {item.currency or 'USD'} {suggested:.2f}[/green]")

                if auto_apply:
                    response = "y"
                else:
                    response = typer.prompt(
                        "  Apply suggested bid? [y/n/s/q/amount]",
                        default="n",
                        show_default=False,
                    )

                response = response.strip().lower()

                if response == "q":
                    console.print("\n[yellow]Quit - stopping review[/yellow]")
                    break
                if response == "s":
                    console.print("  [dim]Skipped[/dim]")
                    continue
                if response == "n":
                    console.print("  [dim]No change[/dim]")
                    continue
                if response == "y":
                    new_bid = suggested
                else:
                    try:
                        new_bid = Decimal(response).quantize(Decimal("0.01"))
                        if new_bid <= 0:
                            console.print("  [red]Invalid bid amount[/red]")
                            continue
                    except Exception:
                        console.print("  [red]Invalid input, skipping[/red]")
                        continue

                if item.keyword_id:
                    try:
                        updated = client.keywords.update(
                            item.keyword_id,
                            KeywordUpdate(bid=Money(amount=str(new_bid), currency=item.currency or "USD")),
                        )
                        old_bid = item.current_bid or Decimal("0")
                        actual_bid = (
                            Decimal(updated.bid.amount) if updated.bid and updated.bid.amount is not None else new_bid
                        )
                        changes_made.append((item, old_bid, actual_bid))
                        console.print(
                            f"  [green]Updated: {item.currency or 'USD'} {old_bid:.2f} -> {actual_bid:.2f}[/green]"
                        )
                    except AppleSearchAdsError as e:
                        console.print(f"  [red]Failed to update: {e}[/red]")
                else:
                    console.print("  [red]Missing keyword ID[/red]")

            console.print("\n" + "=" * 50)
            if changes_made:
                console.print(f"[bold green]Applied {len(changes_made)} bid changes:[/bold green]")
                for item, old, new in changes_made:
                    change_pct = ((new - old) / old * 100) if old > 0 else 0
                    console.print(
                        f"  {item.keyword_text} ({item.country}): "
                        f"{item.currency or 'USD'} {old:.2f} -> {new:.2f} "
                        f"[dim](+{change_pct:.0f}%)[/dim]"
                    )
            else:
                console.print("[dim]No changes made[/dim]")

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


def _default_sun_sat_week(weeks_ago: int = 1) -> tuple[date, date]:
    """Return ``(sunday, saturday)`` for the completed Sun-Sat week ``weeks_ago`` back."""
    today = date.today()
    # Python weekday(): Mon=0 ... Sat=5, Sun=6
    days_since_sunday = (today.weekday() + 1) % 7
    current_week_sunday = today - timedelta(days=days_since_sunday)
    target_sunday = current_week_sunday - timedelta(days=7 * weeks_ago)
    target_saturday = target_sunday + timedelta(days=6)
    return target_sunday, target_saturday


@app.command("popularity")
def search_term_popularity(
    country: Annotated[
        str | None,
        typer.Option("--country", "-c", help="Filter by country/region code (e.g. US, AU)"),
    ] = None,
    genre: Annotated[
        str | None,
        typer.Option("--genre", "-g", help="Filter by App Store genre (partial match, e.g. Productivity)"),
    ] = None,
    search: Annotated[
        str | None,
        typer.Option("--search", "-s", help="Filter by search term (partial match)"),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option("--start", help="Start date (YYYY-MM-DD; Sunday for WEEKLY_SUN_SAT)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="End date (YYYY-MM-DD)"),
    ] = None,
    granularity: Annotated[
        SearchTermPopularityGranularity,
        typer.Option("--granularity", help="Time granularity (WEEKLY_SUN_SAT or MONTHLY)"),
    ] = SearchTermPopularityGranularity.WEEKLY_SUN_SAT,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum rows to display"),
    ] = 50,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Query App Store search term popularity rankings (Platform API v1 Insights).

    Examples:
        asa impression-share popularity --country US --genre Productivity
        asa impression-share popularity --country AU --search "calculator"
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format

    if start and end:
        start_date = parse_date(start)
        end_date = parse_date(end)
        auto_window = False
    elif granularity == SearchTermPopularityGranularity.MONTHLY:
        first_of_month = date.today().replace(day=1)
        end_date = first_of_month - timedelta(days=1)
        start_date = end_date.replace(day=1)
        auto_window = False
    else:
        start_date, end_date = _default_sun_sat_week(weeks_ago=1)
        auto_window = True

    fields = [
        "rankInGenre",
        "searchPopularityInGenre",
        "searchPopularity1to100",
        "searchPopularity1to5",
    ]

    client = get_client()

    try:
        with client:
            with spinner("Querying search term popularity..."):
                req = SearchTermPopularityQueryRequest(
                    fields=fields,
                    time_range=SearchTermPopularityTimeRange(
                        start=start_date,
                        end=end_date,
                        granularity=granularity,
                    ),
                )
                rows: list[SearchTermPopularityRow] = list(client.insights.iter_search_term_popularity(req))

                # The most recently completed Sun-Sat week can lag publication; fall back 1 week.
                if not rows and auto_window:
                    start_date, end_date = _default_sun_sat_week(weeks_ago=2)
                    req = SearchTermPopularityQueryRequest(
                        fields=fields,
                        time_range=SearchTermPopularityTimeRange(
                            start=start_date,
                            end=end_date,
                            granularity=granularity,
                        ),
                    )
                    rows = list(client.insights.iter_search_term_popularity(req))

        # Apply filters client-side (live API returns 0 rows when server-side filters are sent)
        if country:
            country_upper = country.upper()
            rows = [r for r in rows if (r.country_or_region or "").upper() == country_upper]
        if genre:
            genre_lower = genre.lower()
            rows = [r for r in rows if r.genre and genre_lower in r.genre.lower()]
        if search:
            search_lower = search.lower()
            rows = [r for r in rows if r.search_term and search_lower in r.search_term.lower()]

        if limit > 0:
            rows = rows[:limit]

        records = [
            {
                "period": r.week.isoformat() if r.week else (r.month or "-"),
                "country": r.country_or_region or "-",
                "genre": r.genre or "-",
                "search_term": r.search_term or "-",
                "rank_in_genre": r.rank_in_genre,
                "pop_in_genre": r.search_popularity_in_genre,
                "pop_100": r.search_popularity_1_to_100,
                "pop_5": r.search_popularity_1_to_5,
            }
            for r in rows
        ]

        output_data(
            records,
            columns=[
                "period",
                "country",
                "genre",
                "search_term",
                "rank_in_genre",
                "pop_in_genre",
                "pop_100",
                "pop_5",
            ],
            format=effective_format,
            title=f"Search Term Popularity ({start_date} to {end_date})",
        )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
