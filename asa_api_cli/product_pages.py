"""Custom and default product page CLI commands (Platform API v1)."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="View App Store product pages (DPPs, CPPs, and PPO variants)")

PAGE_COLUMNS = ["id", "name", "state", "adam_id", "deep_link"]
LOCALE_COLUMNS = ["language_code", "language", "app_name", "sub_title", "promotional_text", "device_classes"]


@app.command("list")
def list_product_pages(
    adam_id: Annotated[
        int,
        typer.Option("--adam-id", "-a", help="App Store app ID (Adam ID)"),
    ],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List product pages configured in App Store Connect for an app.

    Examples:
        asa product-pages list --adam-id 123456789
        asa product-pages list -a 123456789 --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching product pages..."):
                page = client.product_pages.query(
                    Query().where("adamId", "EQUALS", adam_id).page(size=limit, fetch_total_count=True)
                )

            if not page.result:
                print_warning("No product pages found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "id": pp.id,
                    "name": pp.name or "-",
                    "state": enum_value(pp.state) if pp.state else "-",
                    "adam_id": pp.adam_id or "-",
                    "deep_link": pp.deep_link or "-",
                }
                for pp in page.result
            ]

            output_data(rows, PAGE_COLUMNS, format, title="Product pages")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_product_page(
    product_page_id: Annotated[
        str,
        typer.Argument(help="Product page ID (UUID)"),
    ],
    adam_id: Annotated[
        int | None,
        typer.Option("--adam-id", "-a", help="App Store app ID (optional in Platform API v1)"),
    ] = None,
) -> None:
    """Show details for a single product page.

    Example:
        asa product-pages get 45812c9b-c296-43d3-c6a0-c5a02f74bf6e
    """
    del adam_id
    client = get_client()

    try:
        with client:
            with spinner("Fetching product page..."):
                pp = client.product_pages.get(product_page_id)

            print_result_panel(
                f"Product page {pp.id}",
                {
                    "Name": pp.name or "-",
                    "State": enum_value(pp.state) if pp.state else "-",
                    "Adam ID": str(pp.adam_id) if pp.adam_id else "-",
                    "Deep link": pp.deep_link or "-",
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("locales")
def list_product_page_locales(
    product_page_id: Annotated[
        str,
        typer.Argument(help="Product page ID (UUID)"),
    ],
    adam_id: Annotated[
        int | None,
        typer.Option("--adam-id", "-a", help="App Store app ID (optional in Platform API v1)"),
    ] = None,
    language_code: Annotated[
        str | None,
        typer.Option("--locale", help="Filter by BCP-47 language code (e.g. en-US)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum number of results"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List localized metadata and assets for a product page.

    Examples:
        asa product-pages locales 45812c9b-c296-43d3-c6a0-c5a02f74bf6e
        asa product-pages locales 45812c9b-... --format json
    """
    del adam_id
    client = get_client()

    try:
        with client:
            with spinner("Fetching product page locales..."):
                q = Query().where("productPageId", "EQUALS", product_page_id).page(size=limit)
                if language_code:
                    q = q.where("languageCode", "EQUALS", language_code)
                page = client.product_pages.query_locale_details(q)

            if not page.result:
                print_warning("No locales found for product page")
                return

            rows: list[dict[str, Any]] = [
                {
                    "language_code": loc.language_code or "-",
                    "language": loc.language or "-",
                    "app_name": loc.app_name or "-",
                    "sub_title": loc.sub_title or "-",
                    "promotional_text": loc.promotional_text or "-",
                    "device_classes": ", ".join(enum_value(dc) for dc in (loc.device_classes or [])) or "-",
                }
                for loc in page.result
            ]

            output_data(rows, LOCALE_COLUMNS, format, title=f"Locales for {product_page_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("app-locales")
def list_app_locales(
    adam_id_arg: Annotated[
        int | None,
        typer.Argument(help="App Store app ID (Adam ID)"),
    ] = None,
    adam_id_opt: Annotated[
        int | None,
        typer.Option("--adam-id", "-a", help="App Store app ID (Adam ID)"),
    ] = None,
    language_code: Annotated[
        str | None,
        typer.Option("--locale", help="Filter by BCP-47 language code (e.g. en-US)"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List localized metadata for an app's Default Product Page.

    Examples:
        asa product-pages app-locales 123456789
        asa product-pages app-locales --adam-id 123456789
    """
    adam_id = adam_id_arg if adam_id_arg is not None else adam_id_opt
    if adam_id is None:
        print_error("Missing Adam ID", "Provide an App Store Adam ID (e.g. 'asa product-pages app-locales 123456789').")
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            with spinner("Fetching Default Product Page locales..."):
                q = Query().where("languageCode", "EQUALS", language_code) if language_code else None
                locales = list(client.product_pages.iter_app_locale_details(adam_id, q))

            if not locales:
                print_warning("No Default Product Page locales found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "language_code": loc.language_code or "-",
                    "language": loc.language or "-",
                    "app_name": loc.app_name or "-",
                    "sub_title": loc.sub_title or "-",
                    "promotional_text": loc.promotional_text or "-",
                    "device_classes": ", ".join(enum_value(dc) for dc in (loc.device_classes or [])) or "-",
                }
                for loc in locales
            ]

            output_data(rows, LOCALE_COLUMNS, format, title=f"Default Product Page locales for {adam_id}")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
