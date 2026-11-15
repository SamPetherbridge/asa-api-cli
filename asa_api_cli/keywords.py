"""Keyword management CLI commands (Platform API v1)."""

from typing import Annotated

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.bulk import (
    BulkKeywordCreate,
    BulkNegativeKeywordCreate,
    BulkRequestItem,
)
from asa_api_client.v1.models.keywords import (
    KeywordMatchType,
    KeywordStatus,
    KeywordUpdate,
    NegativeKeywordStatus,
)
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    ad_group_bid_money,
    campaign_currency,
    enum_value,
    format_money_obj,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    spinner,
)

app = typer.Typer(help="Manage targeting and negative keywords")
negatives_app = typer.Typer(help="Manage negative keywords")
app.add_typer(negatives_app, name="negatives")

KEYWORD_COLUMNS = ["id", "text", "match_type", "status", "bid"]
NEGATIVE_COLUMNS = ["id", "text", "match_type", "status"]


@app.command("list")
def list_keywords(
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID"),
    ] = None,
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID"),
    ] = None,
    status: Annotated[
        KeywordStatus | None,
        typer.Option("--status", "-s", help="Filter by status (ENABLED, PAUSED)"),
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
    """List targeting keywords in an ad group or campaign.

    Examples:
        asa keywords list --ad-group 456
        asa keywords list --campaign 123 --ad-group 456
        asa keywords list --campaign 123 --status ENABLED
        asa keywords list -a 456 --format json
    """
    if ad_group_id is None and campaign_id is None:
        print_error(
            "Missing Option",
            "Specify --ad-group or --campaign to query keywords",
            hint="Platform API v1 requires an adGroupId or campaignId filter on keyword queries.",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            with spinner("Fetching keywords..."):
                q = Query().page(size=limit, fetch_total_count=True)
                if ad_group_id is not None:
                    q = q.where("adGroupId", "EQUALS", ad_group_id)
                if campaign_id is not None:
                    q = q.where("campaignId", "EQUALS", campaign_id)
                if status:
                    q = q.where("status", "EQUALS", status.value)
                page = client.keywords.query(q)

            data = [
                {
                    "id": kw.id,
                    "text": kw.text or "-",
                    "match_type": enum_value(kw.match_type),
                    "status": enum_value(kw.status),
                    "bid": format_money_obj(kw.bid),
                }
                for kw in page.result
            ]

            total_count = (
                page.pagination.total_count
                if page.pagination and page.pagination.total_count is not None
                else len(page)
            )
            output_data(
                data,
                KEYWORD_COLUMNS,
                format,
                title=f"Keywords ({total_count})",
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("get")
def get_keyword(
    keyword_id: Annotated[int, typer.Argument(help="Keyword ID")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Get details for a specific keyword.

    Examples:
        asa keywords get 789
        asa keywords get 789 --format json
    """
    del campaign_id, ad_group_id
    client = get_client()

    try:
        with client:
            with spinner(f"Fetching keyword {keyword_id}..."):
                kw = client.keywords.get(keyword_id)

            if format == OutputFormat.JSON:
                from asa_api_cli.utils import output_json

                output_json(kw)
            else:
                print_result_panel(
                    f"Keyword: {kw.text}",
                    {
                        "ID": kw.id,
                        "Campaign ID": kw.campaign_id,
                        "Ad Group ID": kw.ad_group_id,
                        "Match Type": enum_value(kw.match_type),
                        "Status": enum_value(kw.status),
                        "Display Status": enum_value(kw.display_status) or "-",
                        "Bid": format_money_obj(kw.bid),
                    },
                    style="cyan",
                )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("add")
def add_keywords(
    keywords: Annotated[
        list[str],
        typer.Argument(help="Keyword text(s) to add"),
    ],
    ad_group_id: Annotated[
        int,
        typer.Option("--ad-group", "-a", help="Ad Group ID"),
    ],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    match_type: Annotated[
        KeywordMatchType,
        typer.Option("--match", "-m", help="Match type (EXACT, BROAD, PHRASE)"),
    ] = KeywordMatchType.EXACT,
    bid: Annotated[
        float | None,
        typer.Option("--bid", "-b", help="Bid amount (uses ad group default if omitted)"),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Currency code (defaults to ad group/campaign currency or USD)"),
    ] = None,
) -> None:
    """Add targeting keywords to an ad group.

    Examples:
        asa keywords add "photo editor" -a 456
        asa keywords add "photo app" "image editor" -a 456 --match BROAD --bid 1.50
    """
    client = get_client()

    try:
        with client:
            bid_money: Money | None = None
            if bid is not None:
                resolved_currency = currency.upper() if currency else None
                if not resolved_currency:
                    ag = client.ad_groups.get(ad_group_id)
                    ag_bid = ad_group_bid_money(ag)
                    if ag_bid and ag_bid.currency:
                        resolved_currency = ag_bid.currency
                    elif (cid := campaign_id or ag.campaign_id) is not None:
                        resolved_currency = campaign_currency(client.campaigns.get(cid))
                    else:
                        resolved_currency = "USD"
                bid_money = Money(amount=f"{bid:.2f}", currency=resolved_currency)

            items: list[BulkRequestItem[BulkKeywordCreate]] = [
                BulkRequestItem(
                    data=BulkKeywordCreate(
                        ad_group_id=ad_group_id,
                        text=text,
                        match_type=match_type,
                        bid=bid_money,
                        status=KeywordStatus.ENABLED,
                    )
                )
                for text in keywords
            ]

            with spinner(f"Adding {len(keywords)} keyword(s)..."):
                results = client.bulk.create_keywords(items)

            created = [r.result for r in results if r.success and r.result is not None]
            failed = [r for r in results if not r.success]

            if created:
                print_success(f"Added {len(created)} keyword(s)")
                for kw in created:
                    from asa_api_cli.utils import console

                    console.print(f"  [muted]•[/muted] {kw.text} [muted]({kw.id})[/muted]")
            if failed:
                err_obj = failed[0].error
                first_err = err_obj.message if err_obj and err_obj.message else "Bulk create error"
                print_error("Keyword Creation Failed", f"{len(failed)} item(s) failed: {first_err}")
                raise typer.Exit(EXIT_ERROR)
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("pause")
def pause_keyword(
    keyword_id: Annotated[int, typer.Argument(help="Keyword ID to pause")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
) -> None:
    """Pause a keyword.

    Examples:
        asa keywords pause 789
    """
    del campaign_id, ad_group_id
    client = get_client()

    try:
        with client:
            with spinner(f"Pausing keyword {keyword_id}..."):
                kw = client.keywords.update(keyword_id, KeywordUpdate(status=KeywordStatus.PAUSED))
            print_success(f"Keyword '{kw.text}' ({kw.id}) paused")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("enable")
def enable_keyword(
    keyword_id: Annotated[int, typer.Argument(help="Keyword ID to enable")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
) -> None:
    """Enable a paused keyword.

    Examples:
        asa keywords enable 789
    """
    del campaign_id, ad_group_id
    client = get_client()

    try:
        with client:
            with spinner(f"Enabling keyword {keyword_id}..."):
                kw = client.keywords.update(keyword_id, KeywordUpdate(status=KeywordStatus.ENABLED))
            print_success(f"Keyword '{kw.text}' ({kw.id}) enabled")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("set-bid")
def set_keyword_bid(
    keyword_id: Annotated[int, typer.Argument(help="Keyword ID")],
    bid: Annotated[float, typer.Option("--bid", "-b", help="Bid amount")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Currency code (defaults to keyword/campaign currency or USD)"),
    ] = None,
) -> None:
    """Update keyword bid amount.

    Examples:
        asa keywords set-bid 789 --bid 2.00
    """
    del campaign_id, ad_group_id
    client = get_client()

    try:
        with client:
            resolved_currency = currency.upper() if currency else None
            if not resolved_currency:
                existing = client.keywords.get(keyword_id)
                if existing.bid and existing.bid.currency:
                    resolved_currency = existing.bid.currency
                elif existing.campaign_id is not None:
                    resolved_currency = campaign_currency(client.campaigns.get(existing.campaign_id))
                else:
                    resolved_currency = "USD"

            with spinner(f"Updating bid for keyword {keyword_id}..."):
                kw = client.keywords.update(
                    keyword_id,
                    KeywordUpdate(bid=Money(amount=f"{bid:.2f}", currency=resolved_currency)),
                )
            print_result_panel(
                f"Bid Updated: {kw.text}",
                {
                    "Bid": format_money_obj(kw.bid),
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("delete")
def delete_keyword(
    keyword_id: Annotated[int, typer.Argument(help="Keyword ID to delete")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete a keyword.

    Examples:
        asa keywords delete 789
        asa keywords delete 789 --force
    """
    del campaign_id, ad_group_id
    if not force:
        confirm = typer.confirm(f"Delete keyword {keyword_id}?")
        if not confirm:
            print_info("Cancelled")
            raise typer.Exit(0)

    client = get_client()

    try:
        with client:
            with spinner(f"Deleting keyword {keyword_id}..."):
                client.keywords.delete(keyword_id)
            print_success(f"Keyword {keyword_id} deleted")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


# Negative keyword subcommands
@negatives_app.command("list")
def list_negative_keywords(
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (required for campaign-level negatives)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (for ad-group-level negatives)"),
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
    """List negative keywords at campaign or ad group level.

    Examples:
        asa keywords negatives list --campaign 123
        asa keywords negatives list --ad-group 456
        asa keywords negatives list -c 123 -a 456
    """
    if campaign_id is None and ad_group_id is None:
        print_error(
            "Missing Option",
            "Specify --campaign (campaign-level) or --ad-group (ad-group-level)",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            level = f"ad group {ad_group_id}" if ad_group_id else f"campaign {campaign_id}"
            with spinner(f"Fetching negative keywords for {level}..."):
                q = Query().page(size=limit, fetch_total_count=True)
                if ad_group_id is not None:
                    q = q.where("adGroupId", "EQUALS", ad_group_id)
                    if campaign_id is not None:
                        q = q.where("campaignId", "EQUALS", campaign_id)
                else:
                    # Live Platform API v1 requires an adGroupId filter on every negative-keywords query;
                    # IS_NULL scopes to campaign-level negative keywords.
                    assert campaign_id is not None
                    q = q.where("adGroupId", "IS_NULL").where("campaignId", "EQUALS", campaign_id)

                page = client.negative_keywords.query(q)

            data = [
                {
                    "id": nk.id,
                    "text": nk.text or "-",
                    "match_type": enum_value(nk.match_type),
                    "status": enum_value(nk.status),
                }
                for nk in page.result
            ]

            total_count = (
                page.pagination.total_count
                if page.pagination and page.pagination.total_count is not None
                else len(page)
            )
            output_data(
                data,
                NEGATIVE_COLUMNS,
                format,
                title=f"Negative Keywords ({total_count})",
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@negatives_app.command("add")
def add_negative_keywords(
    keywords: Annotated[
        list[str],
        typer.Argument(help="Negative keyword text(s) to add"),
    ],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (omit for campaign-level)"),
    ] = None,
    match_type: Annotated[
        KeywordMatchType,
        typer.Option("--match", "-m", help="Match type (EXACT, BROAD, PHRASE)"),
    ] = KeywordMatchType.EXACT,
) -> None:
    """Add negative keywords at campaign or ad group level.

    Examples:
        asa keywords negatives add "free" "cheap" -c 123
        asa keywords negatives add "competitor" -a 456
    """
    if campaign_id is None and ad_group_id is None:
        print_error(
            "Missing Option",
            "Specify --campaign (campaign-level) or --ad-group (ad-group-level)",
        )
        raise typer.Exit(EXIT_USAGE)

    client = get_client()

    try:
        with client:
            resolved_campaign_id = campaign_id
            if ad_group_id is not None and resolved_campaign_id is None:
                ag = client.ad_groups.get(ad_group_id)
                resolved_campaign_id = ag.campaign_id

            items: list[BulkRequestItem[BulkNegativeKeywordCreate]] = [
                BulkRequestItem(
                    data=BulkNegativeKeywordCreate(
                        campaign_id=resolved_campaign_id,
                        ad_group_id=ad_group_id,
                        text=text,
                        match_type=match_type,
                        status=NegativeKeywordStatus.ENABLED,
                    )
                )
                for text in keywords
            ]

            level = f"ad group {ad_group_id}" if ad_group_id else f"campaign {resolved_campaign_id}"
            with spinner(f"Adding {len(keywords)} negative keyword(s) to {level}..."):
                results = client.bulk.create_negative_keywords(items)

            created = [r.result for r in results if r.success and r.result is not None]
            failed = [r for r in results if not r.success]

            if created:
                print_success(f"Added {len(created)} negative keyword(s) to {level}")
                for nk in created:
                    from asa_api_cli.utils import console

                    console.print(f"  [muted]•[/muted] {nk.text} [muted]({nk.id})[/muted]")
            if failed:
                err_obj = failed[0].error
                first_err = err_obj.message if err_obj and err_obj.message else "Bulk create error"
                print_error("Negative Keyword Creation Failed", f"{len(failed)} item(s) failed: {first_err}")
                raise typer.Exit(EXIT_ERROR)
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@negatives_app.command("delete")
def delete_negative_keyword(
    keyword_id: Annotated[int, typer.Argument(help="Negative keyword ID to delete")],
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Campaign ID (optional in Platform API v1)"),
    ] = None,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Ad Group ID (optional in Platform API v1)"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete a negative keyword.

    Examples:
        asa keywords negatives delete 789
        asa keywords negatives delete 789 --force
    """
    del campaign_id, ad_group_id
    if not force:
        confirm = typer.confirm(f"Delete negative keyword {keyword_id}?")
        if not confirm:
            print_info("Cancelled")
            raise typer.Exit(0)

    client = get_client()

    try:
        with client:
            with spinner(f"Deleting negative keyword {keyword_id}..."):
                client.negative_keywords.delete(keyword_id)
            print_success(f"Negative keyword {keyword_id} deleted")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
