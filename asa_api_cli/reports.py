"""Report CLI commands for Apple Ads Platform API v1."""

import csv
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
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
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from asa_api_cli.report_export import (
    DEFAULT_REPORT_PAGE_SIZE,
    collect_app_report_export,
    serialize_apps_metrics,
    write_export_json_atomic,
)
from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    campaign_adam_id,
    cli_state,
    console,
    enum_value,
    error_console,
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

app: typer.Typer = typer.Typer(help="Generate performance reports and reconcilable per-app exports")

DEFAULT_PAGE_SIZE = DEFAULT_REPORT_PAGE_SIZE


def _resolve_date_args(
    start: str | None,
    end: str | None,
    days: int | None,
) -> tuple[date, date]:
    """Resolve ``(--start, --end)`` or ``--days`` into an inclusive ``(start_date, end_date)`` pair."""
    if days is not None:
        if start is not None or end is not None:
            print_error(
                "Conflicting Date Options",
                "Cannot combine --days with --start or --end.",
                hint="Pass either --start YYYY-MM-DD --end YYYY-MM-DD or --days N.",
            )
            raise typer.Exit(EXIT_USAGE)
        if days < 1:
            print_error("Invalid Days", "--days must be at least 1.")
            raise typer.Exit(EXIT_USAGE)
        end_date = datetime.now(tz=UTC).date() - timedelta(days=1)
        start_date = end_date - timedelta(days=days - 1)
        return start_date, end_date

    if start is None or end is None:
        print_error(
            "Missing Date Range",
            "Both --start and --end are required unless --days is specified.",
            hint="Use --start YYYY-MM-DD --end YYYY-MM-DD (or --days 14).",
        )
        raise typer.Exit(EXIT_USAGE)

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date > end_date:
        print_error(
            "Invalid Date Range",
            f"--start ({start_date.isoformat()}) must not be after --end ({end_date.isoformat()}).",
        )
        raise typer.Exit(EXIT_USAGE)
    return start_date, end_date


def _apply_row_metadata(row: Any, result: dict[str, Any]) -> None:
    """Populate stable entity identifiers and dimensions from a report row's metadata."""
    meta = getattr(row, "metadata", None)
    if meta is None:
        return

    ad_account_id = getattr(meta, "ad_account_id", None)
    if ad_account_id is not None:
        result["ad_account_id"] = ad_account_id

    if isinstance(meta, AppsReportingCampaign):
        result["id"] = meta.id
        result["campaign_id"] = meta.id
        if meta.promoted_object_id:
            try:
                result["adam_id"] = int(meta.promoted_object_id)
            except ValueError:
                pass
        result["campaign"] = meta.name or str(meta.id or "-")
        if meta.status is not None:
            result["status"] = enum_value(meta.status)
        if meta.deleted is not None:
            result["deleted"] = meta.deleted
        if meta.country_or_region:
            result["country"] = meta.country_or_region
        elif meta.targeting and meta.targeting.country_or_region and meta.targeting.country_or_region.include:
            result["country"] = ",".join(meta.targeting.country_or_region.include)
        if meta.device_class:
            result["device_class"] = meta.device_class
    elif isinstance(meta, AppsReportingAdGroup):
        result["id"] = meta.id
        result["campaign_id"] = meta.campaign_id
        result["ad_group_id"] = meta.id
        result["ad_group"] = meta.name or str(meta.id or "-")
        if meta.status is not None:
            result["status"] = enum_value(meta.status)
        if meta.deleted is not None:
            result["deleted"] = meta.deleted
        if meta.country_or_region:
            result["country"] = meta.country_or_region
        if meta.device_class:
            result["device_class"] = meta.device_class
    elif isinstance(meta, AppsReportingAd):
        result["id"] = meta.id
        result["campaign_id"] = meta.campaign_id
        result["ad_group_id"] = meta.ad_group_id
        result["ad_id"] = meta.id
        result["ad"] = meta.name or str(meta.id or "-")
        if meta.creative:
            if meta.creative.id is not None:
                result["creative_id"] = meta.creative.id
            if meta.creative.creative_type:
                result["creative_type"] = enum_value(meta.creative.creative_type)
            if meta.creative.creative_spec and meta.creative.creative_spec.language:
                result["language"] = meta.creative.creative_spec.language
        if meta.status is not None:
            result["status"] = enum_value(meta.status)
        if meta.deleted is not None:
            result["deleted"] = meta.deleted
        if meta.country_or_region:
            result["country"] = meta.country_or_region
        if meta.device_class:
            result["device_class"] = meta.device_class
    elif isinstance(meta, ReportingKeyword):
        result["id"] = meta.id
        result["campaign_id"] = meta.campaign_id
        result["ad_group_id"] = meta.ad_group_id
        result["keyword_id"] = meta.id
        result["keyword"] = meta.text or str(meta.id or "-")
        if meta.match_type:
            result["match_type"] = enum_value(meta.match_type)
        if meta.ad_group and meta.ad_group.name:
            result["ad_group"] = meta.ad_group.name
        if meta.status is not None:
            result["status"] = enum_value(meta.status)
        if meta.deleted is not None:
            result["deleted"] = meta.deleted
        if meta.country_or_region:
            result["country"] = meta.country_or_region
        if meta.device_class:
            result["device_class"] = meta.device_class
    elif isinstance(meta, ReportingSearchTerm):
        result["campaign_id"] = meta.campaign_id
        result["ad_group_id"] = meta.ad_group_id
        result["search_term"] = meta.search_term_text or "-"
        if meta.search_term_source:
            result["search_term_source"] = meta.search_term_source
        if meta.keyword:
            if meta.keyword.id is not None:
                result["keyword_id"] = meta.keyword.id
            if meta.keyword.text:
                result["keyword"] = meta.keyword.text
            if meta.keyword.match_type:
                result["match_type"] = enum_value(meta.keyword.match_type)
        if meta.ad_group and meta.ad_group.name:
            result["ad_group"] = meta.ad_group.name
        if meta.country_or_region:
            result["country"] = meta.country_or_region
        if meta.device_class:
            result["device_class"] = meta.device_class


def _apply_metrics_dict(
    metrics: AppsMetrics | None,
    result: dict[str, Any],
    *,
    default_currency: str | None = None,
) -> None:
    """Attach explicit tap/view/total metrics and consistent tap-basis table aliases."""
    if metrics is None:
        return
    if metrics.date is not None:
        result["date"] = metrics.date.isoformat()

    explicit, _ = serialize_apps_metrics(metrics, default_currency=default_currency)
    result.update(explicit)

    # Convenience display columns strictly share the tap attribution basis
    result["attribution_basis"] = "tap"
    result["installs"] = explicit["tap_installs"]
    result["conv_rate"] = explicit["tap_install_rate"]
    result["spend"] = explicit["local_spend"]
    result["avg_cpt"] = explicit["cpt"]
    result["avg_cpa"] = explicit["tap_install_cpi"]


def report_row_to_dict(
    row: Any,
    *,
    time_zone: ReportTimeZone | None = None,
    default_currency: str | None = None,
    metrics_override: AppsMetrics | None = None,
) -> dict[str, Any]:
    """Convert a Platform API v1 report row to a dictionary with stable IDs and explicit attribution."""
    result: dict[str, Any] = {}
    _apply_row_metadata(row, result)
    if time_zone is not None:
        result["time_zone"] = time_zone.value

    target_metrics: AppsMetrics | None = (
        metrics_override if metrics_override is not None else getattr(row, "total_metrics", None)
    )
    _apply_metrics_dict(target_metrics, result, default_currency=default_currency)
    return result


def report_rows_to_dicts(
    rows: list[Any],
    *,
    daily: bool = False,
    time_zone: ReportTimeZone | None = None,
    default_currency: str | None = None,
) -> list[dict[str, Any]]:
    """Convert report rows to either entity-period summaries or daily observations."""
    if not daily:
        return [report_row_to_dict(row, time_zone=time_zone, default_currency=default_currency) for row in rows]

    out: list[dict[str, Any]] = []
    for row in rows:
        granular: list[AppsMetrics] = list(getattr(row, "granular_metrics", None) or [])
        if granular:
            for entry in granular:
                out.append(
                    report_row_to_dict(
                        row,
                        time_zone=time_zone,
                        default_currency=default_currency,
                        metrics_override=entry,
                    )
                )
        elif getattr(row, "total_metrics", None) is not None:
            out.append(
                report_row_to_dict(
                    row,
                    time_zone=time_zone,
                    default_currency=default_currency,
                )
            )
    return out


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

        pagination = getattr(resp, "pagination", None)
        total_count = pagination.total_count if pagination is not None else None
        if not rows:
            if total_count is not None and offset < total_count:
                raise AppleSearchAdsError(f"Incomplete report pagination: received {offset} of {total_count} rows.")
            break
        if total_count is not None:
            if offset + len(rows) >= total_count:
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
        "date": "Date",
        "campaign_id": "Campaign ID",
        "campaign": "Campaign",
        "ad_group_id": "Ad Group ID",
        "ad_group": "Ad Group",
        "ad_id": "Ad ID",
        "ad": "Ad",
        "creative_type": "Creative Type",
        "keyword_id": "Keyword ID",
        "keyword": "Keyword",
        "match_type": "Match",
        "search_term": "Search Term",
        "search_term_source": "Source",
        "country": "Country",
        "time_zone": "TZ",
        "currency": "Currency",
        "impressions": "Impressions",
        "taps": "Taps",
        "installs": "Tap Installs",
        "tap_installs": "Tap Installs",
        "view_installs": "View Installs",
        "total_installs": "Total Installs",
        "ttr": "TTR",
        "conv_rate": "Tap CVR",
        "spend": "Spend",
        "avg_cpt": "Avg CPT",
        "avg_cpa": "Tap CPI",
    }

    right_cols = {
        "campaign_id",
        "ad_group_id",
        "ad_id",
        "keyword_id",
        "impressions",
        "taps",
        "installs",
        "tap_installs",
        "view_installs",
        "total_installs",
        "ttr",
        "conv_rate",
        "spend",
        "avg_cpt",
        "avg_cpa",
    }

    for col in columns:
        justify: Any = "right" if col in right_cols else "left"
        table.add_column(column_labels.get(col, col), justify=justify)

    for row in data:
        values = []
        for col in columns:
            value = row.get(col)
            if col in ("ttr", "conv_rate", "tap_install_rate", "total_install_rate"):
                values.append(format_percent(value))
            elif col in ("impressions", "taps", "installs", "tap_installs", "view_installs", "total_installs"):
                values.append(format_number(value))
            elif col in ("spend", "avg_cpt", "avg_cpa", "local_spend", "tap_install_cpi", "total_avg_cpi"):
                values.append(str(value) if value is not None else "-")
            elif value is None or value == "":
                values.append("-")
            else:
                values.append(str(value))
        table.add_row(*values)

    console.print(table)


def print_grand_totals(total: AppsMetrics | None) -> None:
    """Print grand totals in a styled panel."""
    if total is None:
        return

    lines = [
        f"[label]Impressions:[/label] [value]{format_number(total.impressions)}[/value]",
        f"[label]Taps:[/label] [value]{format_number(total.taps)}[/value]",
        f"[label]Tap Installs:[/label] [value]{format_number(total.tap_installs)}[/value]",
    ]
    if total.view_installs is not None:
        lines.append(f"[label]View Installs:[/label] [value]{format_number(total.view_installs)}[/value]")
    if total.total_installs is not None:
        lines.append(f"[label]Total Installs:[/label] [value]{format_number(total.total_installs)}[/value]")

    if total.ttr is not None:
        lines.append(f"[label]TTR:[/label] [value]{format_percent(total.ttr)}[/value]")
    if total.tap_install_rate is not None:
        lines.append(f"[label]Tap Conv Rate:[/label] [value]{format_percent(total.tap_install_rate)}[/value]")
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
    """Save report to file (including explicit empty-result files)."""
    suffix = output.suffix.lower()
    output.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".json":
        output.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    elif suffix == ".csv":
        csv_fields = list(columns)
        for row in data:
            for k in row:
                if k not in csv_fields:
                    csv_fields.append(k)
        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
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
    daily: bool = False,
    time_zone: ReportTimeZone | None = None,
    default_currency: str | None = None,
) -> None:
    """Render report rows to file, JSON stdout, or Rich table."""
    display_cols = ["date", *columns] if daily and "date" not in columns else list(columns)
    data = report_rows_to_dicts(
        rows,
        daily=daily,
        time_zone=time_zone,
        default_currency=default_currency,
    )

    if not data:
        if output is not None:
            save_report([], output, display_cols)
            if cli_state.json_output:
                output_json([])
            else:
                print_warning("No data found for the specified period (wrote empty report)")
        elif cli_state.json_output:
            output_json([])
        else:
            print_warning("No data found for the specified period")
        return

    if output:
        save_report(data, output, display_cols)
        if cli_state.json_output:
            output_json(data)
    elif cli_state.json_output:
        output_json(data)
    else:
        print_report_table(data, display_cols, title)
        print_grand_totals(grand_total)


@app.command("export")
def export_app_report(
    adam_id: Annotated[
        int,
        typer.Option(
            "--app",
            "--adam-id",
            "-a",
            help="App Store adamId to isolate and export",
        ),
    ],
    start: Annotated[
        str,
        typer.Option("--start", "-s", help="Inclusive start date (YYYY-MM-DD)"),
    ],
    end: Annotated[
        str,
        typer.Option("--end", "-e", help="Inclusive end date (YYYY-MM-DD)"),
    ],
    timezone: Annotated[
        ReportTimeZone,
        typer.Option(
            "--timezone",
            help=(
                "Reporting timezone: ORTZ (default; unified across all 5 levels including search terms) "
                "or UTC (selectable on campaigns/ad-groups/ads/keywords; search-terms marked unavailable "
                "unless --include-ortz-search-terms is passed)."
            ),
        ),
    ] = ReportTimeZone.ORTZ,
    include_ortz_search_terms: Annotated[
        bool,
        typer.Option(
            "--include-ortz-search-terms",
            help="When --timezone UTC is used, collect ORTZ search-term rows in a separate ORTZ-tagged section.",
        ),
    ] = False,
    include_zero_rows: Annotated[
        bool,
        typer.Option(
            "--include-zero-rows",
            help="Request EMPTY_METRICS on entity-level reports (campaigns, ad-groups, ads, keywords).",
        ),
    ] = False,
    page_size: Annotated[
        int,
        typer.Option(
            "--page-size",
            min=1,
            max=5000,
            help="Records per API page (1..5000).",
        ),
    ] = DEFAULT_REPORT_PAGE_SIZE,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write structured JSON export atomically to this .json file (otherwise prints JSON to stdout).",
        ),
    ] = None,
) -> None:
    """Export a reliable, read-only, reconcilable per-app performance report as JSON.

    Collects daily observations and reconciled period totals across campaigns,
    ad groups, ads, keywords, and search terms for a single App Store app
    (``--app <adamId>``) over an explicit fixed date range.

    Timezone policy:
      - ``campaigns``, ``ad-groups``, ``ads``, ``keywords``: selectable (``ORTZ`` or ``UTC``).
      - ``search-terms``: fixed to ``ORTZ`` by Apple Ads Platform API v1 (and limited
        to the trailing 90 days). Under ``--timezone UTC``, search terms are marked
        ``unavailable`` by default rather than silently mixed with UTC daily data.

    Completeness contract:
      - ``status`` is ``complete``, ``empty``, ``partial``, or ``failed`` (with ``complete: bool``).
      - ``complete`` and ``empty`` exit ``0``; ``partial`` and ``failed`` record errors in the
        JSON envelope and exit ``1`` so incomplete runs never masquerade as complete reports.

    Examples:
        asa reports export --app 654321987 --start 2026-01-01 --end 2026-01-07 -o app1-wk1.json
        asa --ad-account 123456 reports export --app 654321987 -s 2026-01-01 -e 2026-01-07 --timezone UTC
    """
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date > end_date:
        print_error(
            "Invalid Date Range",
            f"--start ({start_date.isoformat()}) must not be after --end ({end_date.isoformat()}).",
        )
        raise typer.Exit(EXIT_USAGE)

    if output is not None and output.suffix.lower() != ".json":
        print_error(
            "Unsupported Export Format",
            f"'asa reports export' writes structured multi-level JSON (got '{output.suffix or output.name}').",
            hint="Use a .json path (e.g., --output app-report.json) or omit --output to write JSON to stdout.",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    with client:
        if not cli_state.quiet and error_console.is_terminal:
            with Progress(
                SpinnerColumn(style="cyan"),
                TextColumn("[progress.description]{task.description}"),
                console=error_console,
                transient=True,
            ) as progress:
                progress.add_task(
                    description=f"Exporting app {adam_id} reports ({start} to {end})...",
                    total=None,
                )
                payload = collect_app_report_export(
                    client,
                    adam_id=adam_id,
                    start_date=start_date,
                    end_date=end_date,
                    requested_timezone=timezone,
                    include_ortz_search_terms=include_ortz_search_terms,
                    include_zero_rows=include_zero_rows,
                    page_size=page_size,
                )
        else:
            payload = collect_app_report_export(
                client,
                adam_id=adam_id,
                start_date=start_date,
                end_date=end_date,
                requested_timezone=timezone,
                include_ortz_search_terms=include_ortz_search_terms,
                include_zero_rows=include_zero_rows,
                page_size=page_size,
            )

    if output is not None:
        write_export_json_atomic(output, payload)
        if cli_state.json_output:
            output_json(payload)
        elif payload["complete"] and not cli_state.quiet:
            error_console.print(
                f"[success]✓[/success] Exported {payload['status']} report for app {adam_id} to {output}"
            )
    else:
        output_json(payload)

    if not payload["complete"]:
        err_count = len(payload.get("errors") or [])
        first_msg = payload["errors"][0]["message"] if payload.get("errors") else "Collection did not complete."
        print_error(
            f"Export {str(payload['status']).title()}",
            f"{first_msg} ({err_count} error(s) recorded in export payload).",
        )
        raise typer.Exit(EXIT_ERROR)


@app.command("campaigns")
def campaign_report(
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", "-d", help="Trailing N full days ending yesterday (alternative to --start/--end)"),
    ] = None,
    adam_id: Annotated[
        int | None,
        typer.Option("--app", "--adam-id", help="Filter campaigns by App Store adamId"),
    ] = None,
    campaign_ids: Annotated[
        list[int] | None,
        typer.Option("--campaign", "-c", help="Filter by campaign ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    timezone: Annotated[
        ReportTimeZone,
        typer.Option("--timezone", help="Reporting timezone (UTC or ORTZ)"),
    ] = ReportTimeZone.UTC,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Emit one row per (campaign, date) instead of period totals"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a campaign performance report.

    Examples:
        asa reports campaigns --start 2026-01-01 --end 2026-01-31
        asa reports campaigns --app 654321987 -s 2026-01-01 -e 2026-01-31 --daily -o daily.json
        asa reports campaigns --days 14 --campaign 123
        asa reports campaigns -s 2026-01-01 -e 2026-01-31 -o report.csv
    """
    start_date, end_date = _resolve_date_args(start, end, days)
    if output is not None and output.suffix.lower() not in (".json", ".csv"):
        print_error(
            "Unsupported Format",
            f"File format '{output.suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            resolved_ids: list[int] | None = list(campaign_ids) if campaign_ids else None
            if adam_id is not None:
                app_cids = [
                    c.id for c in client.campaigns.iter_all() if c.id is not None and campaign_adam_id(c) == adam_id
                ]
                if resolved_ids is not None:
                    allowed = set(app_cids)
                    resolved_ids = [cid for cid in resolved_ids if cid in allowed]
                else:
                    resolved_ids = app_cids

                if not resolved_ids:
                    _render_report_result(
                        [],
                        None,
                        columns=[
                            "campaign_id",
                            "campaign",
                            "impressions",
                            "taps",
                            "installs",
                            "ttr",
                            "conv_rate",
                            "spend",
                        ],
                        title=f"Campaign Report ({start_date} to {end_date})",
                        output=output,
                        daily=daily,
                        time_zone=timezone,
                    )
                    return

            filters: list[ReportFilter] | None = None
            if resolved_ids:
                filters = [
                    ReportFilter(
                        field="id",
                        operator=ReportFilterOperator.IN,
                        value=[str(cid) for cid in resolved_ids],
                    )
                ]

            with spinner("Generating campaign report..."):
                rows, grand_total = _execute_apps_report(
                    client.reports.campaigns,
                    start_date=start_date,
                    end_date=end_date,
                    granularity=granularity,
                    time_zone=timezone,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["campaign_id", "campaign", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Campaign Report ({start_date} to {end_date})",
                output=output,
                daily=daily,
                time_zone=timezone,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("ad-groups")
def ad_group_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", "-d", help="Trailing N full days ending yesterday (alternative to --start/--end)"),
    ] = None,
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    timezone: Annotated[
        ReportTimeZone,
        typer.Option("--timezone", help="Reporting timezone (UTC or ORTZ)"),
    ] = ReportTimeZone.UTC,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Emit one row per (ad group, date) instead of period totals"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate an ad group performance report.

    Examples:
        asa reports ad-groups 123 --start 2026-01-01 --end 2026-01-31
        asa reports ad-groups 123 --days 14
        asa reports ad-groups 123 -s 2026-01-01 -e 2026-01-31 -o report.csv
    """
    start_date, end_date = _resolve_date_args(start, end, days)
    if output is not None and output.suffix.lower() not in (".json", ".csv"):
        print_error(
            "Unsupported Format",
            f"File format '{output.suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

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
                    time_zone=timezone,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=["ad_group_id", "ad_group", "impressions", "taps", "installs", "ttr", "conv_rate", "spend"],
                title=f"Ad Group Report ({start_date} to {end_date})",
                output=output,
                daily=daily,
                time_zone=timezone,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("ads")
def ad_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", "-d", help="Trailing N full days ending yesterday (alternative to --start/--end)"),
    ] = None,
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity (HOURLY not supported)"),
    ] = ReportGranularity.DAILY,
    timezone: Annotated[
        ReportTimeZone,
        typer.Option("--timezone", help="Reporting timezone (UTC or ORTZ)"),
    ] = ReportTimeZone.UTC,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Emit one row per (ad, date) instead of period totals"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate an ad-level performance report.

    Examples:
        asa reports ads 123 --start 2026-01-01 --end 2026-01-31
        asa reports ads 123 --days 14
        asa reports ads 123 -s 2026-01-01 -e 2026-01-31 --ad-group 456
    """
    if granularity == ReportGranularity.HOURLY:
        print_error(
            "Unsupported Granularity",
            "HOURLY granularity is not supported for ad-level reports.",
            hint="Use DAILY, WEEKLY, or MONTHLY.",
        )
        raise typer.Exit(EXIT_USAGE)

    start_date, end_date = _resolve_date_args(start, end, days)
    if output is not None and output.suffix.lower() not in (".json", ".csv"):
        print_error(
            "Unsupported Format",
            f"File format '{output.suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

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
                    time_zone=timezone,
                    filters=filters,
                )
            typed_rows: list[AppsAdReportRow] = rows

            _render_report_result(
                typed_rows,
                grand_total,
                columns=[
                    "ad_id",
                    "ad",
                    "creative_type",
                    "impressions",
                    "taps",
                    "installs",
                    "ttr",
                    "conv_rate",
                    "spend",
                ],
                title=f"Ad Report ({start_date} to {end_date})",
                output=output,
                daily=daily,
                time_zone=timezone,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("keywords")
def keyword_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", "-d", help="Trailing N full days ending yesterday (alternative to --start/--end)"),
    ] = None,
    ad_group_ids: Annotated[
        list[int] | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID(s)"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity"),
    ] = ReportGranularity.DAILY,
    timezone: Annotated[
        ReportTimeZone,
        typer.Option("--timezone", help="Reporting timezone (UTC or ORTZ)"),
    ] = ReportTimeZone.UTC,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Emit one row per (keyword, date) instead of period totals"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a keyword performance report.

    Examples:
        asa reports keywords 123 --start 2026-01-01 --end 2026-01-31
        asa reports keywords 123 --days 30
        asa reports keywords 123 -s 2026-01-01 -e 2026-01-31 --ad-group 456
        asa reports keywords 123 -s 2026-01-01 -e 2026-01-31 -o keywords.csv
    """
    start_date, end_date = _resolve_date_args(start, end, days)
    if output is not None and output.suffix.lower() not in (".json", ".csv"):
        print_error(
            "Unsupported Format",
            f"File format '{output.suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

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
                    time_zone=timezone,
                    filters=filters,
                )

            _render_report_result(
                rows,
                grand_total,
                columns=[
                    "keyword_id",
                    "keyword",
                    "impressions",
                    "taps",
                    "installs",
                    "ttr",
                    "conv_rate",
                    "spend",
                    "avg_cpt",
                ],
                title=f"Keyword Report ({start_date} to {end_date})",
                output=output,
                daily=daily,
                time_zone=timezone,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("search-terms")
def search_term_report(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", "-d", help="Trailing N full days ending yesterday (alternative to --start/--end)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Filter by ad group ID"),
    ] = None,
    granularity: Annotated[
        ReportGranularity,
        typer.Option("--granularity", "-g", help="Time granularity (HOURLY not supported)"),
    ] = ReportGranularity.DAILY,
    timezone: Annotated[
        ReportTimeZone,
        typer.Option("--timezone", help="Reporting timezone (fixed to ORTZ for search terms)"),
    ] = ReportTimeZone.ORTZ,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Emit one row per (search term, date) instead of period totals"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save to file (JSON or CSV)"),
    ] = None,
) -> None:
    """Generate a search term performance report.

    Shows actual search queries that triggered your ads.
    Note: Search term reports are fixed to organization time zone (ORTZ) by the API.

    Examples:
        asa reports search-terms 123 --start 2026-01-01 --end 2026-01-31
        asa reports search-terms 123 --days 14 --output terms.csv
    """
    if granularity == ReportGranularity.HOURLY:
        print_error(
            "Unsupported Granularity",
            "HOURLY granularity is not supported for search term reports.",
            hint="Use DAILY, WEEKLY, or MONTHLY.",
        )
        raise typer.Exit(EXIT_USAGE)

    if timezone != ReportTimeZone.ORTZ:
        print_error(
            "Unsupported Timezone",
            "Search term reports only support ORTZ (organization timezone) in Apple Ads Platform API v1.",
            hint="Omit --timezone or pass --timezone ORTZ.",
        )
        raise typer.Exit(EXIT_USAGE)

    start_date, end_date = _resolve_date_args(start, end, days)
    if output is not None and output.suffix.lower() not in (".json", ".csv"):
        print_error(
            "Unsupported Format",
            f"File format '{output.suffix}' is not supported",
            hint="Supported formats: .json, .csv",
        )
        raise typer.Exit(EXIT_USAGE)

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
                title=f"Search Term Report ({start_date} to {end_date})",
                output=output,
                daily=daily,
                time_zone=ReportTimeZone.ORTZ,
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
