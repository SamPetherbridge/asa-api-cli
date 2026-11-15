"""Budget order (shared budget) CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    enum_value,
    format_money_obj,
    get_client,
    handle_api_error,
    output_data,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="View budget orders (shared budgets)")

BUDGET_ORDER_COLUMNS = ["id", "name", "status", "budget", "order_number", "start_date", "end_date"]


def _date_str(value: object) -> str:
    """Render a datetime/date-ish value as an ISO date, or '-'."""
    if value is None:
        return "-"
    return str(value).split("T")[0].split(" ")[0]


@app.command("list")
def list_budget_orders(
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List budget orders (shared budgets).

    Examples:
        asa budget-orders list
        asa budget-orders list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching budget orders..."):
                page = client.budget_orders.query(Query().page(size=limit, fetch_total_count=True))

            if not page.result:
                print_warning("No budget orders found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": bo.id,
                    "name": bo.name or "-",
                    "status": enum_value(bo.system_status) or "-",
                    "budget": format_money_obj(bo.value),
                    "order_number": (
                        bo.invoice_detail.order_number if bo.invoice_detail and bo.invoice_detail.order_number else "-"
                    ),
                    "start_date": _date_str(bo.start_time),
                    "end_date": _date_str(bo.end_time),
                }
                for bo in page.result
            ]

            output_data(rows, BUDGET_ORDER_COLUMNS, format, title="Budget orders")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_budget_order(
    budget_order_id: Annotated[
        int,
        typer.Argument(help="Budget order ID"),
    ],
) -> None:
    """Show details for a single budget order.

    Example:
        asa budget-orders get 123456
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching budget order..."):
                bo = client.budget_orders.get(budget_order_id)

            reasons = ", ".join(enum_value(r) for r in bo.system_status_reasons) if bo.system_status_reasons else "-"
            accounts = ", ".join(str(aid) for aid in bo.ad_account_ids) if bo.ad_account_ids else "-"
            inv = bo.invoice_detail

            print_result_panel(
                f"Budget order {bo.id}",
                {
                    "Name": bo.name or "-",
                    "Status": enum_value(bo.system_status) or "-",
                    "Status reasons": reasons,
                    "Budget": format_money_obj(bo.value),
                    "Ad accounts": accounts,
                    "Order number": inv.order_number if inv and inv.order_number else "-",
                    "Start date": _date_str(bo.start_time),
                    "End date": _date_str(bo.end_time),
                    "Client name": inv.client_name if inv and inv.client_name else "-",
                    "Billing email": inv.billing_email if inv and inv.billing_email else "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
