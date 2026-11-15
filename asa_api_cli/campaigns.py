"""Campaign management CLI commands (Platform API v1)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.campaigns import (
    CampaignStatus,
    CampaignUpdate,
    DailyBudget,
)
from asa_api_client.v1.models.reports import (
    AppsReportingRequest,
    ReportFilter,
    ReportFilterOperator,
    ReportGranularity,
    ReportTimeRange,
)
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    campaign_countries,
    campaign_currency,
    campaign_daily_budget,
    enum_value,
    format_bid_strategy,
    format_money,
    format_money_obj,
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

app = typer.Typer(help="Manage campaigns")

CAMPAIGN_COLUMNS = ["id", "name", "status", "serving_status", "daily_budget", "countries"]


@app.command("list")
def list_campaigns(
    status: Annotated[
        CampaignStatus | None,
        typer.Option("--status", "-s", help="Filter by status (ENABLED, PAUSED)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    with_spend: Annotated[
        bool,
        typer.Option("--with-spend", help="Include spend metrics"),
    ] = False,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Days of spend data (with --with-spend)"),
    ] = 30,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List all campaigns.

    Examples:
        asa campaigns list
        asa campaigns list --status ENABLED
        asa campaigns list --with-spend --days 30
        asa campaigns list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching campaigns..."):
                q = Query().page(size=limit, fetch_total_count=True)
                if status:
                    q = q.where("status", "EQUALS", status.value)
                page = client.campaigns.query(q)

            if not page.result:
                if format == OutputFormat.JSON:
                    output_data([], CAMPAIGN_COLUMNS, format)
                else:
                    print_warning("No campaigns found")
                return

            spend_by_campaign: dict[int, tuple[Decimal, str]] = {}
            if with_spend:
                end_date = date.today() - timedelta(days=1)
                start_date = end_date - timedelta(days=days - 1)
                cids = [str(c.id) for c in page.result if c.id is not None]

                with spinner(f"Fetching {days}-day spend data..."):
                    try:
                        req = AppsReportingRequest(
                            filters=(
                                [ReportFilter(field="id", operator=ReportFilterOperator.IN, value=cids)]
                                if cids
                                else None
                            ),
                            time_range=ReportTimeRange(
                                start=start_date,
                                end=end_date,
                                granularity=ReportGranularity.DAILY,
                            ),
                        )
                        for row in client.reports.iter_campaigns(req):
                            if row.metadata and row.metadata.id is not None and row.total_metrics:
                                ls = row.total_metrics.local_spend
                                if ls and ls.amount is not None:
                                    cid = row.metadata.id
                                    default_spend = (Decimal("0"), ls.currency or "USD")
                                    prev_amt, prev_curr = spend_by_campaign.get(cid, default_spend)
                                    spend_by_campaign[cid] = (
                                        prev_amt + Decimal(str(ls.amount)),
                                        ls.currency or prev_curr,
                                    )
                    except AppleSearchAdsError:
                        print_warning("Could not fetch spend data")

            data = []
            for c in page.result:
                countries = campaign_countries(c)
                budget = campaign_daily_budget(c)
                serving = enum_value(c.display_status) or enum_value(c.system_status)
                row_dict = {
                    "id": c.id,
                    "name": c.name or "-",
                    "status": enum_value(c.status),
                    "serving_status": serving,
                    "daily_budget": format_money_obj(budget),
                    "countries": ", ".join(countries[:3]) + ("..." if len(countries) > 3 else ""),
                }
                if with_spend:
                    if c.id is not None and c.id in spend_by_campaign:
                        amt, curr = spend_by_campaign[c.id]
                        row_dict["spend"] = format_money(str(amt), curr)
                    else:
                        row_dict["spend"] = "-"
                data.append(row_dict)

            columns = [*CAMPAIGN_COLUMNS, "spend"] if with_spend else CAMPAIGN_COLUMNS
            has_total = page.pagination and page.pagination.total_count is not None
            total_count = page.pagination.total_count if page.pagination and has_total else len(page)
            title = f"Campaigns ({total_count})" + (f" - {days}d spend" if with_spend else "")

            output_data(
                data,
                columns,
                format,
                title=title,
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_campaign(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Get details for a specific campaign.

    Examples:
        asa campaigns get 123456789
        asa campaigns get 123456789 --format json
    """
    client = get_client()

    try:
        with client:
            with spinner(f"Fetching campaign {campaign_id}..."):
                c = client.campaigns.get(campaign_id)

            if format == OutputFormat.JSON:
                from asa_api_cli.utils import output_json

                output_json(c)
            else:
                countries = campaign_countries(c)
                budget = campaign_daily_budget(c)
                supply_sources: list[str] = []
                supply_placements: list[str] = []
                if c.targeting:
                    if c.targeting.supply_source and c.targeting.supply_source.include:
                        supply_sources = [enum_value(s) for s in c.targeting.supply_source.include]
                    if c.targeting.supply_placement and c.targeting.supply_placement.include:
                        supply_placements = [enum_value(p) for p in c.targeting.supply_placement.include]

                promoted = "-"
                if c.promoted_object_id:
                    ptype = enum_value(c.promoted_object_type)
                    promoted = f"{c.promoted_object_id} ({ptype})" if ptype else c.promoted_object_id

                shared_budget_ids = (
                    ", ".join(str(sb.budget_id) for sb in c.shared_budgets if sb.budget_id is not None)
                    if c.shared_budgets
                    else "-"
                )

                print_result_panel(
                    f"Campaign: {c.name}",
                    {
                        "ID": c.id,
                        "Status": enum_value(c.status),
                        "Display Status": enum_value(c.display_status) or "-",
                        "System Status": enum_value(c.system_status) or "-",
                        "Promoted Object": promoted,
                        "Billing Event": enum_value(c.billing_event) or "-",
                        "Daily Budget": format_money_obj(budget),
                        "Shared Budgets": shared_budget_ids or "-",
                        "Bid Strategy": format_bid_strategy(c.bid_strategy),
                        "Countries": ", ".join(countries) if countries else "-",
                        "Supply Sources": ", ".join(supply_sources) if supply_sources else "-",
                        "Placements": ", ".join(supply_placements) if supply_placements else "-",
                    },
                    style="cyan",
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("pause")
def pause_campaign(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID to pause")],
) -> None:
    """Pause an active campaign.

    Examples:
        asa campaigns pause 123456789
    """
    client = get_client()

    try:
        with client:
            with spinner(f"Pausing campaign {campaign_id}..."):
                c = client.campaigns.update(
                    campaign_id,
                    CampaignUpdate(status=CampaignStatus.PAUSED),
                )
            print_success(f"Campaign '{c.name}' ({c.id}) paused")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("enable")
def enable_campaign(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID to enable")],
) -> None:
    """Enable a paused campaign.

    Examples:
        asa campaigns enable 123456789
    """
    client = get_client()

    try:
        with client:
            with spinner(f"Enabling campaign {campaign_id}..."):
                c = client.campaigns.update(
                    campaign_id,
                    CampaignUpdate(status=CampaignStatus.ENABLED),
                )
            print_success(f"Campaign '{c.name}' ({c.id}) enabled")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("set-budget")
def set_budget(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID")],
    daily: Annotated[
        float | None,
        typer.Option("--daily", "-d", help="Daily budget amount"),
    ] = None,
    total: Annotated[
        float | None,
        typer.Option(
            "--total",
            "-t",
            help="[Removed in Platform API v1] Lifetime campaign budgets are no longer supported",
        ),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", "-c", help="Currency code (defaults to campaign currency or USD)"),
    ] = None,
) -> None:
    """Update campaign daily budget.

    Examples:
        asa campaigns set-budget 123456789 --daily 50.00
        asa campaigns set-budget 123456789 --daily 100 --currency EUR
    """
    if total is not None:
        print_error(
            "Unsupported Option",
            "Platform API v1 removed campaign-level lifetime budgets (--total).",
            hint="Use --daily to set a daily budget, or inspect shared budgets via 'asa budget-orders'.",
        )
        raise typer.Exit(EXIT_USAGE)

    if daily is None:
        print_error(
            "Missing Option",
            "Specify --daily budget amount",
            hint="Example: asa campaigns set-budget 123 --daily 50.00",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            resolved_currency = currency.upper() if currency else None
            if not resolved_currency:
                existing = client.campaigns.get(campaign_id)
                resolved_currency = campaign_currency(existing)

            update = CampaignUpdate(
                daily_budget=DailyBudget(value=Money(amount=f"{daily:.2f}", currency=resolved_currency))
            )
            with spinner(f"Updating budget for campaign {campaign_id}..."):
                c = client.campaigns.update(campaign_id, update)

            budget = campaign_daily_budget(c)
            print_result_panel(
                f"Budget Updated: {c.name}",
                {
                    "Daily Budget": format_money_obj(budget),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_campaign(
    campaign_id: Annotated[int, typer.Argument(help="Campaign ID to delete")],
    force: Annotated[
        bool,
        typer.Option("--force", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete a campaign permanently.

    Examples:
        asa campaigns delete 123456789
        asa campaigns delete 123456789 --force
    """
    client = get_client()

    try:
        with client:
            c = client.campaigns.get(campaign_id)

            if not force:
                confirm = typer.confirm(f"Delete campaign '{c.name}' ({c.id})? This cannot be undone")
                if not confirm:
                    print_info("Cancelled")
                    raise typer.Exit(0)

            with spinner(f"Deleting campaign {campaign_id}..."):
                client.campaigns.delete(campaign_id)

            print_success(f"Campaign '{c.name}' ({c.id}) deleted")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
