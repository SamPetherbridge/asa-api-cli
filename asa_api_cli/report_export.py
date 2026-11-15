"""Reliable, machine-readable per-app reporting export for Apple Ads Platform API v1."""

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from asa_api_client.cli.dates import MAX_DAILY_WINDOW, SEARCH_TERM_LOOKBACK, chunk_windows
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.ad_accounts import AdAccount
from asa_api_client.v1.models.campaigns import Campaign
from asa_api_client.v1.models.reports import (
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
from asa_api_client.v1.query import Query

from asa_api_cli.utils import campaign_adam_id, campaign_currency, enum_value

EXPORT_SCHEMA_VERSION = "1.0"
DEFAULT_REPORT_PAGE_SIZE = 5000
DEFAULT_CAMPAIGN_PAGE_SIZE = 500

LEVEL_ORDER: tuple[str, ...] = (
    "campaigns",
    "ad_groups",
    "ads",
    "keywords",
    "search_terms",
)

ADDITIVE_INT_FIELDS: tuple[str, ...] = (
    "impressions",
    "taps",
    "tap_installs",
    "tap_new_downloads",
    "tap_redownloads",
    "tap_pre_orders_placed",
    "view_installs",
    "view_new_downloads",
    "view_redownloads",
    "view_pre_orders_placed",
    "total_installs",
    "total_new_downloads",
    "total_redownloads",
    "total_pre_orders_placed",
)

RECONCILE_INT_FIELDS: tuple[str, ...] = (
    "impressions",
    "taps",
    "tap_installs",
    "view_installs",
    "total_installs",
)

TIMEZONE_POLICY: dict[str, str] = {
    "campaigns": "selectable",
    "ad_groups": "selectable",
    "ads": "selectable",
    "keywords": "selectable",
    "search_terms": "fixed_ortz",
}

ENTITY_KEY_FIELDS: dict[str, list[str]] = {
    "campaigns": ["campaign_id", "country_or_region", "device_class"],
    "ad_groups": ["campaign_id", "ad_group_id", "country_or_region", "device_class"],
    "ads": ["campaign_id", "ad_group_id", "ad_id", "country_or_region", "device_class"],
    "keywords": ["campaign_id", "ad_group_id", "keyword_id", "country_or_region", "device_class"],
    "search_terms": [
        "campaign_id",
        "ad_group_id",
        "keyword_id",
        "match_type",
        "search_term_source",
        "search_term_text",
        "country_or_region",
        "device_class",
    ],
}

ATTRIBUTION_DEFINITIONS: dict[str, dict[str, str | None]] = {
    "tap": {
        "installs_field": "tap_installs",
        "new_downloads_field": "tap_new_downloads",
        "redownloads_field": "tap_redownloads",
        "pre_orders_field": "tap_pre_orders_placed",
        "cpi_field": "tap_install_cpi",
        "cpi_formula": "local_spend / tap_installs",
        "conversion_rate_field": "tap_install_rate",
        "conversion_rate_formula": "tap_installs / taps",
    },
    "view": {
        "installs_field": "view_installs",
        "new_downloads_field": "view_new_downloads",
        "redownloads_field": "view_redownloads",
        "pre_orders_field": "view_pre_orders_placed",
        "cpi_field": None,
        "cpi_formula": None,
        "conversion_rate_field": None,
        "conversion_rate_formula": None,
        "note": (
            "Apple Ads Platform API v1 exposes view-attributed install and download counts only; "
            "view-only CPI and conversion rate are not defined by the API and remain null."
        ),
    },
    "total": {
        "installs_field": "total_installs",
        "new_downloads_field": "total_new_downloads",
        "redownloads_field": "total_redownloads",
        "pre_orders_field": "total_pre_orders_placed",
        "cpi_field": "total_avg_cpi",
        "cpi_formula": "local_spend / total_installs",
        "conversion_rate_field": "total_install_rate",
        "conversion_rate_formula": "total_installs / taps",
    },
}


@dataclass
class PaginationRecord:
    """Pagination audit record for one API query sequence."""

    campaign_id: int | None
    window_start: str | None
    window_end: str | None
    pages_fetched: int = 0
    rows_fetched: int = 0
    reported_total_count: int | None = None
    complete: bool = False
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize pagination record to dictionary."""
        return {
            "campaign_id": self.campaign_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "pages_fetched": self.pages_fetched,
            "rows_fetched": self.rows_fetched,
            "reported_total_count": self.reported_total_count,
            "complete": self.complete,
            "error": self.error,
        }


@dataclass
class ScopeDiscoveryResult:
    """Account metadata and per-app campaign scope."""

    ad_account_id: int
    ad_account_name: str | None
    org_id: int | None
    currency: str | None
    account_timezone: str | None
    app_name: str | None
    campaigns: list[Campaign] = field(default_factory=list)
    pagination: PaginationRecord = field(
        default_factory=lambda: PaginationRecord(campaign_id=None, window_start=None, window_end=None)
    )
    errors: list[dict[str, Any]] = field(default_factory=list)


def _cli_version() -> str:
    """Return installed ``asa-api-cli`` version."""
    try:
        return version("asa-api-cli")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0"


def _client_version() -> str:
    """Return installed ``asa-api-client`` version."""
    try:
        return version("asa-api-client")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_decimal(raw: str | Decimal | None) -> Decimal | None:
    """Parse a string or Decimal amount without float rounding."""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _format_decimal(
    value: Decimal | None,
    *,
    rounding: str = ROUND_HALF_UP,
) -> str | None:
    """Format a Decimal to 2–4 fractional digits (preserving cents and fractional unit costs)."""
    if value is None:
        return None
    quantized = value.quantize(Decimal("0.0001"), rounding=rounding)
    text = f"{quantized:f}"
    whole, dot, frac = text.partition(".")
    if not dot:
        return f"{whole}.00"
    trimmed = frac.rstrip("0")
    if len(trimmed) < 2:
        trimmed = trimmed.ljust(2, "0")
    return f"{whole}.{trimmed}"


def _error_dict(
    *,
    scope: str,
    level: str | None,
    message: str,
    error_type: str = "ExportError",
    status_code: int | None = None,
    campaign_id: int | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Build a structured, machine-readable error record."""
    return {
        "scope": scope,
        "level": level,
        "campaign_id": campaign_id,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "offset": offset,
        "error_type": error_type,
        "status_code": status_code,
        "message": message,
    }


def _compute_derived_metrics(
    *,
    impressions: int | None,
    taps: int | None,
    local_spend: Decimal | None,
    tap_installs: int | None,
    total_installs: int | None,
) -> dict[str, str | float | None]:
    """Compute rate and unit-cost metrics strictly from same-basis numerators and denominators."""
    ttr: float | None = None
    if impressions is not None and taps is not None and impressions > 0:
        ttr = round(taps / impressions, 6)

    cpt: str | None = None
    if local_spend is not None and taps is not None and taps > 0:
        cpt = _format_decimal(local_spend / Decimal(taps))

    cpm: str | None = None
    if local_spend is not None and impressions is not None and impressions > 0:
        cpm = _format_decimal((local_spend * Decimal(1000)) / Decimal(impressions))

    tap_install_rate: float | None = None
    if tap_installs is not None and taps is not None and taps > 0:
        tap_install_rate = round(tap_installs / taps, 6)

    tap_install_cpi: str | None = None
    if local_spend is not None and tap_installs is not None and tap_installs > 0:
        tap_install_cpi = _format_decimal(local_spend / Decimal(tap_installs))

    total_install_rate: float | None = None
    if total_installs is not None and taps is not None and taps > 0:
        total_install_rate = round(total_installs / taps, 6)

    total_avg_cpi: str | None = None
    if local_spend is not None and total_installs is not None and total_installs > 0:
        total_avg_cpi = _format_decimal(local_spend / Decimal(total_installs))

    return {
        "ttr": ttr,
        "cpt": cpt,
        "cpm": cpm,
        "tap_install_rate": tap_install_rate,
        "tap_install_cpi": tap_install_cpi,
        "total_install_rate": total_install_rate,
        "total_avg_cpi": total_avg_cpi,
    }


def serialize_apps_metrics(
    metrics: AppsMetrics,
    *,
    default_currency: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Serialize an ``AppsMetrics`` object with explicit tap/view/total attribution fields.

    Valid zeroes (``0``, ``"0.00"``) are preserved; missing or unsupported API
    values remain ``None`` and are never fabricated into zeroes.
    """
    spend_dec = _parse_decimal(metrics.local_spend.amount) if metrics.local_spend else None
    detected_currency: str | None = None
    for money_obj in (
        metrics.local_spend,
        metrics.cpt,
        metrics.cpm,
        metrics.tap_install_cpi,
        metrics.total_avg_cpi,
    ):
        if money_obj is not None and money_obj.currency:
            detected_currency = money_obj.currency
            break
    currency = detected_currency or default_currency

    derived = _compute_derived_metrics(
        impressions=metrics.impressions,
        taps=metrics.taps,
        local_spend=spend_dec,
        tap_installs=metrics.tap_installs,
        total_installs=metrics.total_installs,
    )

    out: dict[str, Any] = {
        "currency": currency,
        "impressions": metrics.impressions,
        "taps": metrics.taps,
        "local_spend": _format_decimal(spend_dec),
        "ttr": derived["ttr"],
        "cpt": derived["cpt"],
        "cpm": derived["cpm"],
        # Tap attribution
        "tap_installs": metrics.tap_installs,
        "tap_new_downloads": metrics.tap_new_downloads,
        "tap_redownloads": metrics.tap_redownloads,
        "tap_pre_orders_placed": metrics.tap_pre_orders_placed,
        "tap_install_rate": derived["tap_install_rate"],
        "tap_install_cpi": derived["tap_install_cpi"],
        # View-through attribution (counts only; view CPI/rate unsupported by Apple Ads API)
        "view_installs": metrics.view_installs,
        "view_new_downloads": metrics.view_new_downloads,
        "view_redownloads": metrics.view_redownloads,
        "view_pre_orders_placed": metrics.view_pre_orders_placed,
        # Total attribution (tap + view)
        "total_installs": metrics.total_installs,
        "total_new_downloads": metrics.total_new_downloads,
        "total_redownloads": metrics.total_redownloads,
        "total_pre_orders_placed": metrics.total_pre_orders_placed,
        "total_install_rate": derived["total_install_rate"],
        "total_avg_cpi": derived["total_avg_cpi"],
    }
    return out, currency


def _sum_int_values(values: Sequence[int | None], field_name: str, notes: list[str]) -> int | None:
    """Sum integer observations while distinguishing valid zeroes from missing values."""
    if not values:
        return None
    missing_count = sum(1 for v in values if v is None)
    if missing_count == len(values):
        return None
    if missing_count > 0:
        notes.append(
            f"Metric '{field_name}' was missing on {missing_count} of {len(values)} observations; "
            "period total left null rather than assuming zero."
        )
        return None
    return sum(v for v in values if v is not None)


def _sum_decimal_values(
    values: Sequence[Decimal | None],
    field_name: str,
    notes: list[str],
) -> Decimal | None:
    """Sum Decimal observations while distinguishing valid zeroes from missing values."""
    if not values:
        return None
    missing_count = sum(1 for v in values if v is None)
    if missing_count == len(values):
        return None
    if missing_count > 0:
        notes.append(
            f"Metric '{field_name}' was missing on {missing_count} of {len(values)} observations; "
            "period total left null rather than assuming zero."
        )
        return None
    total = Decimal("0.00")
    for val in values:
        if val is not None:
            total += val
    return total


def aggregate_metric_rows(
    rows: Sequence[dict[str, Any]],
    *,
    default_currency: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Aggregate daily metric dictionaries into period totals without averaging ratios."""
    notes: list[str] = []
    int_sums: dict[str, int | None] = {}
    for field_name in ADDITIVE_INT_FIELDS:
        int_sums[field_name] = _sum_int_values(
            [row.get(field_name) for row in rows],
            field_name,
            notes,
        )

    spend_sum = _sum_decimal_values(
        [_parse_decimal(row.get("local_spend")) for row in rows],
        "local_spend",
        notes,
    )
    currency = next((str(r["currency"]) for r in rows if r.get("currency")), default_currency)

    derived = _compute_derived_metrics(
        impressions=int_sums["impressions"],
        taps=int_sums["taps"],
        local_spend=spend_sum,
        tap_installs=int_sums["tap_installs"],
        total_installs=int_sums["total_installs"],
    )

    aggregated: dict[str, Any] = {
        "currency": currency,
        "impressions": int_sums["impressions"],
        "taps": int_sums["taps"],
        "local_spend": _format_decimal(spend_sum),
        "ttr": derived["ttr"],
        "cpt": derived["cpt"],
        "cpm": derived["cpm"],
        "tap_installs": int_sums["tap_installs"],
        "tap_new_downloads": int_sums["tap_new_downloads"],
        "tap_redownloads": int_sums["tap_redownloads"],
        "tap_pre_orders_placed": int_sums["tap_pre_orders_placed"],
        "tap_install_rate": derived["tap_install_rate"],
        "tap_install_cpi": derived["tap_install_cpi"],
        "view_installs": int_sums["view_installs"],
        "view_new_downloads": int_sums["view_new_downloads"],
        "view_redownloads": int_sums["view_redownloads"],
        "view_pre_orders_placed": int_sums["view_pre_orders_placed"],
        "total_installs": int_sums["total_installs"],
        "total_new_downloads": int_sums["total_new_downloads"],
        "total_redownloads": int_sums["total_redownloads"],
        "total_pre_orders_placed": int_sums["total_pre_orders_placed"],
        "total_install_rate": derived["total_install_rate"],
        "total_avg_cpi": derived["total_avg_cpi"],
    }
    return aggregated, notes


def _lookup_app_name(client: AppleAdsClient, adam_id: int) -> str | None:
    """Best-effort read-only lookup of app display name."""
    try:
        page = client.apps.search(return_owned_apps=True, limit=500)
        for info in page:
            if info.adam_id is not None and int(info.adam_id) == adam_id and info.app_name:
                return str(info.app_name)
    except Exception:
        pass
    return None


def _discover_app_scope(
    client: AppleAdsClient,
    *,
    adam_id: int,
    campaign_page_size: int = DEFAULT_CAMPAIGN_PAGE_SIZE,
) -> ScopeDiscoveryResult:
    """Resolve ad account metadata and paginated campaign scope for a single app."""
    errors: list[dict[str, Any]] = []
    raw_account_id = client.ad_account_id
    if raw_account_id is None:
        try:
            raw_account_id = client.resolve_ad_account()
        except AppleSearchAdsError as exc:
            errors.append(
                _error_dict(
                    scope="account_resolution",
                    level=None,
                    message=exc.message,
                    error_type=type(exc).__name__,
                    status_code=exc.status_code,
                )
            )
            return ScopeDiscoveryResult(
                ad_account_id=0,
                ad_account_name=None,
                org_id=None,
                currency=None,
                account_timezone=None,
                app_name=None,
                errors=errors,
            )
    ad_account_id = int(raw_account_id)

    account_name: str | None = None
    org_id: int | None = None
    currency: str | None = None
    account_timezone: str | None = None

    try:
        acct: AdAccount = client.ad_accounts.get(ad_account_id)
        account_name = acct.name
        org_id = acct.org_id
        currency = enum_value(acct.currency) or None
        account_timezone = acct.timezone
    except AppleSearchAdsError as exc:
        errors.append(
            _error_dict(
                scope="account",
                level=None,
                message=exc.message,
                error_type=type(exc).__name__,
                status_code=exc.status_code,
            )
        )
        return ScopeDiscoveryResult(
            ad_account_id=ad_account_id,
            ad_account_name=None,
            org_id=None,
            currency=None,
            account_timezone=None,
            app_name=None,
            errors=errors,
        )

    pagination_rec = PaginationRecord(campaign_id=None, window_start=None, window_end=None)
    all_campaigns: list[Campaign] = []
    offset = 0

    while True:
        try:
            page = client.campaigns.query(Query().page(size=campaign_page_size, offset=offset, fetch_total_count=True))
        except AppleSearchAdsError as exc:
            err = _error_dict(
                scope="scope_campaigns",
                level=None,
                message=exc.message,
                error_type=type(exc).__name__,
                status_code=exc.status_code,
                offset=offset,
            )
            pagination_rec.error = err
            pagination_rec.complete = False
            errors.append(err)
            break

        pagination_rec.pages_fetched += 1
        batch = list(page.result)
        all_campaigns.extend(batch)
        pagination_rec.rows_fetched = len(all_campaigns)

        total_count = page.pagination.total_count if page.pagination is not None else None
        if total_count is not None:
            pagination_rec.reported_total_count = total_count

        if not batch:
            if pagination_rec.reported_total_count is not None and offset < pagination_rec.reported_total_count:
                err = _error_dict(
                    scope="scope_campaigns",
                    level=None,
                    message=(
                        f"Campaign query pagination ended early at offset {offset} "
                        f"before reported totalCount {pagination_rec.reported_total_count}."
                    ),
                    error_type="IncompletePaginationError",
                    offset=offset,
                )
                pagination_rec.error = err
                pagination_rec.complete = False
                errors.append(err)
            else:
                pagination_rec.complete = True
            break

        if pagination_rec.reported_total_count is not None:
            if offset + len(batch) >= pagination_rec.reported_total_count:
                pagination_rec.complete = True
                break
        elif len(batch) < campaign_page_size:
            pagination_rec.complete = True
            break

        offset += len(batch)

    # Fallback currency from any campaign in the account when AdAccount.currency is omitted
    if currency is None:
        for camp in all_campaigns:
            inferred = campaign_currency(camp, default="")
            if inferred:
                currency = inferred
                break

    app_campaigns: list[Campaign] = []
    for camp in all_campaigns:
        if camp.ad_account_id is not None and int(camp.ad_account_id) != ad_account_id:
            err = _error_dict(
                scope="scope_campaigns",
                level=None,
                message=(f"Campaign {camp.id} reported adAccountId={camp.ad_account_id}, expected {ad_account_id}."),
                error_type="AccountIsolationError",
                campaign_id=camp.id,
            )
            errors.append(err)
            pagination_rec.complete = False
            continue
        if campaign_adam_id(camp) == adam_id:
            app_campaigns.append(camp)

    app_campaigns.sort(key=lambda c: c.id or 0)
    app_name = _lookup_app_name(client, adam_id)

    return ScopeDiscoveryResult(
        ad_account_id=ad_account_id,
        ad_account_name=account_name,
        org_id=org_id,
        currency=currency,
        account_timezone=account_timezone,
        app_name=app_name,
        campaigns=app_campaigns,
        pagination=pagination_rec,
        errors=errors,
    )


def _fetch_paged_report(
    fetch_fn: Callable[[AppsReportingRequest], Any],
    *,
    level: str,
    campaign_id: int | None,
    filters: list[ReportFilter],
    window_start: date,
    window_end: date,
    time_zone: ReportTimeZone,
    page_size: int,
    include_zero_rows: bool,
) -> tuple[list[Any], PaginationRecord]:
    """Execute one report window across all pages with strict pagination verification."""
    rec = PaginationRecord(
        campaign_id=campaign_id,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )
    rows: list[Any] = []
    offset = 0
    include_rows = [AppsIncludeRows.GRAND_TOTAL]
    if include_zero_rows and level != "search_terms":
        include_rows.append(AppsIncludeRows.EMPTY_METRICS)

    while True:
        req = AppsReportingRequest(
            filters=filters,
            time_range=ReportTimeRange(
                start=window_start,
                end=window_end,
                time_zone=time_zone,
                granularity=ReportGranularity.DAILY,
            ),
            pagination=RequestPagination(offset=offset, page_size=page_size),
            options=AppsOptions(include_rows=include_rows),
        )
        try:
            resp = fetch_fn(req)
        except AppleSearchAdsError as exc:
            err = _error_dict(
                scope="report_query",
                level=level,
                campaign_id=campaign_id,
                window_start=window_start,
                window_end=window_end,
                offset=offset,
                error_type=type(exc).__name__,
                status_code=exc.status_code,
                message=exc.message,
            )
            rec.error = err
            rec.complete = False
            break

        rec.pages_fetched += 1
        container = getattr(resp, "result", None)
        batch = list(container.rows or []) if container is not None else []
        rows.extend(batch)
        rec.rows_fetched = len(rows)

        pagination = getattr(resp, "pagination", None)
        total_count = pagination.total_count if pagination is not None else None
        if total_count is not None:
            rec.reported_total_count = total_count

        if not batch:
            if rec.reported_total_count is not None and offset < rec.reported_total_count:
                rec.error = _error_dict(
                    scope="report_pagination",
                    level=level,
                    campaign_id=campaign_id,
                    window_start=window_start,
                    window_end=window_end,
                    offset=offset,
                    error_type="IncompletePaginationError",
                    message=(
                        f"{level} report pagination ended early at offset {offset} "
                        f"before reported totalCount {rec.reported_total_count}."
                    ),
                )
                rec.complete = False
            else:
                rec.complete = True
            break

        if rec.reported_total_count is not None:
            if offset + len(batch) >= rec.reported_total_count:
                rec.complete = True
                break
        elif len(batch) < page_size:
            rec.complete = True
            break

        offset += len(batch)

    return rows, rec


def _extract_level_metadata(
    level: str,
    meta: Any,
    *,
    ad_account_id: int,
    adam_id: int,
    expected_campaign_ids: set[int],
    requested_campaign_id: int | None,
    campaign_catalog_names: dict[int, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Extract normalized metadata and enforce strict app/account isolation."""
    if meta is None:
        return None, _error_dict(
            scope="row_validation",
            level=level,
            campaign_id=requested_campaign_id,
            error_type="MissingMetadataError",
            message=f"{level} report row omitted metadata.",
        )

    meta_account = getattr(meta, "ad_account_id", None)
    if meta_account is not None and int(meta_account) != ad_account_id:
        return None, _error_dict(
            scope="row_validation",
            level=level,
            campaign_id=requested_campaign_id,
            error_type="AccountIsolationError",
            message=f"{level} row reported adAccountId={meta_account}, expected {ad_account_id}.",
        )

    if level == "campaigns" and isinstance(meta, AppsReportingCampaign):
        if meta.id is None:
            return None, _error_dict(
                scope="row_validation",
                level=level,
                error_type="MissingEntityIdError",
                message="Campaign report row omitted metadata.id.",
            )
        cid = int(meta.id)
        if cid not in expected_campaign_ids:
            return None, _error_dict(
                scope="row_validation",
                level=level,
                campaign_id=cid,
                error_type="AppIsolationError",
                message=f"Campaign {cid} is outside the scoped campaigns for app {adam_id}.",
            )
        if meta.promoted_object_id is not None and str(meta.promoted_object_id) != str(adam_id):
            return None, _error_dict(
                scope="row_validation",
                level=level,
                campaign_id=cid,
                error_type="AppIsolationError",
                message=(f"Campaign {cid} reported promotedObjectId={meta.promoted_object_id}, expected {adam_id}."),
            )
        return {
            "ad_account_id": ad_account_id,
            "adam_id": adam_id,
            "campaign_id": cid,
            "campaign_name": meta.name or campaign_catalog_names.get(cid),
            "campaign_status": enum_value(meta.status) or None,
            "display_status": enum_value(meta.display_status) or None,
            "ad_channel_type": enum_value(meta.ad_channel_type) or None,
            "billing_event": enum_value(meta.billing_event) or None,
            "deleted": meta.deleted,
            "country_or_region": meta.country_or_region,
            "device_class": meta.device_class,
        }, None

    cid_raw = getattr(meta, "campaign_id", None)
    resolved_cid = int(cid_raw) if cid_raw is not None else requested_campaign_id
    if resolved_cid is None or resolved_cid not in expected_campaign_ids:
        return None, _error_dict(
            scope="row_validation",
            level=level,
            campaign_id=resolved_cid,
            error_type="AppIsolationError",
            message=f"{level} row campaignId={resolved_cid} is outside app {adam_id} scope.",
        )

    if level == "ad_groups" and isinstance(meta, AppsReportingAdGroup):
        if meta.id is None:
            return None, _error_dict(
                scope="row_validation",
                level=level,
                campaign_id=resolved_cid,
                error_type="MissingEntityIdError",
                message="Ad group report row omitted metadata.id.",
            )
        return {
            "ad_account_id": ad_account_id,
            "adam_id": adam_id,
            "campaign_id": resolved_cid,
            "campaign_name": campaign_catalog_names.get(resolved_cid),
            "ad_group_id": int(meta.id),
            "ad_group_name": meta.name,
            "ad_group_status": enum_value(meta.status) or None,
            "display_status": enum_value(meta.display_status) or None,
            "pricing_model": enum_value(meta.pricing_model) or None,
            "automated_keywords_opt_in": meta.automated_keywords_opt_in,
            "deleted": meta.deleted,
            "country_or_region": meta.country_or_region,
            "device_class": meta.device_class,
        }, None

    if level == "ads" and isinstance(meta, AppsReportingAd):
        if meta.id is None or meta.ad_group_id is None:
            return None, _error_dict(
                scope="row_validation",
                level=level,
                campaign_id=resolved_cid,
                error_type="MissingEntityIdError",
                message="Ad report row omitted metadata.id or metadata.adGroupId.",
            )
        creative = meta.creative
        language = (
            creative.creative_spec.language
            if creative and creative.creative_spec and creative.creative_spec.language
            else None
        )
        return {
            "ad_account_id": ad_account_id,
            "adam_id": adam_id,
            "campaign_id": resolved_cid,
            "campaign_name": campaign_catalog_names.get(resolved_cid),
            "ad_group_id": int(meta.ad_group_id),
            "ad_id": int(meta.id),
            "ad_name": meta.name,
            "ad_status": enum_value(meta.status) or None,
            "display_status": enum_value(meta.display_status) or None,
            "creative_id": int(creative.id) if creative and creative.id is not None else None,
            "creative_type": enum_value(creative.creative_type) if creative else None,
            "language": language,
            "deleted": meta.deleted,
            "country_or_region": meta.country_or_region,
            "device_class": meta.device_class,
        }, None

    if level == "keywords" and isinstance(meta, ReportingKeyword):
        if meta.id is None or meta.ad_group_id is None:
            return None, _error_dict(
                scope="row_validation",
                level=level,
                campaign_id=resolved_cid,
                error_type="MissingEntityIdError",
                message="Keyword report row omitted metadata.id or metadata.adGroupId.",
            )
        return {
            "ad_account_id": ad_account_id,
            "adam_id": adam_id,
            "campaign_id": resolved_cid,
            "campaign_name": campaign_catalog_names.get(resolved_cid),
            "ad_group_id": int(meta.ad_group_id),
            "ad_group_name": meta.ad_group.name if meta.ad_group else None,
            "keyword_id": int(meta.id),
            "keyword_text": meta.text,
            "match_type": enum_value(meta.match_type) or None,
            "keyword_status": enum_value(meta.status) or None,
            "display_status": enum_value(meta.display_status) or None,
            "bid_amount": _format_decimal(_parse_decimal(meta.bid.amount)) if meta.bid else None,
            "deleted": meta.deleted,
            "country_or_region": meta.country_or_region,
            "device_class": meta.device_class,
        }, None

    if level == "search_terms" and isinstance(meta, ReportingSearchTerm):
        kw = meta.keyword
        return {
            "ad_account_id": ad_account_id,
            "adam_id": adam_id,
            "campaign_id": resolved_cid,
            "campaign_name": campaign_catalog_names.get(resolved_cid),
            "ad_group_id": int(meta.ad_group_id) if meta.ad_group_id is not None else None,
            "ad_group_name": meta.ad_group.name if meta.ad_group else None,
            "keyword_id": int(kw.id) if kw and kw.id is not None else None,
            "keyword_text": kw.text if kw else None,
            "match_type": (enum_value(kw.match_type) or None) if kw else None,
            "search_term_source": meta.search_term_source,
            "search_term_text": meta.search_term_text,
            "country_or_region": meta.country_or_region,
            "device_class": meta.device_class,
        }, None

    return None, _error_dict(
        scope="row_validation",
        level=level,
        campaign_id=resolved_cid,
        error_type="UnexpectedMetadataTypeError",
        message=f"Unexpected metadata type {type(meta).__name__} for {level}.",
    )


def _entity_key(level: str, row: dict[str, Any]) -> tuple[Any, ...]:
    """Return the stable identity tuple for an entity row (never keyed by display name)."""
    return tuple(row.get(k) for k in ENTITY_KEY_FIELDS[level])


def _primary_name_field(level: str) -> str | None:
    """Return the mutable human label field for a level, if applicable."""
    return {
        "campaigns": "campaign_name",
        "ad_groups": "ad_group_name",
        "ads": "ad_name",
        "keywords": "keyword_text",
        "search_terms": None,
    }[level]


def _build_period_totals_and_summary(
    level: str,
    daily_rows: list[dict[str, Any]],
    *,
    default_currency: str | None,
    catalog_names: dict[int, str],
    level_complete: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Aggregate sorted daily rows into per-entity ``period_totals`` and level ``summary``."""
    notes: list[str] = []
    if not daily_rows:
        return [], None, notes

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in daily_rows:
        key = _entity_key(level, row)
        grouped.setdefault(key, []).append(row)

    name_field = _primary_name_field(level)
    period_totals: list[dict[str, Any]] = []

    for key in sorted(grouped.keys(), key=lambda tup: tuple("" if x is None else str(x) for x in tup)):
        group = grouped[key]
        # Latest observation in chronological order carries latest entity attributes
        latest = group[-1]
        agg_metrics, group_notes = aggregate_metric_rows(group, default_currency=default_currency)
        for note in group_notes:
            if note not in notes:
                notes.append(note)

        base: dict[str, Any] = {k: v for k, v in latest.items() if k not in agg_metrics and k != "date"}
        if name_field is not None:
            seen_names: list[str] = []
            if level == "campaigns":
                cat_name = catalog_names.get(int(latest["campaign_id"]))
                if cat_name:
                    seen_names.append(cat_name)
            for obs in group:
                candidate = obs.get(name_field)
                if isinstance(candidate, str) and candidate and candidate not in seen_names:
                    seen_names.append(candidate)
            if level == "campaigns":
                cat_name = catalog_names.get(int(latest["campaign_id"]))
                if cat_name:
                    base[name_field] = cat_name
                    if cat_name not in seen_names:
                        seen_names.append(cat_name)
            base["observed_names"] = seen_names
            if len(seen_names) > 1:
                notes.append(
                    f"Entity {dict(zip(ENTITY_KEY_FIELDS[level], key, strict=True))} had multiple "
                    f"observed names ({seen_names}); rows were reconciled by stable ID."
                )

        period_totals.append({**base, **agg_metrics})

    summary: dict[str, Any] | None = None
    if level_complete:
        summary_metrics, summary_notes = aggregate_metric_rows(
            daily_rows,
            default_currency=default_currency,
        )
        for note in summary_notes:
            if note not in notes:
                notes.append(note)
        summary = {
            "entity_count": len(period_totals),
            "daily_row_count": len(daily_rows),
            **summary_metrics,
        }

    return period_totals, summary, notes


def _reconcile_levels(levels_payload: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Verify daily-to-period mathematical reconciliation and cross-level coverage vs campaigns."""
    all_daily_match = True
    level_reconciliations: dict[str, Any] = {}

    campaigns_section = levels_payload["campaigns"]
    campaigns_comparable = campaigns_section["complete"] and campaigns_section["status"] in ("complete", "empty")
    campaigns_summary = campaigns_section.get("summary")

    for level in LEVEL_ORDER:
        sec = levels_payload[level]
        daily_rows: list[dict[str, Any]] = sec["daily"]
        period_rows: list[dict[str, Any]] = sec["period_totals"]
        discrepancies: list[str] = []

        if sec["status"] in ("unavailable", "failed"):
            level_reconciliations[level] = {
                "daily_to_period_matches": None,
                "discrepancies": [],
            }
            if level != "campaigns":
                level_reconciliations[level]["vs_campaigns"] = {
                    "comparable": False,
                    "comparison_skipped_reason": f"level_{sec['status']}"
                    + (f": {sec['reason']}" if sec.get("reason") else ""),
                    "matches_campaigns": None,
                    "deltas": None,
                    "explanation": sec["notes"][0] if sec["notes"] else None,
                }
            continue

        daily_agg, _ = aggregate_metric_rows(daily_rows, default_currency=sec.get("currency"))
        period_agg, _ = aggregate_metric_rows(period_rows, default_currency=sec.get("currency"))

        for int_field in ADDITIVE_INT_FIELDS:
            if daily_agg.get(int_field) != period_agg.get(int_field):
                discrepancies.append(
                    f"{int_field}: daily sum {daily_agg.get(int_field)} != period sum {period_agg.get(int_field)}"
                )
        if daily_agg.get("local_spend") != period_agg.get("local_spend"):
            discrepancies.append(
                f"local_spend: daily sum {daily_agg.get('local_spend')} != period sum {period_agg.get('local_spend')}"
            )

        matches = len(discrepancies) == 0
        if not matches:
            all_daily_match = False

        entry: dict[str, Any] = {
            "daily_to_period_matches": matches,
            "discrepancies": discrepancies,
        }

        if level != "campaigns":
            same_tz = sec.get("timezone") == campaigns_section.get("timezone")
            same_coverage = sec.get("actual_start") == campaigns_section.get("actual_start") and sec.get(
                "actual_end"
            ) == campaigns_section.get("actual_end")
            sub_complete = sec["complete"] and sec["status"] in ("complete", "empty")

            if not (campaigns_comparable and sub_complete and same_tz and same_coverage):
                if not campaigns_comparable or not sub_complete:
                    skip_reason = "incomplete_collection"
                elif not same_tz:
                    skip_reason = f"timezone_mismatch ({sec.get('timezone')} vs {campaigns_section.get('timezone')})"
                else:
                    skip_reason = (
                        f"coverage_mismatch ({sec.get('actual_start')}..{sec.get('actual_end')} "
                        f"vs {campaigns_section.get('actual_start')}..{campaigns_section.get('actual_end')})"
                    )
                entry["vs_campaigns"] = {
                    "comparable": False,
                    "comparison_skipped_reason": skip_reason,
                    "matches_campaigns": None,
                    "deltas": None,
                    "explanation": sec["notes"][0] if sec["notes"] else None,
                }
            else:
                deltas: dict[str, Any] = {}
                exact_match = True
                for int_field in RECONCILE_INT_FIELDS:
                    c_val = (campaigns_summary or {}).get(int_field) if campaigns_summary else 0
                    s_val = daily_agg.get(int_field) if daily_rows else 0
                    if c_val is None and not campaigns_section["daily"]:
                        c_val = 0
                    if s_val is None and not daily_rows:
                        s_val = 0
                    if c_val is None or s_val is None:
                        deltas[int_field] = None
                        if c_val != s_val:
                            exact_match = False
                    else:
                        diff = int(s_val) - int(c_val)
                        deltas[int_field] = diff
                        if diff != 0:
                            exact_match = False

                c_spend = (
                    _parse_decimal((campaigns_summary or {}).get("local_spend"))
                    if campaigns_summary
                    else Decimal("0.00")
                )
                s_spend = _parse_decimal(daily_agg.get("local_spend")) if daily_rows else Decimal("0.00")
                if c_spend is None and not campaigns_section["daily"]:
                    c_spend = Decimal("0.00")
                if s_spend is None and not daily_rows:
                    s_spend = Decimal("0.00")

                if c_spend is None or s_spend is None:
                    deltas["local_spend"] = None
                    if c_spend != s_spend:
                        exact_match = False
                else:
                    spend_diff = s_spend - c_spend
                    deltas["local_spend"] = _format_decimal(spend_diff)
                    if spend_diff != Decimal("0.00"):
                        exact_match = False

                explanation: str | None = None
                if not exact_match:
                    if level == "keywords":
                        explanation = (
                            "Keyword totals exclude Search Match / automated keyword delivery and "
                            "placements without bidded keyword IDs (e.g. Search tab, Today tab, product pages)."
                        )
                    elif level == "search_terms":
                        explanation = (
                            "Apple Ads suppresses low-volume search terms below privacy thresholds, "
                            "so search-term totals typically sum to less than campaign totals."
                        )
                    elif level == "ads":
                        explanation = (
                            "Ad-level totals reflect creative-backed ads; default placements without "
                            "explicit ad records can account for unallocated campaign delivery."
                        )
                    else:
                        explanation = (
                            "Subordinate totals differ from campaign totals; inspect campaign placement "
                            "types or rounding across windows."
                        )

                entry["vs_campaigns"] = {
                    "comparable": True,
                    "comparison_skipped_reason": None,
                    "matches_campaigns": exact_match,
                    "deltas": deltas,
                    "explanation": explanation,
                }

        level_reconciliations[level] = entry

    return {
        "daily_to_period_matches": all_daily_match,
        "levels": level_reconciliations,
    }


def _empty_level_section(
    level: str,
    *,
    status: str,
    complete: bool,
    reason: str | None,
    timezone: str | None,
    requested_start: date,
    requested_end: date,
    actual_start: date | None,
    actual_end: date | None,
    currency: str | None,
    notes: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct a structured level section with zero rows."""
    return {
        "status": status,
        "complete": complete,
        "reason": reason,
        "timezone": timezone,
        "timezone_mode": TIMEZONE_POLICY[level],
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": actual_start.isoformat() if actual_start else None,
        "actual_end": actual_end.isoformat() if actual_end else None,
        "observed_min_date": None,
        "observed_max_date": None,
        "currency": currency,
        "entity_key_fields": ENTITY_KEY_FIELDS[level],
        "row_key_fields": [*ENTITY_KEY_FIELDS[level], "date"],
        "pagination": {
            "requests_expected": 0,
            "requests_succeeded": 0,
            "requests_failed": 0,
            "pages_fetched": 0,
            "api_rows_fetched": 0,
            "complete": complete,
            "requests": [],
        },
        "notes": list(notes or []),
        "errors": list(errors or []),
        "summary": None,
        "period_totals": [],
        "daily": [],
    }


def collect_app_report_export(
    client: AppleAdsClient,
    *,
    adam_id: int,
    start_date: date,
    end_date: date,
    requested_timezone: ReportTimeZone = ReportTimeZone.ORTZ,
    include_ortz_search_terms: bool = False,
    include_zero_rows: bool = False,
    page_size: int = DEFAULT_REPORT_PAGE_SIZE,
    campaign_page_size: int = DEFAULT_CAMPAIGN_PAGE_SIZE,
    today: date | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    """Collect a read-only, reconcilable per-app multi-level report export.

    Args:
        client: Configured read-only ``AppleAdsClient`` (Platform API v1).
        adam_id: App Store ``adamId`` to isolate and export.
        start_date: Inclusive period start date.
        end_date: Inclusive period end date.
        requested_timezone: ``ORTZ`` (default) or ``UTC``.
        include_ortz_search_terms: When ``requested_timezone`` is ``UTC``,
            explicitly opt into collecting ORTZ search-term rows in a separate
            ``ORTZ``-tagged section instead of marking ``search_terms`` unavailable.
        include_zero_rows: Request ``EMPTY_METRICS`` on entity report levels.
        page_size: Report pagination page size (1..5000).
        campaign_page_size: Campaign discovery page size (1..500).
        today: Reference current date for the 90-day search-term lookback window.
        collected_at: Timestamp override for deterministic testing.

    Returns:
        Structured export dictionary with machine-detectable completeness status.
    """
    now_utc = collected_at or datetime.now(tz=UTC)
    ref_today = today or now_utc.date()
    windows = chunk_windows(start_date, end_date, max_days=MAX_DAILY_WINDOW)
    day_count = (end_date - start_date).days + 1

    scope = _discover_app_scope(
        client,
        adam_id=adam_id,
        campaign_page_size=campaign_page_size,
    )

    base_envelope: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "status": "failed",
        "complete": False,
        "empty_reason": None,
        "collected_at": now_utc.isoformat(),
        "versions": {
            "asa_api_cli": _cli_version(),
            "asa_api_client": _client_version(),
        },
        "account": {
            "ad_account_id": scope.ad_account_id,
            "ad_account_name": scope.ad_account_name,
            "org_id": scope.org_id,
            "currency": scope.currency,
            "account_timezone": scope.account_timezone,
        },
        "app": {
            "adam_id": adam_id,
            "app_name": scope.app_name,
        },
        "period": {
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "day_count": day_count,
            "requested_timezone": requested_timezone.value,
            "effective_account_timezone": scope.account_timezone,
            "windows": [{"start": w[0].isoformat(), "end": w[1].isoformat()} for w in windows],
        },
        "timezone_policy": dict(TIMEZONE_POLICY),
        "attribution_definitions": ATTRIBUTION_DEFINITIONS,
        "scope": {
            "campaign_ids": [c.id for c in scope.campaigns if c.id is not None],
            "campaigns": [
                {
                    "campaign_id": c.id,
                    "campaign_name": c.name,
                    "status": enum_value(c.status) or None,
                    "display_status": enum_value(c.display_status) or None,
                    "deleted": c.deleted,
                    "adam_id": adam_id,
                }
                for c in scope.campaigns
                if c.id is not None
            ],
            "pagination": scope.pagination.to_dict(),
        },
        "levels": {},
        "reconciliation": None,
        "errors": list(scope.errors),
    }

    if scope.errors or not scope.pagination.complete:
        for level in LEVEL_ORDER:
            tz_str = ReportTimeZone.ORTZ.value if level == "search_terms" else requested_timezone.value
            base_envelope["levels"][level] = _empty_level_section(
                level,
                status="failed",
                complete=False,
                reason="scope_discovery_failed",
                timezone=tz_str,
                requested_start=start_date,
                requested_end=end_date,
                actual_start=None,
                actual_end=None,
                currency=scope.currency,
                errors=scope.errors,
            )
        return base_envelope

    campaign_ids = [int(c.id) for c in scope.campaigns if c.id is not None]
    expected_campaign_ids = set(campaign_ids)
    catalog_names = {int(c.id): c.name for c in scope.campaigns if c.id is not None and c.name}

    # Legitimate empty result: account has zero App Store campaigns for this app.
    if not campaign_ids:
        for level in LEVEL_ORDER:
            if level == "search_terms" and requested_timezone == ReportTimeZone.UTC and not include_ortz_search_terms:
                base_envelope["levels"][level] = _empty_level_section(
                    level,
                    status="unavailable",
                    complete=True,
                    reason="timezone_incompatible",
                    timezone=None,
                    requested_start=start_date,
                    requested_end=end_date,
                    actual_start=None,
                    actual_end=None,
                    currency=scope.currency,
                    notes=[
                        "Search-term reports require ORTZ (organization timezone) in Apple Ads Platform API v1 "
                        "and are omitted from UTC exports so ORTZ rows are never mixed with UTC daily data."
                    ],
                )
            else:
                eff_tz = ReportTimeZone.ORTZ.value if level == "search_terms" else requested_timezone.value
                base_envelope["levels"][level] = _empty_level_section(
                    level,
                    status="empty",
                    complete=True,
                    reason="no_campaigns_for_app",
                    timezone=eff_tz,
                    requested_start=start_date,
                    requested_end=end_date,
                    actual_start=start_date,
                    actual_end=end_date,
                    currency=scope.currency,
                )
        base_envelope["status"] = "empty"
        base_envelope["complete"] = True
        base_envelope["empty_reason"] = "no_campaigns_for_app"
        base_envelope["reconciliation"] = _reconcile_levels(base_envelope["levels"])
        return base_envelope

    fetch_fns: dict[str, Callable[[AppsReportingRequest], Any]] = {
        "campaigns": client.reports.campaigns,
        "ad_groups": client.reports.ad_groups,
        "ads": client.reports.ads,
        "keywords": client.reports.keywords,
        "search_terms": client.reports.search_terms,
    }

    all_errors: list[dict[str, Any]] = []
    detected_currency = scope.currency
    levels_payload: dict[str, dict[str, Any]] = {}

    st_min_date = ref_today - timedelta(days=SEARCH_TERM_LOOKBACK)

    for level in LEVEL_ORDER:
        level_notes: list[str] = []
        level_errors: list[dict[str, Any]] = []

        # Check deterministic endpoint constraints for search_terms
        if level == "search_terms":
            if requested_timezone == ReportTimeZone.UTC and not include_ortz_search_terms:
                levels_payload[level] = _empty_level_section(
                    level,
                    status="unavailable",
                    complete=True,
                    reason="timezone_incompatible",
                    timezone=None,
                    requested_start=start_date,
                    requested_end=end_date,
                    actual_start=None,
                    actual_end=None,
                    currency=detected_currency,
                    notes=[
                        "Search-term reports only support ORTZ (organization timezone) in Apple Ads Platform API v1. "
                        "Omitted from UTC export so ORTZ search-term dates are never mixed with UTC daily data. "
                        "Use --timezone ORTZ for unified daily reconciliation, or pass --include-ortz-search-terms."
                    ],
                )
                continue

            st_start = max(start_date, st_min_date)
            if st_start > end_date:
                levels_payload[level] = _empty_level_section(
                    level,
                    status="unavailable",
                    complete=True,
                    reason="outside_lookback_window",
                    timezone=ReportTimeZone.ORTZ.value,
                    requested_start=start_date,
                    requested_end=end_date,
                    actual_start=None,
                    actual_end=None,
                    currency=detected_currency,
                    notes=[
                        f"Search-term reports are only available for the trailing {SEARCH_TERM_LOOKBACK} days "
                        f"(from {st_min_date.isoformat()}); requested period {start_date.isoformat()}.."
                        f"{end_date.isoformat()} precedes the lookback window."
                    ],
                )
                continue

            level_actual_start = st_start
            level_actual_end = end_date
            level_windows = chunk_windows(st_start, end_date, max_days=MAX_DAILY_WINDOW)
            effective_tz = ReportTimeZone.ORTZ
            level_reason: str | None = None
            if st_start > start_date:
                level_reason = "clipped_to_lookback_window"
                level_notes.append(
                    f"Search-term coverage is clipped to {st_start.isoformat()}..{end_date.isoformat()} "
                    f"because Apple Ads restricts search-term reports to the trailing {SEARCH_TERM_LOOKBACK} days."
                )
            if requested_timezone == ReportTimeZone.UTC and include_ortz_search_terms:
                level_notes.append(
                    "Search-term rows are reported in ORTZ (organization timezone) while other levels use UTC; "
                    "do not reconcile daily search-term dates directly against UTC dates."
                )
        else:
            level_actual_start = start_date
            level_actual_end = end_date
            level_windows = windows
            effective_tz = requested_timezone
            level_reason = None

        pagination_records: list[PaginationRecord] = []
        raw_observations: list[dict[str, Any]] = []
        seen_daily_keys: set[tuple[Any, ...]] = set()

        if level == "campaigns":
            campaign_filter = [
                ReportFilter(
                    field="id",
                    operator=ReportFilterOperator.IN,
                    value=[str(cid) for cid in campaign_ids],
                )
            ]
            query_plan: list[tuple[int | None, list[ReportFilter], date, date]] = [
                (None, campaign_filter, win[0], win[1]) for win in level_windows
            ]
        else:
            query_plan = [
                (
                    cid,
                    [
                        ReportFilter(
                            field="campaignId",
                            operator=ReportFilterOperator.EQUALS,
                            value=str(cid),
                        )
                    ],
                    win[0],
                    win[1],
                )
                for cid in campaign_ids
                for win in level_windows
            ]

        for req_cid, req_filters, win_start, win_end in query_plan:
            api_rows, pag_rec = _fetch_paged_report(
                fetch_fns[level],
                level=level,
                campaign_id=req_cid,
                filters=req_filters,
                window_start=win_start,
                window_end=win_end,
                time_zone=effective_tz,
                page_size=page_size,
                include_zero_rows=include_zero_rows,
            )
            pagination_records.append(pag_rec)
            if pag_rec.error is not None:
                level_errors.append(pag_rec.error)
                all_errors.append(pag_rec.error)

            for api_row in api_rows:
                meta_dict, meta_err = _extract_level_metadata(
                    level,
                    getattr(api_row, "metadata", None),
                    ad_account_id=scope.ad_account_id,
                    adam_id=adam_id,
                    expected_campaign_ids=expected_campaign_ids,
                    requested_campaign_id=req_cid,
                    campaign_catalog_names=catalog_names,
                )
                if meta_err is not None or meta_dict is None:
                    if meta_err is not None:
                        level_errors.append(meta_err)
                        all_errors.append(meta_err)
                    continue

                granular: list[AppsMetrics] = list(getattr(api_row, "granular_metrics", None) or [])
                total_m: AppsMetrics | None = getattr(api_row, "total_metrics", None)

                metric_entries: list[tuple[date, AppsMetrics]] = []
                if granular:
                    for g_entry in granular:
                        if g_entry.date is None:
                            g_err = _error_dict(
                                scope="row_validation",
                                level=level,
                                campaign_id=meta_dict.get("campaign_id"),
                                window_start=win_start,
                                window_end=win_end,
                                error_type="MissingGranularDateError",
                                message=f"{level} granularMetrics entry omitted date.",
                            )
                            level_errors.append(g_err)
                            all_errors.append(g_err)
                            continue
                        metric_entries.append((g_entry.date, g_entry))
                elif total_m is not None:
                    fallback_date = total_m.date or (win_start if win_start == win_end else None)
                    if fallback_date is None:
                        g_err = _error_dict(
                            scope="row_validation",
                            level=level,
                            campaign_id=meta_dict.get("campaign_id"),
                            window_start=win_start,
                            window_end=win_end,
                            error_type="MissingGranularMetricsError",
                            message=(
                                f"{level} report returned totalMetrics without daily granularMetrics "
                                f"for multi-day window {win_start.isoformat()}..{win_end.isoformat()}."
                            ),
                        )
                        level_errors.append(g_err)
                        all_errors.append(g_err)
                        continue
                    metric_entries.append((fallback_date, total_m))

                for obs_date, m_entry in metric_entries:
                    m_dict, row_currency = serialize_apps_metrics(
                        m_entry,
                        default_currency=detected_currency,
                    )
                    if detected_currency is None and row_currency:
                        detected_currency = row_currency
                    obs_row: dict[str, Any] = {
                        **meta_dict,
                        "date": obs_date.isoformat(),
                        "time_zone": effective_tz.value,
                        **m_dict,
                    }
                    daily_key = (*_entity_key(level, obs_row), obs_row["date"])
                    if daily_key in seen_daily_keys:
                        dup_err = _error_dict(
                            scope="row_validation",
                            level=level,
                            campaign_id=meta_dict.get("campaign_id"),
                            window_start=win_start,
                            window_end=win_end,
                            error_type="DuplicateDailyRowError",
                            message=f"Duplicate daily observation for key {daily_key} in {level}.",
                        )
                        level_errors.append(dup_err)
                        all_errors.append(dup_err)
                        continue
                    seen_daily_keys.add(daily_key)
                    raw_observations.append(obs_row)

        # Update catalog_names with any campaign names learned from the campaigns report
        if level == "campaigns":
            for obs in raw_observations:
                cid_val = int(obs["campaign_id"])
                cname_val = obs.get("campaign_name")
                if cid_val not in catalog_names and isinstance(cname_val, str) and cname_val:
                    catalog_names[cid_val] = cname_val

        raw_observations.sort(
            key=lambda r: (
                *(("" if r.get(k) is None else str(r.get(k))) for k in ENTITY_KEY_FIELDS[level]),
                str(r["date"]),
            )
        )

        requests_succeeded = sum(1 for r in pagination_records if r.complete and r.error is None)
        requests_failed = len(pagination_records) - requests_succeeded
        level_complete = requests_failed == 0 and len(level_errors) == 0

        if level_complete:
            level_status = "complete" if raw_observations else "empty"
        elif requests_succeeded == 0 and not raw_observations:
            level_status = "failed"
        else:
            level_status = "partial"

        period_totals, summary, agg_notes = _build_period_totals_and_summary(
            level,
            raw_observations,
            default_currency=detected_currency,
            catalog_names=catalog_names,
            level_complete=level_complete,
        )
        level_notes.extend(agg_notes)

        obs_dates = [str(r["date"]) for r in raw_observations]
        levels_payload[level] = {
            "status": level_status,
            "complete": level_complete,
            "reason": level_reason if level_status != "empty" else (level_reason or "no_metrics_in_period"),
            "timezone": effective_tz.value,
            "timezone_mode": TIMEZONE_POLICY[level],
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "actual_start": level_actual_start.isoformat() if level_complete else None,
            "actual_end": level_actual_end.isoformat() if level_complete else None,
            "observed_min_date": min(obs_dates) if obs_dates else None,
            "observed_max_date": max(obs_dates) if obs_dates else None,
            "currency": detected_currency,
            "entity_key_fields": ENTITY_KEY_FIELDS[level],
            "row_key_fields": [*ENTITY_KEY_FIELDS[level], "date"],
            "pagination": {
                "requests_expected": len(pagination_records),
                "requests_succeeded": requests_succeeded,
                "requests_failed": requests_failed,
                "pages_fetched": sum(r.pages_fetched for r in pagination_records),
                "api_rows_fetched": sum(r.rows_fetched for r in pagination_records),
                "complete": level_complete,
                "requests": [r.to_dict() for r in pagination_records],
            },
            "notes": level_notes,
            "errors": level_errors,
            "summary": summary,
            "period_totals": period_totals,
            "daily": raw_observations,
        }

    # Backfill currency if only discovered from report rows
    if detected_currency is not None:
        if base_envelope["account"]["currency"] is None:
            base_envelope["account"]["currency"] = detected_currency
        for lv_sec in levels_payload.values():
            if lv_sec["currency"] is None:
                lv_sec["currency"] = detected_currency

    reconciliation = _reconcile_levels(levels_payload)
    if not reconciliation["daily_to_period_matches"]:
        rec_err = _error_dict(
            scope="reconciliation",
            level=None,
            error_type="ReconciliationError",
            message="Daily metric sums did not match aggregated period totals.",
        )
        all_errors.append(rec_err)

    any_rows = any(bool(levels_payload[lv]["daily"]) for lv in LEVEL_ORDER)
    campaigns_lvl_status = levels_payload["campaigns"]["status"]

    if not all_errors:
        if any_rows:
            overall_status = "complete"
            overall_complete = True
            empty_reason = None
        else:
            overall_status = "empty"
            overall_complete = True
            empty_reason = "no_metrics_in_period"
    elif campaigns_lvl_status == "failed" and not any_rows:
        overall_status = "failed"
        overall_complete = False
        empty_reason = None
    else:
        overall_status = "partial"
        overall_complete = False
        empty_reason = None

    base_envelope["status"] = overall_status
    base_envelope["complete"] = overall_complete
    base_envelope["empty_reason"] = empty_reason
    base_envelope["levels"] = levels_payload
    base_envelope["reconciliation"] = reconciliation
    base_envelope["errors"] = all_errors
    return base_envelope


def write_export_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write structured export JSON so readers never see truncated or stale files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
