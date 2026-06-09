"""Custom Product Page (CPP) read-only CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    get_client,
    handle_api_error,
    output_data,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="View custom product pages (CPP)")

PRODUCT_PAGE_COLUMNS = ["id", "name", "adam_id", "state", "deep_link"]
LOCALE_COLUMNS = ["language_code", "language", "app_name", "device_classes"]


@app.command("list")
def list_product_pages(
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List custom product pages.

    Examples:
        asa product-pages list
        asa product-pages list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching product pages..."):
                result = client.product_pages.list(limit=limit)

            if not result.data:
                print_warning("No product pages found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": pp.id,
                    "name": pp.name or "-",
                    "adam_id": pp.adam_id or "-",
                    "state": pp.state or "-",
                    "deep_link": pp.deep_link or "-",
                }
                for pp in result.data
            ]

            output_data(rows, PRODUCT_PAGE_COLUMNS, format, title="Custom product pages")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_product_page(
    product_page_id: Annotated[
        str,
        typer.Argument(help="Product page ID"),
    ],
) -> None:
    """Show details for a single custom product page.

    Example:
        asa product-pages get <product-page-id>
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching product page..."):
                pp = client.product_pages.get(product_page_id)

            print_result_panel(
                f"Product page {pp.id}",
                {
                    "Name": pp.name or "-",
                    "adam ID": str(pp.adam_id) if pp.adam_id else "-",
                    "State": pp.state or "-",
                    "Deep link": pp.deep_link or "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("locales")
def product_page_locales(
    product_page_id: Annotated[
        str,
        typer.Argument(help="Product page ID"),
    ],
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List the per-locale details of a custom product page.

    Example:
        asa product-pages locales <product-page-id>
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching locale details..."):
                details = client.product_pages.get_locale_details(product_page_id)

            if not details:
                print_warning("No locale details found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "language_code": detail.language_code or "-",
                    "language": detail.language or "-",
                    "app_name": detail.app_name or "-",
                    "device_classes": ", ".join(detail.device_classes or []) or "-",
                }
                for detail in details
            ]

            output_data(rows, LOCALE_COLUMNS, format, title=f"Locales for {product_page_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
