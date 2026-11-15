"""Suggestions CLI commands for Apple Ads Platform API v1."""

from typing import Annotated

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.suggestions import (
    SuggestionPromotedObjectType,
    SuggestionQueryType,
)
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    cli_state,
    enum_value,
    format_money_obj,
    get_client,
    handle_api_error,
    output_data,
    output_json,
    print_error,
    print_result_panel,
    spinner,
)

app = typer.Typer(help="Query keyword, phrase, category, and Target CPA suggestions")


def _resolve_promoted_object_id(
    client: AppleAdsClient,
    explicit_id: str | None,
    object_type: SuggestionPromotedObjectType,
) -> str:
    """Resolve explicit or single campaign-discovered promoted object ID."""
    if explicit_id:
        return explicit_id.strip()

    discovered: list[str] = []
    for camp in client.campaigns.iter_all():
        if (
            enum_value(camp.promoted_object_type) == object_type.value
            and camp.promoted_object_id
            and camp.promoted_object_id not in discovered
        ):
            discovered.append(str(camp.promoted_object_id))

    if len(discovered) == 1:
        return discovered[0]
    if not discovered:
        print_error(
            "Promoted Object Required",
            "Pass --adam-id / --promoted-object-id to query suggestions.",
            hint="Example: asa suggestions keywords --adam-id 123456789",
        )
        raise typer.Exit(EXIT_USAGE)
    print_error(
        "Multiple Promoted Objects Found",
        f"Found multiple promoted objects ({', '.join(discovered)}).",
        hint="Pass --adam-id / --promoted-object-id <id> to select one.",
    )
    raise typer.Exit(EXIT_USAGE)


@app.command("keywords")
def suggest_keywords(
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--adam-id", "--promoted-object-id", "-a", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        SuggestionPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = SuggestionPromotedObjectType.APPSTORE_APP,
    terms: Annotated[
        list[str] | None,
        typer.Option("--term", "-t", help="Seed keyword term(s) to expand"),
    ] = None,
    countries: Annotated[
        list[str] | None,
        typer.Option("--country", "-c", help="Filter by country/region code(s) (e.g. US, AU)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum suggestions to return"),
    ] = 25,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Query keyword suggestions for an app.

    Examples:
        asa suggestions keywords --adam-id 123456789
        asa suggestions keywords -a 123456789 --term "calculator" --country US
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            pid = _resolve_promoted_object_id(client, promoted_object_id, promoted_object_type)
            query = (
                Query()
                .where("promotedObjectId", "EQUALS", [pid])
                .where("promotedObjectType", "EQUALS", [promoted_object_type.value])
                .page(size=limit)
            )
            if terms:
                query = query.where("terms", "IN", terms)
            if countries:
                query = query.where("countriesOrRegions", "IN", [c.upper() for c in countries])

            with spinner("Fetching keyword suggestions..."):
                page = client.suggestions.query_keywords(query)

            rows = [
                {
                    "keyword": item.text or "-",
                    "popularity": item.popularity,
                }
                for item in page.result
            ]

            output_data(
                rows,
                columns=["keyword", "popularity"],
                format=effective_format,
                title=f"Keyword Suggestions (Promoted Object {pid})",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("phrases")
def suggest_phrases(
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--adam-id", "--promoted-object-id", "-a", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        SuggestionPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = SuggestionPromotedObjectType.APPSTORE_APP,
    query_type: Annotated[
        SuggestionQueryType,
        typer.Option("--query-type", help="SUGGESTION or SEARCH"),
    ] = SuggestionQueryType.SUGGESTION,
    phrases: Annotated[
        list[str] | None,
        typer.Option("--phrase", "-p", help="Phrase(s) to look up when --query-type SEARCH"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum suggestions to return"),
    ] = 25,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Query phrase suggestions for an app or brand.

    Examples:
        asa suggestions phrases --adam-id 123456789
        asa suggestions phrases -a 123456789 --query-type SEARCH --phrase "fraction calculator"
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            pid = _resolve_promoted_object_id(client, promoted_object_id, promoted_object_type)
            query = (
                Query()
                .where("promotedObjectId", "EQUALS", [pid])
                .where("promotedObjectType", "EQUALS", [promoted_object_type.value])
                .where("queryType", "EQUALS", [query_type.value])
                .page(size=limit)
            )
            if phrases:
                query = query.where("phrases", "IN", phrases)

            with spinner("Fetching phrase suggestions..."):
                page = client.suggestions.query_phrases(query)

            rows = [
                {
                    "phrase": item.phrase or "-",
                    "popularity": item.popularity,
                }
                for item in page.result
            ]

            output_data(
                rows,
                columns=["phrase", "popularity"],
                format=effective_format,
                title=f"Phrase Suggestions (Promoted Object {pid})",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("categories")
def suggest_categories(
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--adam-id", "--promoted-object-id", "-a", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        SuggestionPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = SuggestionPromotedObjectType.APPSTORE_APP,
    query_type: Annotated[
        SuggestionQueryType,
        typer.Option("--query-type", help="SUGGESTION or SEARCH"),
    ] = SuggestionQueryType.SUGGESTION,
    categories: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Category name(s) or %pattern% when --query-type SEARCH"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum categories to return"),
    ] = 25,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Query category suggestions or search categories by name.

    Examples:
        asa suggestions categories --adam-id 123456789
        asa suggestions categories --query-type SEARCH --category "%Tools%"
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            query = Query().where("queryType", "EQUALS", [query_type.value]).page(size=limit)
            if query_type == SuggestionQueryType.SUGGESTION or promoted_object_id:
                pid = _resolve_promoted_object_id(client, promoted_object_id, promoted_object_type)
                query = query.where("promotedObjectId", "EQUALS", [pid]).where(
                    "promotedObjectType", "EQUALS", [promoted_object_type.value]
                )
            if categories:
                op = "LIKE" if len(categories) == 1 and "%" in categories[0] else "IN"
                query = query.where("category", op, categories)

            with spinner("Fetching category suggestions..."):
                page = client.suggestions.query_categories(query)

            rows = [
                {
                    "category": item.category or "-",
                    "popularity": item.popularity,
                }
                for item in page.result
            ]

            output_data(
                rows,
                columns=["category", "popularity"],
                format=effective_format,
                title="Category Suggestions",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("target-cpa")
def suggest_target_cpa(
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--adam-id", "--promoted-object-id", "-a", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        SuggestionPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = SuggestionPromotedObjectType.APPSTORE_APP,
    countries: Annotated[
        list[str] | None,
        typer.Option("--country", "-c", help="Scope suggestion to specific country/region code(s)"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Get Apple's suggested Target CPA for Maximize Conversions campaigns.

    Examples:
        asa suggestions target-cpa --adam-id 123456789
        asa suggestions target-cpa -a 123456789 --country US --country GB
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            pid = _resolve_promoted_object_id(client, promoted_object_id, promoted_object_type)
            query = (
                Query()
                .where("promotedObjectId", "EQUALS", [pid])
                .where("promotedObjectType", "EQUALS", [promoted_object_type.value])
            )
            if countries:
                query = query.where("countryOrRegion", "IN", [c.upper() for c in countries])

            with spinner("Fetching Target CPA suggestion..."):
                suggestion = client.suggestions.query_target_cpa(query)

            if effective_format == OutputFormat.JSON:
                output_json(suggestion)
                return

            countries_str = ", ".join(suggestion.country_or_region) if suggestion.country_or_region else "-"
            if effective_format == OutputFormat.CSV:
                output_data(
                    [
                        {
                            "promoted_object_id": suggestion.promoted_object_id or pid,
                            "app_category": suggestion.app_category or "-",
                            "countries": countries_str,
                            "suggested_target_cpa": format_money_obj(suggestion.suggested_target_cpa),
                        }
                    ],
                    columns=["promoted_object_id", "app_category", "countries", "suggested_target_cpa"],
                    format=OutputFormat.CSV,
                )
                return

            print_result_panel(
                "Target CPA Suggestion",
                {
                    "Promoted Object ID": suggestion.promoted_object_id or pid,
                    "App Category": suggestion.app_category or "-",
                    "Countries/Regions": countries_str,
                    "Suggested Target CPA": format_money_obj(suggestion.suggested_target_cpa),
                },
                style="cyan",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
