"""Recommendations CLI commands for Apple Ads Platform API v1."""

from enum import StrEnum
from typing import Annotated

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.recommendations import (
    ApplyDailyCapRecommendation,
    ApplyTargetCpaRecommendation,
    DailyCapRecommendation,
    RecommendationFilterCondition,
    RecommendationFilterOperator,
    RecommendationPromotedObjectType,
    RecommendationQueryRequest,
    RecommendationState,
    TargetCpaRecommendation,
)

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    cli_state,
    enum_value,
    format_money_obj,
    format_number,
    get_client,
    handle_api_error,
    output_data,
    output_json,
    print_error,
    print_result_panel,
    spinner,
)

app = typer.Typer(help="View, apply, and dismiss Apple Ads optimization recommendations")


class RecommendationKind(StrEnum):
    """Supported recommendation categories for apply/dismiss."""

    TARGET_CPA = "target-cpa"
    DAILY_BUDGET = "daily-budget"


def _resolve_promoted_object_ids(
    client: AppleAdsClient,
    explicit_ids: list[str] | None,
    object_type: RecommendationPromotedObjectType,
) -> list[str]:
    """Resolve explicit or campaign-discovered promoted object IDs."""
    if explicit_ids:
        seen: list[str] = []
        for raw in explicit_ids:
            val = raw.strip()
            if val and val not in seen:
                seen.append(val)
        return seen

    discovered: list[str] = []
    for camp in client.campaigns.iter_all():
        if (
            enum_value(camp.promoted_object_type) == object_type.value
            and camp.promoted_object_id
            and camp.promoted_object_id not in discovered
        ):
            discovered.append(str(camp.promoted_object_id))
    return discovered


def _resolve_single_promoted_object_id(
    client: AppleAdsClient,
    explicit_id: str | None,
    object_type: RecommendationPromotedObjectType,
) -> str:
    """Resolve a single ``promotedObjectId`` for apply/dismiss commands."""
    if explicit_id:
        return explicit_id.strip()
    ids = _resolve_promoted_object_ids(client, None, object_type)
    if len(ids) == 1:
        return ids[0]
    if not ids:
        print_error(
            "Promoted Object Required",
            "Could not discover a promoted object ID from campaigns.",
            hint="Pass --promoted-object-id / --adam-id <id>.",
        )
        raise typer.Exit(EXIT_USAGE)
    print_error(
        "Multiple Promoted Objects Found",
        f"Found multiple promoted objects ({', '.join(ids)}).",
        hint="Pass --promoted-object-id / --adam-id <id> to disambiguate.",
    )
    raise typer.Exit(EXIT_USAGE)


@app.command("target-cpas")
def list_target_cpas(
    promoted_object_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--promoted-object-id",
            "--adam-id",
            "-p",
            help="App Store adamId or brand ID (auto-discovered from campaigns if omitted)",
        ),
    ] = None,
    promoted_object_type: Annotated[
        RecommendationPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = RecommendationPromotedObjectType.APPSTORE_APP,
    state: Annotated[
        RecommendationState | None,
        typer.Option("--state", "-s", help="Filter by state (AVAILABLE, APPLIED, DISMISSED, DELETE)"),
    ] = RecommendationState.AVAILABLE,
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Filter by campaign ID"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """List Target CPA recommendations for Maximize Conversions campaigns.

    Examples:
        asa recommendations target-cpas
        asa recommendations target-cpas --adam-id 123456789 --state AVAILABLE
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            with spinner("Fetching Target CPA recommendations..."):
                object_ids = _resolve_promoted_object_ids(client, promoted_object_ids, promoted_object_type)
                extra_filters: list[RecommendationFilterCondition] = []
                if state is not None:
                    extra_filters.append(
                        RecommendationFilterCondition(
                            field="state",
                            operator=RecommendationFilterOperator.EQUALS,
                            value=[state.value],
                        )
                    )
                if campaign_id is not None:
                    extra_filters.append(
                        RecommendationFilterCondition(
                            field="campaignId",
                            operator=RecommendationFilterOperator.EQUALS,
                            value=[str(campaign_id)],
                        )
                    )

                recs: list[TargetCpaRecommendation] = []
                for pid in object_ids:
                    query = RecommendationQueryRequest.for_promoted_object(
                        pid,
                        promoted_object_type,
                        filters=extra_filters or None,
                    )
                    recs.extend(client.recommendations.iter_target_cpas(query))

            rows = [
                {
                    "id": r.id or "-",
                    "campaign_id": r.campaign_id,
                    "campaign_name": r.campaign_name or "-",
                    "promoted_object_id": r.promoted_object_id or "-",
                    "state": enum_value(r.state),
                    "current_tcpa": format_money_obj(r.bid_strategy.bid_amount if r.bid_strategy else None),
                    "recommended_tcpa": format_money_obj(r.recommended_target_cpa),
                    "avg_cpa": format_money_obj(r.average_cpa),
                    "expected_installs": format_number(r.expected_installs),
                }
                for r in recs
            ]

            output_data(
                rows,
                columns=[
                    "id",
                    "campaign_id",
                    "campaign_name",
                    "state",
                    "current_tcpa",
                    "recommended_tcpa",
                    "avg_cpa",
                    "expected_installs",
                ],
                format=effective_format,
                title="Target CPA Recommendations",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("daily-budgets")
def list_daily_budgets(
    promoted_object_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--promoted-object-id",
            "--adam-id",
            "-p",
            help="App Store adamId or brand ID (auto-discovered from campaigns if omitted)",
        ),
    ] = None,
    promoted_object_type: Annotated[
        RecommendationPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = RecommendationPromotedObjectType.APPSTORE_APP,
    state: Annotated[
        RecommendationState | None,
        typer.Option("--state", "-s", help="Filter by state (AVAILABLE, APPLIED, DISMISSED, DELETE)"),
    ] = RecommendationState.AVAILABLE,
    campaign_id: Annotated[
        int | None,
        typer.Option("--campaign", "-c", help="Filter by campaign ID"),
    ] = None,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """List daily budget recommendations for budget-constrained campaigns.

    Examples:
        asa recommendations daily-budgets
        asa recommendations daily-budgets --adam-id 123456789 --campaign 999
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            with spinner("Fetching daily budget recommendations..."):
                object_ids = _resolve_promoted_object_ids(client, promoted_object_ids, promoted_object_type)
                extra_filters: list[RecommendationFilterCondition] = []
                if state is not None:
                    extra_filters.append(
                        RecommendationFilterCondition(
                            field="state",
                            operator=RecommendationFilterOperator.EQUALS,
                            value=[state.value],
                        )
                    )
                if campaign_id is not None:
                    extra_filters.append(
                        RecommendationFilterCondition(
                            field="campaignId",
                            operator=RecommendationFilterOperator.EQUALS,
                            value=[str(campaign_id)],
                        )
                    )

                recs: list[DailyCapRecommendation] = []
                for pid in object_ids:
                    query = RecommendationQueryRequest.for_promoted_object(
                        pid,
                        promoted_object_type,
                        filters=extra_filters or None,
                    )
                    recs.extend(client.recommendations.iter_daily_budgets(query))

            rows = [
                {
                    "id": r.id or "-",
                    "campaign_id": r.campaign_id,
                    "campaign_name": r.campaign_name or "-",
                    "promoted_object_id": r.promoted_object_id or "-",
                    "state": enum_value(r.state),
                    "current_budget": format_money_obj(r.daily_budget),
                    "suggested_budget": format_money_obj(r.suggested_daily_budget_amount),
                    "expected_installs": format_number(r.expected_installs),
                    "expected_spend": format_money_obj(r.expected_spend),
                }
                for r in recs
            ]

            output_data(
                rows,
                columns=[
                    "id",
                    "campaign_id",
                    "campaign_name",
                    "state",
                    "current_budget",
                    "suggested_budget",
                    "expected_installs",
                    "expected_spend",
                ],
                format=effective_format,
                title="Daily Budget Recommendations",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("apply")
def apply_recommendation(
    recommendation_id: Annotated[str, typer.Argument(help="Recommendation ID to apply")],
    kind: Annotated[
        RecommendationKind,
        typer.Option("--type", "-t", help="Recommendation kind: target-cpa or daily-budget"),
    ] = RecommendationKind.TARGET_CPA,
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--promoted-object-id", "--adam-id", "-p", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        RecommendationPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = RecommendationPromotedObjectType.APPSTORE_APP,
    amount: Annotated[
        float | None,
        typer.Option("--amount", help="Optional override amount instead of recommended value"),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Currency code when --amount is supplied (defaults to account currency)"),
    ] = None,
) -> None:
    """Apply an AVAILABLE Target CPA or daily budget recommendation.

    Examples:
        asa recommendations apply rec_123 --type target-cpa --adam-id 123456789
        asa recommendations apply rec_456 --type daily-budget --adam-id 123456789 --amount 150
    """
    client = get_client()

    try:
        with client:
            pid = _resolve_single_promoted_object_id(client, promoted_object_id, promoted_object_type)
            override_money: Money | None = None
            if amount is not None:
                resolved_currency = currency
                if not resolved_currency and client.ad_account_id is not None:
                    acct = client.ad_accounts.get(int(client.ad_account_id))
                    resolved_currency = enum_value(acct.currency) or None
                override_money = Money(amount=str(amount), currency=resolved_currency or "USD")

            with spinner(f"Applying {kind.value} recommendation {recommendation_id}..."):
                if kind == RecommendationKind.TARGET_CPA:
                    tcpa_hist = client.recommendations.apply_target_cpas(
                        [
                            ApplyTargetCpaRecommendation(
                                id=recommendation_id,
                                promoted_object_id=pid,
                                promoted_object_type=promoted_object_type,
                                applied_target_cpa=override_money,
                            )
                        ]
                    )
                    if cli_state.json_output:
                        output_json(tcpa_hist)
                        return
                    first = tcpa_hist[0] if tcpa_hist else None
                    print_result_panel(
                        "Recommendation Applied",
                        {
                            "Recommendation ID": first.recommendation_id if first else recommendation_id,
                            "Type": "Target CPA",
                            "Campaign": first.campaign_name if first and first.campaign_name else "-",
                            "State": enum_value(first.state) if first else "APPLIED",
                            "Applied Target CPA": format_money_obj(first.applied_target_cpa) if first else "-",
                        },
                    )
                else:
                    budget_hist = client.recommendations.apply_daily_budgets(
                        [
                            ApplyDailyCapRecommendation(
                                id=recommendation_id,
                                promoted_object_id=pid,
                                promoted_object_type=promoted_object_type,
                                applied_daily_budget=override_money,
                            )
                        ]
                    )
                    if cli_state.json_output:
                        output_json(budget_hist)
                        return
                    first_b = budget_hist[0] if budget_hist else None
                    print_result_panel(
                        "Recommendation Applied",
                        {
                            "Recommendation ID": first_b.recommendation_id if first_b else recommendation_id,
                            "Type": "Daily Budget",
                            "Campaign": first_b.campaign_name if first_b and first_b.campaign_name else "-",
                            "State": enum_value(first_b.state) if first_b else "APPLIED",
                            "Applied Budget": (
                                format_money_obj(first_b.applied_daily_budget_amount) if first_b else "-"
                            ),
                        },
                    )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("dismiss")
def dismiss_recommendation(
    recommendation_id: Annotated[str, typer.Argument(help="Recommendation ID to dismiss")],
    kind: Annotated[
        RecommendationKind,
        typer.Option("--type", "-t", help="Recommendation kind: target-cpa or daily-budget"),
    ] = RecommendationKind.TARGET_CPA,
    promoted_object_id: Annotated[
        str | None,
        typer.Option("--promoted-object-id", "--adam-id", "-p", help="App Store adamId or brand ID"),
    ] = None,
    promoted_object_type: Annotated[
        RecommendationPromotedObjectType,
        typer.Option("--promoted-object-type", help="APPSTORE_APP or BUSINESS_BRAND"),
    ] = RecommendationPromotedObjectType.APPSTORE_APP,
) -> None:
    """Dismiss an AVAILABLE Target CPA or daily budget recommendation.

    Examples:
        asa recommendations dismiss rec_123 --type target-cpa --adam-id 123456789
        asa recommendations dismiss rec_456 --type daily-budget --adam-id 123456789
    """
    client = get_client()

    try:
        with client:
            pid = _resolve_single_promoted_object_id(client, promoted_object_id, promoted_object_type)

            with spinner(f"Dismissing {kind.value} recommendation {recommendation_id}..."):
                if kind == RecommendationKind.TARGET_CPA:
                    tcpa_hist = client.recommendations.dismiss_target_cpas(
                        [
                            ApplyTargetCpaRecommendation(
                                id=recommendation_id,
                                promoted_object_id=pid,
                                promoted_object_type=promoted_object_type,
                            )
                        ]
                    )
                    if cli_state.json_output:
                        output_json(tcpa_hist)
                        return
                    first = tcpa_hist[0] if tcpa_hist else None
                    print_result_panel(
                        "Recommendation Dismissed",
                        {
                            "Recommendation ID": first.recommendation_id if first else recommendation_id,
                            "Type": "Target CPA",
                            "Campaign": first.campaign_name if first and first.campaign_name else "-",
                            "State": enum_value(first.state) if first else "DISMISSED",
                        },
                        style="yellow",
                    )
                else:
                    budget_hist = client.recommendations.dismiss_daily_budgets(
                        [
                            ApplyDailyCapRecommendation(
                                id=recommendation_id,
                                promoted_object_id=pid,
                                promoted_object_type=promoted_object_type,
                            )
                        ]
                    )
                    if cli_state.json_output:
                        output_json(budget_hist)
                        return
                    first_b = budget_hist[0] if budget_hist else None
                    print_result_panel(
                        "Recommendation Dismissed",
                        {
                            "Recommendation ID": first_b.recommendation_id if first_b else recommendation_id,
                            "Type": "Daily Budget",
                            "Campaign": first_b.campaign_name if first_b and first_b.campaign_name else "-",
                            "State": enum_value(first_b.state) if first_b else "DISMISSED",
                        },
                        style="yellow",
                    )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
