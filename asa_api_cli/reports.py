"""Report CLI commands for Apple Ads Platform API v1."""

import csv
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.reports import (
    AppsAdReportRow,
    AppsIncludeRows,
    AppsMetrics,
    AppsOptions,
    AppsReportingAd,
    AppsReportingAdGroup,
    AppsReportingCampaign,
    AppsReportingRequest,
    ReportFilter,
    ReportFilterOperator,
    ReportGranularity,
    ReportingKeyword,
    ReportingSearchTerm,
    ReportTimeRange,
    ReportTimeZone,
    RequestPagination,
)
from rich.panel import Panel
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    cli_state,
    console,
    enum_value,
    format_number,
    format_percent,
    get_client,
    handle_api_error,
    output_json,
    parse_date,
    print_error,
    print_success,
    print_warning,
    spinner,
)

app = typer.Typer(help="Generate performance reports")

DEFAULT_PAGE_SIZE = 5000


def report_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a Platform API v1 report row to a display dictionary."""
    result: dict[str, Any] = {}

    meta = getattr(row, "metadata", None)
    if meta is not None:
        if isinstance(meta, AppsReportingCampaign):
            result["id"] = meta.id
            result["campaign"] = meta.name or str(meta.id or "-")
            if meta.country_or_region:
                result["country"] = meta.country_or_region
            elif meta.targeting and meta.targeting.country_or_region and meta.targeting.country_or_region.include:
                result["country"] = ",".join(meta.targeting.country_or_region.include)
        elif isinstance(meta, AppsReportingAdGroup):
            result["id"] = meta.id
            result["ad_group"] = meta.name or str(meta.id or "-")
            if meta.country_or_region:
                result["country"] = meta.country_or_region
        elif isinstance(meta, AppsReportingAd):
            result["id"] = meta.id
            result["ad"] = meta.name or str(meta.id or "-")
            if meta.creative and meta.creative.creative_type:
                result["creative_type"] = enum_value(meta.creative.creative_type)
            if meta.country_or_region:
                result["country"] = meta.country_or_region
        elif isinstance(meta, ReportingKeyword):
            result["id"] = meta.id
            result["keyword"] = meta.text or str(meta.id or "-")
            if meta.match_type:
                result["match_type"] = enum_value(meta.match_type)
            if meta.ad_group and meta.ad_group.name:
                result["ad_group"] = meta.ad_group.name
            if meta.country_or_region:
                result["country"] = meta.country_or_region
        elif isinstance(meta, ReportingSearchTerm):
            result["search_term"] = meta.search_term_text or "-"
            if meta.keyword and meta.keyword.text:
                result["keyword"] = meta.keyword.text
            if meta.ad_group and meta.ad_group.name:
                result["ad_group"] = meta.ad_group.name
            if meta.country_or_region:
                result["country"] = meta.country_or_region

    total: AppsMetrics | None = getattr(row, "total_metrics", None)
    if total is not None:
        result["impressions"] = total.impressions
        result["taps"] = total.taps
        result["installs"] = total.tap_installs if total.tap_installs is not None else total.total_installs
        result["ttr"] = total.ttr
        result["conv_rate"] = total.tap_install_rate
        result["spend"] = total.local_spend.amount if total.local_spend else None
        result["currency"] = total.local_spend.currency if total.local_spend else None
        result["avg_cpt"] = total.cpt.amount if total.cpt else None
        avg_cpi = total.total_avg_cpi or total.tap_install_cpi
        result["avg_cpa"] = avg_cpi.amount if avg_cpi else None

    return result


def _execute_apps_report(
    fetch_fn: Callable[[AppsReportingRequest], Any],
    *,
    start_date: date,
    end_date: date,
    granularity: ReportGranularity,
    time_zone: ReportTimeZone,
    filters: list[ReportFilter] | None = None,
) -> tuple[list[Any], AppsMetrics | None]:
    """Execute a paginated APPS report and return ``(rows, grand_total)``."""
    all_rows: list[Any] = []
    grand_total: AppsMetrics | None = None
    offset = 0

    while True:
        req = AppsReportingRequest(
            filters=filters or None,
            time_range=ReportTimeRange(
                start=start_date,
                end=end_date,
                time_zone=time_zone,
                granularity=granularity,
            ),
            pagination=RequestPagination(offset=offset, page_size=DEFAULT_PAGE_SIZE),
            options=AppsOptions(include_rows=[AppsIncludeRows.GRAND_TOTAL]),
        )
        resp = fetch_fn(req)
        container = getattr(resp, "result", None)
        rows = list(container.rows or []) if container is not None else []
        if offset == 0 and container is not None and container.summary is not None:
            grand_total = container.summary.grand_total
        all_rows.extend(rows)

        if not rows:
            break
        pagination = getattr(resp, "pagination", None)
        if pagination is not None and pagination.total_count is not None:
            if offset + len(rows) >= pagination.total_count:
                break
        elif len(rows) < DEFAULT_PAGE_SIZE:
            break
        offset += len(rows)

    return all_rows, grand_total


def print_report_table(
    data: list[dict[str, Any]],
    columns: list[str],
    title: str,
) -> None:
    """Print report data as a styled table."""
    table = Table(
        title=title,
        show_header=True,
        header_style="header",
        border_style="muted",
        row_styles=["", "dim"],
    )

    column_labels = {
        "campaign": "Campaign",
        "ad_group": "Ad Group",
        "ad": "Ad",
        "creative_type": "Creative Type",
        "keyword": "Keyword",
        "match_type": "Match",
        "search_term": "Search Term",
        "country": "Country",
        "impressions": "Impressions",
        "taps": "Taps",
        "installs": "Installs",
        "ttr": "TTR",
        "conv_rate": "Conv Rate",
        "spend": "Spend",
        "avg_cpt": "Avg CPT",
        "avg_cpa": "Avg CPA",
    }

    for col in columns:
        justify: Any = (
            "right"
            if col in ("impressions", "taps", "installs", "ttr", "conv_rate", "spend", "avg_cpt", "avg_cpa")
            else "left"
        )
        table.add_column(column_labels.get(col, col), justify=justify)

    for row in data:
        values = []
        for col in columns:
            value = row.get(col)
            if col in ("ttr", "conv_rate"):
                values.append(format_percent(value))
            elif col in ("impressions", "taps", "installs"):
                values.append(format_number(value))
            elif col in ("spend", "avg_cpt", "avg_cpa"):
                values.append(str(value) if value is not None else "-")
            else:
                values.append(str(value) if value else "-")
        table.add_row(*values)

    console.print(table)


def print_grand_totals(total: AppsMetrics | None) -> None:
    """Print grand totals in a styled panel."""
    if total is None:
        return

    installs = total.tap_installs if total.tap_installs is not None else total.total_installs
    lines = [
        f"[label]Impressions:[/label] [value]{format_number(total.impressions)}[/value]",
        f"[label]Taps:[/label] [value]{format_number(total.taps)}[/value]",
        f"[label]Installs:[/label] [value]{format_number(installs)}[/value]",
    ]

    if total.ttr is not None:
        lines.append(f"[label]TTR:[/label] [value]{format_percent(total.ttr)}[/value]")
    if total.tap_install_rate is not None:
        lines.append(f"[label]Conv Rate:[/label] [value]{format_percent(total.tap_install_rate)}[/value]")
    if total.local_spend and total.local_spend.amount is not None:
        spend = total.local_spend
        currency = f" {spend.currency}" if spend.currency else ""
        lines.append(f"[label]Total Spend:[/label] [value]{spend.amount}{currency}[/value]")

    panel = Panel(
        "\n".join(lines),
        title="[info]Grand Totals[/info]",
        border_style="cyan",
        padding=(0, 1),
    )
    console.print()
    console.print(panel)


def save_report(
    data: list[dict[str, Any]],
    output: Path,
    columns: list[str],
) -> None:
    """Save report to file."""
    suffix = output.suffix.lower()

    if suffix == ".json":
        output.write_text(json.dumps(data, indent=2, default=str))
    elif suffix == ".csv":
        with output.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
    else:
        print_error(
            "Unsupported Format",
            f"File format '{suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

    print_success(f"Report saved to {output}")


def _render_report_result(
    rows: list[Any],
    grand_total: AppsMetrics | None,
    *,
    columns: list[str],
    title: str,
    output: Path | None,
) -> None:
    """Render report rows to file, JSON stdout, or Rich table."""
    if not rows:
        if cli_state.json_output and output is None:
            output_json([])
        else:
            print_warning("No data found for the specified period")
        return

    data = [report_row_to_dict(row) for row in rows]
    if output:
        save_report(data, output, columns)
    elif cli_state.json_output:
        output_json(data)
    else:
        print_report_table(data, columns, title)
        print_grand_totals(grand_total)


@app.command("campaigns")
def campaign_report(
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ],
    campaign_ids: Annotated[
        list[int] | None,
        typer.Option("--campaign", "-c", help="Filter by campaign ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a campaign performance report.

    Examples:
        asa reports campaigns --start 2026-01-01 --end 2026-01-31
        asa reports campaigns -s 2026-01-01 -e 2026-01-31 --campaign 123
        asa reports campaigns -s 2026-01-01 -e 2026-01-31 -o report.csv
        asa reports campaigns -s 2026-01-01 -e 2026-01-31 --granularity MONTHLY
    """
    start_date = parse_date(start)
    end_date = parse_date(end)

    filters: list[ReportFilter] | None = None
    if campaign_ids:
        filters = [
            ReportFilter(
                field="id",
                operator=ReportFilterOperator.IN,
                value=[str(cid) for cid in campaign_ids],
            )
        ]

    client = get_client()

    try:
        with client:
            with spinner("Generating campaign report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.campaigns,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=ReportTimeZone.UTC,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["campaign", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Campaign Report ({start} to {end})",
                output=output,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("ad-groups")
def ad_group_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ],
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate an ad group performance report.

    Examples:
        asa reports ad-groups 123 --start 2026-01-01 --end 2026-01-31
        asa reports ad-groups 123 -s 2026-01-01 -e 2026-01-31 -o report.csv
    """
    start_date = parse_date(start)
    end_date = parse_date(end)

    filters = [
        ReportFilter(
            field="campaignId",
            operator=ReportFilterOperator.EQUALS,
            value=str(campaign_id),
        )
    ]
    if ad_group_ids:
        filters.append(
            ReportFilter(
                field="adGroupId",
                operator=ReportFilterOperator.IN,
                value=[str(aid) for aid in ad_group_ids],
            )
        )

    client = get_client()

    try:
        with client:
            with spinner("Generating ad group report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.ad_groups,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=ReportTimeZone.UTC,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["ad_group", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Ad Group Report ({start} to {end})",
                output=output,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("ads")
def ad_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ],
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity (HOURLY not supported)"),
    ] = ReportGranularity.DAILY,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate an ad-level performance report.

    Examples:
        asa reports ads 123 --start 2026-01-01 --end 2026-01-31
        asa reports ads 123 -s 2026-01-01 -e 2026-01-31 --ad-group 456
    """
    if granularity == ReportGranularity.HOURLY:
        print_error(
            "Unsupported Granularity",
            "HOURLY granularity is not supported for ad-level reports.",
            hint="Use DAILY, WEEKLY, or MONTHLY.",
        )
        raise typer.Exit(EXIT_USAGE)

    start_date = parse_date(start)
    end_date = parse_date(end)

    filters = [
        ReportFilter(
            field="campaignId",
            operator=ReportFilterOperator.EQUALS,
            value=str(campaign_id),
        )
    ]
    if ad_group_ids:
        filters.append(
            ReportFilter(
                field="adGroupId",
                operator=ReportFilterOperator.IN,
                value=[str(aid) for aid in ad_group_ids],
            )
        )

    client = get_client()

    try:
        with client:
            with spinner("Generating ad report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.ads,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=ReportTimeZone.UTC,
                    filters=filters,
                )
            typed_rows: list[AppsAdReportRow] = rows

            _render_report_result(
                typed_rows,
                grand_total,
                columns=["ad", "creative_type", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Ad Report ({start} to {end})",
                output=output,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("keywords")
def keyword_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ],
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a keyword performance report.

    Examples:
        asa reports keywords 123 --start 2026-01-01 --end 2026-01-31
        asa reports keywords 123 -s 2026-01-01 -e 2026-01-31 --ad-group 456
        asa reports keywords 123 -s 2026-01-01 -e 2026-01-31 -o keywords.csv
    """
    start_date = parse_date(start)
    end_date = parse_date(end)

    filters = [
        ReportFilter(
            field="campaignId",
            operator=ReportFilterOperator.EQUALS,
            value=str(campaign_id),
        )
    ]
    if ad_group_ids:
        filters.append(
            ReportFilter(
                field="adGroupId",
                operator=ReportFilterOperator.IN,
                value=[str(aid) for aid in ad_group_ids],
            )
        )

    client = get_client()

    try:
        with client:
            with spinner("Generating keyword report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.keywords,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=ReportTimeZone.UTC,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["keyword", "impressions", "taps", "installs", "ttr", "conv_rate", "spend", "avg_cpt"],
                title=f"Keyword Report ({start} to {end})",
                output=output,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("search-terms")
def search_term_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ],
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity (HOURLY not supported)"),
    ] = ReportGranularity.DAILY,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a search term performance report.

    Shows actual search queries that triggered your ads.
    Note: Search term reports always run in organization time zone (ORTZ).

    Examples:
        asa reports search-terms 123 --start 2026-01-01 --end 2026-01-31
        asa reports search-terms 123 -s 2026-01-01 -e 2026-01-31 -o terms.csv
    """
    if granularity == ReportGranularity.HOURLY:
        print_error(
            "Unsupported Granularity",
            "HOURLY granularity is not supported for search term reports.",
            hint="Use DAILY, WEEKLY, or MONTHLY.",
        )
        raise typer.Exit(EXIT_USAGE)

    start_date = parse_date(start)
    end_date = parse_date(end)

    filters = [
        ReportFilter(
            field="campaignId",
            operator=ReportFilterOperator.EQUALS,
            value=str(campaign_id),
        )
    ]
    if ad_group_id is not None:
        filters.append(
            ReportFilter(
                field="adGroupId",
                operator=ReportFilterOperator.EQUALS,
                value=str(ad_group_id),
            )
        )

    client = get_client()

    try:
        with client:
            with spinner("Generating search term report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.search_terms,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=ReportTimeZone.ORTZ,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["search_term", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Search Term Report ({start} to {end})",
                output=output,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
