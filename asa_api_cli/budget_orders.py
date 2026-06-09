"""Budget order CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    enum_value,
    format_money,
    get_client,
    handle_api_error,
    output_data,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="View budget orders")

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
    """List budget orders.

    Examples:
        asa budget-orders list
        asa budget-orders list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching budget orders..."):
                result = client.budget_orders.list(limit=limit)

            if not result.data:
                print_warning("No budget orders found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": bo.id,
                    "name": bo.name or "-",
                    "status": enum_value(bo.status) if bo.status else "-",
                    "budget": format_money(bo.budget.amount, bo.budget.currency) if bo.budget else "-",
                    "order_number": bo.order_number or "-",
                    "start_date": _date_str(bo.start_date),
                    "end_date": _date_str(bo.end_date),
                }
                for bo in result.data
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

            print_result_panel(
                f"Budget order {bo.id}",
                {
                    "Name": bo.name or "-",
                    "Status": enum_value(bo.status) if bo.status else "-",
                    "Budget": format_money(bo.budget.amount, bo.budget.currency) if bo.budget else "-",
                    "Order number": bo.order_number or "-",
                    "Start date": _date_str(bo.start_date),
                    "End date": _date_str(bo.end_date),
                    "Client name": bo.client_name or "-",
                    "Billing email": bo.billing_email or "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
