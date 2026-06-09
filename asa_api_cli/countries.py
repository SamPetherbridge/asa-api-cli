"""Supported country/region reference CLI commands."""

from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.models import LanguageDetail

from asa_api_cli.utils import (
    EXIT_ERROR,
    OutputFormat,
    get_client,
    handle_api_error,
    output_data,
    print_warning,
    spinner,
)

app = typer.Typer(help="List supported countries and regions")

COUNTRY_COLUMNS = ["country_or_region", "default_language", "supported_languages"]


def _language_label(detail: LanguageDetail | None) -> str:
    """Render a LanguageDetail as a short label, e.g. "English (en)"."""
    if detail is None:
        return "-"
    if detail.language and detail.language_code:
        return f"{detail.language} ({detail.language_code})"
    return detail.language or detail.language_code or "-"


@app.command("list")
def list_countries(
    codes: Annotated[
        list[str] | None,
        typer.Option("--code", "-c", help="Filter by ISO alpha-2 code (repeatable, e.g. -c US -c GB)"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List countries and regions supported for advertising.

    Shows each region's default and supported ad languages.

    Examples:
        asa countries list
        asa countries list --code US --code GB
        asa countries list --format json
    """
    client = get_client()

    try:
        with client:
            with spinner("Fetching supported countries..."):
                results = client.countries_or_regions.list(
                    countries_or_regions=[c.upper() for c in codes] if codes else None,
                )

            if not results:
                print_warning("No countries found")
                return

            rows: list[dict[str, Any]] = [
                {
                    "country_or_region": cr.country_or_region,
                    "default_language": _language_label(cr.default_language),
                    "supported_languages": ", ".join(_language_label(lang) for lang in cr.supported_languages or [])
                    or "-",
                }
                for cr in results
            ]

            output_data(
                rows,
                COUNTRY_COLUMNS,
                format,
                title="Supported countries / regions",
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
