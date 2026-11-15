"""Focused deterministic verification tests for per-app reporting exports (Issue #70)."""

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from asa_api_client.exceptions import RateLimitError, ServerError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.ad_accounts import AdAccount
from asa_api_client.v1.models.apps import AppInfo
from asa_api_client.v1.models.base import Money, V1Page, V1Pagination
from asa_api_client.v1.models.campaigns import Campaign, CampaignTargeting, DailyBudget
from asa_api_client.v1.models.reports import (
    AppsAdGroupMetrics,
    AppsAdGroupReportResponse,
    AppsAdGroupReportRow,
    AppsAdGroupResultContainer,
    AppsAdReportResponse,
    AppsAdReportRow,
    AppsAdResultContainer,
    AppsCampaignMetrics,
    AppsCampaignReportResponse,
    AppsCampaignReportRow,
    AppsCampaignResultContainer,
    AppsCreativeType,
    AppsKeywordMatchType,
    AppsKeywordReportResponse,
    AppsKeywordReportRow,
    AppsKeywordResultContainer,
    AppsMetrics,
    AppsReportingAd,
    AppsReportingAdGroup,
    AppsReportingCampaign,
    AppsReportingCreative,
    AppsReportingRequest,
    AppsSearchTermReportResponse,
    AppsSearchTermReportRow,
    AppsSearchTermResultContainer,
    ReportingAdGroupMin,
    ReportingCreativeSpec,
    ReportingKeyword,
    ReportingSearchTerm,
    ReportTimeZone,
)
from asa_api_client.v1.models.shared import TargetingData
from typer.testing import CliRunner

from asa_api_cli import main as main_mod
from asa_api_cli import reports, utils
from asa_api_cli.report_export import collect_app_report_export

runner = CliRunner()

APP_A_ID = 111000111
APP_B_ID = 222000222
EMPTY_APP_ID = 333000333
FIXED_NOW = datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC)
FIXED_TODAY = date(2026, 11, 16)


def _mock_client(ad_account_id: str = "123456") -> MagicMock:
    client = MagicMock()
    client.ad_account_id = ad_account_id
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.ad_accounts.get.return_value = AdAccount(
        id=int(ad_account_id),
        name="Peth App Studio",
        orgId=98765,
        currency="AUD",
        timezone="Australia/Brisbane",
        paymentModel="PAYG",
        systemStatus="ACTIVE",
    )
    client.apps.search.return_value = V1Page[AppInfo](
        result=[
            AppInfo(
                adamId=APP_A_ID,
                appName="Alpha Budget",
                developerName="Peth App Studio",
                countryOrRegionCodes=["AU", "US"],
            ),
            AppInfo(
                adamId=APP_B_ID,
                appName="Beta Habit",
                developerName="Peth App Studio",
                countryOrRegionCodes=["AU", "US"],
            ),
        ]
    )
    return client


def _campaign(
    cid: int,
    name: str,
    adam_id: int | str,
    *,
    promoted_type: str = "APPSTORE_APP",
    currency: str = "AUD",
    ad_account_id: int = 123456,
) -> Campaign:
    return Campaign(
        id=cid,
        adAccountId=ad_account_id,
        name=name,
        status="ENABLED",
        displayStatus="RUNNING",
        promotedObjectType=promoted_type,  # type: ignore[arg-type]
        promotedObjectId=str(adam_id),
        dailyBudget=DailyBudget(value=Money(amount="50.00", currency=currency)),
        targeting=CampaignTargeting(countryOrRegion=TargetingData(include=["AU"])),
        deleted=False,
    )


def _metrics(
    obs_date: date | None,
    *,
    impressions: int | None,
    taps: int | None,
    spend: str | None,
    currency: str = "AUD",
    tap_installs: int | None = 0,
    tap_new: int | None = 0,
    tap_redownloads: int | None = 0,
    view_installs: int | None = 0,
    view_new: int | None = 0,
    view_redownloads: int | None = 0,
    total_installs: int | None = 0,
    total_new: int | None = 0,
    total_redownloads: int | None = 0,
    tap_pre_orders: int | None = None,
) -> AppsMetrics:
    return AppsMetrics(
        date=obs_date,
        impressions=impressions,
        taps=taps,
        localSpend=Money(amount=spend, currency=currency) if spend is not None else None,
        tapInstalls=tap_installs,
        tapNewDownloads=tap_new,
        tapRedownloads=tap_redownloads,
        tapPreOrdersPlaced=tap_pre_orders,
        viewInstalls=view_installs,
        viewNewDownloads=view_new,
        viewRedownloads=view_redownloads,
        totalInstalls=total_installs,
        totalNewDownloads=total_new,
        totalRedownloads=total_redownloads,
    )


def _campaign_metrics(obs_date: date, **kwargs: Any) -> AppsCampaignMetrics:
    base = _metrics(obs_date, **kwargs)
    return AppsCampaignMetrics.model_validate(base.model_dump(by_alias=True))


def _ad_group_metrics(obs_date: date, **kwargs: Any) -> AppsAdGroupMetrics:
    base = _metrics(obs_date, **kwargs)
    return AppsAdGroupMetrics.model_validate(base.model_dump(by_alias=True))


def test_two_apps_in_one_account_exported_separately_and_reconciled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two apps in the same ad account export separately without mixing campaigns, IDs, or metrics."""
    client = _mock_client("123456")

    # Account holds App A (101, 102), App B (201), and an Apple Maps campaign (999)
    all_campaigns = [
        _campaign(101, "Alpha Brand AU", APP_A_ID),
        _campaign(102, "Alpha Generic AU", APP_A_ID),
        _campaign(201, "Beta Brand AU", APP_B_ID),
        _campaign(999, "Maps Brand", "brand_abc", promoted_type="BUSINESS_BRAND"),
    ]
    client.campaigns.query.return_value = V1Page[Campaign](
        result=all_campaigns,
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=4),
    )

    d1 = date(2026, 10, 1)
    d2 = date(2026, 10, 2)

    def fake_campaigns_report(req: AppsReportingRequest) -> AppsCampaignReportResponse:
        assert req.filters is not None
        requested = set(req.filters[0].value)  # type: ignore[arg-type]
        rows: list[AppsCampaignReportRow] = []
        if "101" in requested:
            rows.append(
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(
                        id=101,
                        name="Alpha Brand AU",
                        promotedObjectType="APPSTORE_APP",
                        promotedObjectId=str(APP_A_ID),
                        status="ENABLED",
                    ),
                    granularMetrics=[
                        _campaign_metrics(
                            d1,
                            impressions=1000,
                            taps=10,
                            spend="20.00",
                            tap_installs=2,
                            tap_new=1,
                            tap_redownloads=1,
                            view_installs=3,
                            view_new=2,
                            view_redownloads=1,
                            total_installs=5,
                            total_new=3,
                            total_redownloads=2,
                        ),
                        _campaign_metrics(
                            d2,
                            impressions=0,
                            taps=0,
                            spend="0.00",
                            tap_installs=0,
                            tap_new=0,
                            tap_redownloads=0,
                            view_installs=0,
                            view_new=0,
                            view_redownloads=0,
                            total_installs=0,
                            total_new=0,
                            total_redownloads=0,
                        ),
                    ],
                )
            )
        if "102" in requested:
            rows.append(
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(
                        id=102,
                        name="Alpha Generic AU",
                        promotedObjectType="APPSTORE_APP",
                        promotedObjectId=str(APP_A_ID),
                        status="ENABLED",
                    ),
                    granularMetrics=[
                        _campaign_metrics(
                            d1,
                            impressions=10,
                            taps=10,
                            spend="10.00",
                            tap_installs=8,
                            tap_new=6,
                            tap_redownloads=2,
                            view_installs=2,
                            view_new=2,
                            view_redownloads=0,
                            total_installs=10,
                            total_new=8,
                            total_redownloads=2,
                        )
                    ],
                )
            )
        if "201" in requested:
            rows.append(
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(
                        id=201,
                        name="Beta Brand AU",
                        promotedObjectType="APPSTORE_APP",
                        promotedObjectId=str(APP_B_ID),
                        status="ENABLED",
                    ),
                    granularMetrics=[
                        _campaign_metrics(
                            d1,
                            impressions=500,
                            taps=50,
                            spend="75.00",
                            tap_installs=25,
                            tap_new=20,
                            tap_redownloads=5,
                            view_installs=0,
                            view_new=0,
                            view_redownloads=0,
                            total_installs=25,
                            total_new=20,
                            total_redownloads=5,
                        )
                    ],
                )
            )
        return AppsCampaignReportResponse(
            result=AppsCampaignResultContainer(rows=rows),
            pagination=V1Pagination(offset=0, pageSize=5000, totalCount=len(rows)),
        )

    def fake_ad_groups_report(req: AppsReportingRequest) -> AppsAdGroupReportResponse:
        assert req.filters is not None
        cid = int(str(req.filters[0].value))
        if cid == 101:
            rows = [
                AppsAdGroupReportRow(
                    metadata=AppsReportingAdGroup(id=1010, campaignId=101, name="Brand Exact"),
                    granularMetrics=[
                        _ad_group_metrics(
                            d1,
                            impressions=1000,
                            taps=10,
                            spend="20.00",
                            tap_installs=2,
                            tap_new=1,
                            tap_redownloads=1,
                            view_installs=3,
                            view_new=2,
                            view_redownloads=1,
                            total_installs=5,
                            total_new=3,
                            total_redownloads=2,
                        )
                    ],
                )
            ]
        elif cid == 102:
            rows = [
                AppsAdGroupReportRow(
                    metadata=AppsReportingAdGroup(id=1020, campaignId=102, name="Generic Search Match"),
                    granularMetrics=[
                        _ad_group_metrics(
                            d1,
                            impressions=10,
                            taps=10,
                            spend="10.00",
                            tap_installs=8,
                            tap_new=6,
                            tap_redownloads=2,
                            view_installs=2,
                            view_new=2,
                            view_redownloads=0,
                            total_installs=10,
                            total_new=8,
                            total_redownloads=2,
                        )
                    ],
                )
            ]
        else:
            rows = [
                AppsAdGroupReportRow(
                    metadata=AppsReportingAdGroup(id=2010, campaignId=201, name="Beta Exact"),
                    granularMetrics=[
                        _ad_group_metrics(
                            d1,
                            impressions=500,
                            taps=50,
                            spend="75.00",
                            tap_installs=25,
                            tap_new=20,
                            tap_redownloads=5,
                            view_installs=0,
                            view_new=0,
                            view_redownloads=0,
                            total_installs=25,
                            total_new=20,
                            total_redownloads=5,
                        )
                    ],
                )
            ]
        return AppsAdGroupReportResponse(
            result=AppsAdGroupResultContainer(rows=rows),
            pagination=V1Pagination(offset=0, pageSize=5000, totalCount=len(rows)),
        )

    def fake_ads_report(req: AppsReportingRequest) -> AppsAdReportResponse:
        assert req.filters is not None
        cid = int(str(req.filters[0].value))
        rows: list[AppsAdReportRow] = []
        if cid == 101:
            rows.append(
                AppsAdReportRow(
                    metadata=AppsReportingAd(
                        id=5001,
                        campaignId=101,
                        adGroupId=1010,
                        name="Default Ad",
                        creative=AppsReportingCreative(
                            id=8001,
                            creativeType=AppsCreativeType.DEFAULT_PRODUCT_PAGE,
                            creativeSpec=ReportingCreativeSpec(language="en-AU"),
                        ),
                    ),
                    granularMetrics=[
                        _metrics(
                            d1,
                            impressions=1000,
                            taps=10,
                            spend="20.00",
                            tap_installs=2,
                            tap_new=1,
                            tap_redownloads=1,
                            view_installs=3,
                            view_new=2,
                            view_redownloads=1,
                            total_installs=5,
                            total_new=3,
                            total_redownloads=2,
                        )
                    ],
                )
            )
        return AppsAdReportResponse(
            result=AppsAdResultContainer(rows=rows),
            pagination=V1Pagination(offset=0, pageSize=5000, totalCount=len(rows)),
        )

    def fake_keywords_report(req: AppsReportingRequest) -> AppsKeywordReportResponse:
        assert req.filters is not None
        cid = int(str(req.filters[0].value))
        rows: list[AppsKeywordReportRow] = []
        if cid == 101:
            rows.append(
                AppsKeywordReportRow(
                    metadata=ReportingKeyword(
                        id=7001,
                        campaignId=101,
                        adGroupId=1010,
                        text="alpha budget",
                        matchType=AppsKeywordMatchType.EXACT,
                        adGroup=ReportingAdGroupMin(name="Brand Exact"),
                    ),
                    granularMetrics=[
                        _metrics(
                            d1,
                            impressions=1000,
                            taps=10,
                            spend="20.00",
                            tap_installs=2,
                            tap_new=1,
                            tap_redownloads=1,
                            view_installs=3,
                            view_new=2,
                            view_redownloads=1,
                            total_installs=5,
                            total_new=3,
                            total_redownloads=2,
                        )
                    ],
                )
            )
        # cid 102 uses Search Match -> 0 bidded keywords, exercising suppressed/unallocated keyword reconciliation!
        return AppsKeywordReportResponse(
            result=AppsKeywordResultContainer(rows=rows),
            pagination=V1Pagination(offset=0, pageSize=5000, totalCount=len(rows)),
        )

    def fake_search_terms_report(req: AppsReportingRequest) -> AppsSearchTermReportResponse:
        assert req.filters is not None
        cid = int(str(req.filters[0].value))
        rows: list[AppsSearchTermReportRow] = []
        if cid == 101:
            # Privacy threshold suppressed 100 impressions of low-volume queries
            rows.append(
                AppsSearchTermReportRow(
                    metadata=ReportingSearchTerm(
                        campaignId=101,
                        adGroupId=1010,
                        searchTermText="alpha budget app",
                        searchTermSource="TARGETED",
                        keyword=ReportingKeyword(
                            id=7001,
                            text="alpha budget",
                            matchType=AppsKeywordMatchType.EXACT,
                        ),
                    ),
                    granularMetrics=[
                        _metrics(
                            d1,
                            impressions=900,
                            taps=9,
                            spend="18.00",
                            tap_installs=2,
                            tap_new=1,
                            tap_redownloads=1,
                            view_installs=2,
                            view_new=1,
                            view_redownloads=1,
                            total_installs=4,
                            total_new=2,
                            total_redownloads=2,
                        )
                    ],
                )
            )
        return AppsSearchTermReportResponse(
            result=AppsSearchTermResultContainer(rows=rows),
            pagination=V1Pagination(offset=0, pageSize=5000, totalCount=len(rows)),
        )

    client.reports.campaigns.side_effect = fake_campaigns_report
    client.reports.ad_groups.side_effect = fake_ad_groups_report
    client.reports.ads.side_effect = fake_ads_report
    client.reports.keywords.side_effect = fake_keywords_report
    client.reports.search_terms.side_effect = fake_search_terms_report

    monkeypatch.setattr(reports, "get_client", lambda: client)

    out_a = tmp_path / "app_a.json"
    out_b = tmp_path / "app_b.json"

    res_a = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-02",
            "--output",
            str(out_a),
        ],
    )
    assert res_a.exit_code == 0, res_a.output

    res_b = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_B_ID),
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-02",
            "--output",
            str(out_b),
        ],
    )
    assert res_b.exit_code == 0, res_b.output

    data_a = json.loads(out_a.read_text(encoding="utf-8"))
    data_b = json.loads(out_b.read_text(encoding="utf-8"))

    # 1. App isolation & metadata contract
    assert data_a["status"] == "complete"
    assert data_a["complete"] is True
    assert data_a["account"]["ad_account_id"] == 123456
    assert data_a["account"]["currency"] == "AUD"
    assert data_a["account"]["account_timezone"] == "Australia/Brisbane"
    assert data_a["app"] == {"adam_id": APP_A_ID, "app_name": "Alpha Budget"}
    assert data_a["scope"]["campaign_ids"] == [101, 102]
    assert {r["campaign_id"] for r in data_a["levels"]["campaigns"]["daily"]} == {101, 102}

    assert data_b["status"] == "complete"
    assert data_b["app"] == {"adam_id": APP_B_ID, "app_name": "Beta Habit"}
    assert data_b["scope"]["campaign_ids"] == [201]
    assert {r["campaign_id"] for r in data_b["levels"]["campaigns"]["daily"]} == {201}

    # 2. Attribution correctness & non-averaged period ratios on App A summary:
    # Campaign 101 Day 1: imp=1000, taps=10, spend=20.00, tap_inst=2 (cpi=10.00), view=3, total=5 (cpi=4.00)
    # Campaign 101 Day 2: imp=0, taps=0, spend=0.00 (zero-spend day!)
    # Campaign 102 Day 1: imp=10, taps=10, spend=10.00, tap_inst=8 (cpi=1.25), view=2, total=10 (cpi=1.00)
    # Summed App A: imp=1010, taps=20, spend="30.00", tap_inst=10, view_inst=5, total_inst=15
    camp_sum = data_a["levels"]["campaigns"]["summary"]
    assert camp_sum["impressions"] == 1010
    assert camp_sum["taps"] == 20
    assert camp_sum["local_spend"] == "30.00"
    assert camp_sum["currency"] == "AUD"
    assert camp_sum["tap_installs"] == 10
    assert camp_sum["tap_new_downloads"] == 7
    assert camp_sum["tap_redownloads"] == 3
    assert camp_sum["view_installs"] == 5
    assert camp_sum["view_new_downloads"] == 4
    assert camp_sum["view_redownloads"] == 1
    assert camp_sum["total_installs"] == 15
    assert camp_sum["total_new_downloads"] == 11
    assert camp_sum["total_redownloads"] == 4
    # Unsupported/omitted pre-orders remain null, never fabricated zero
    assert camp_sum["tap_pre_orders_placed"] is None

    # Period ratios MUST equal period_num / period_den (NOT mean of daily ratios!):
    # TTR = 20 / 1010 = 0.019802 (whereas mean of daily TTRs 0.01 and 1.00 would be 0.505!)
    assert camp_sum["ttr"] == round(20 / 1010, 6)
    # Tap CPI = 30.00 / 10 = "3.00" (NOT mean of 10.00 and 1.25 = 5.625!)
    assert camp_sum["tap_install_cpi"] == "3.00"
    assert camp_sum["tap_install_rate"] == 0.5  # 10 / 20
    # Total CPI = 30.00 / 15 = "2.00"
    assert camp_sum["total_avg_cpi"] == "2.00"
    assert camp_sum["total_install_rate"] == 0.75  # 15 / 20

    # Zero-spend day row preserves valid 0 / "0.00" while zero-denominator CPI/rates are None
    zero_day = next(
        r for r in data_a["levels"]["campaigns"]["daily"] if r["campaign_id"] == 101 and r["date"] == "2026-10-02"
    )
    assert zero_day["impressions"] == 0
    assert zero_day["taps"] == 0
    assert zero_day["local_spend"] == "0.00"
    assert zero_day["tap_installs"] == 0
    assert zero_day["tap_install_cpi"] is None
    assert zero_day["tap_install_rate"] is None
    assert zero_day["total_avg_cpi"] is None

    # 3. Daily-to-period reconciliation & cross-level coverage explanations
    rec = data_a["reconciliation"]
    assert rec["daily_to_period_matches"] is True
    assert rec["levels"]["ad_groups"]["vs_campaigns"]["matches_campaigns"] is True
    assert rec["levels"]["keywords"]["vs_campaigns"]["matches_campaigns"] is False
    assert rec["levels"]["keywords"]["vs_campaigns"]["deltas"]["local_spend"] == "-10.00"
    assert "Search Match" in rec["levels"]["keywords"]["vs_campaigns"]["explanation"]
    assert rec["levels"]["search_terms"]["vs_campaigns"]["matches_campaigns"] is False
    assert "privacy thresholds" in rec["levels"]["search_terms"]["vs_campaigns"]["explanation"]

    # 4. Repeat fixed-period export produces fresh collected_at over identical period for restatement diffing
    res_a_repeat = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-10-01",
            "--end",
            "2026-10-02",
        ],
    )
    assert res_a_repeat.exit_code == 0
    repeat_data = json.loads(res_a_repeat.stdout)
    assert repeat_data["period"] == data_a["period"]
    assert repeat_data["levels"]["campaigns"]["summary"] == camp_sum


def test_multi_page_pagination_and_renamed_entities_across_windows() -> None:
    """Multi-page campaign + report queries and renamed entities across >90-day windows reconcile by ID."""
    client = _mock_client("123456")

    # 2 pages of campaigns (campaign_page_size=1)
    def paged_campaigns_query(query: Any) -> V1Page[Campaign]:
        payload = query.to_payload()
        offset = payload["pagination"]["offset"]
        items = [
            _campaign(101, "Alpha Brand - Nov Name", APP_A_ID),
            _campaign(201, "Beta Brand", APP_B_ID),
        ]
        batch = items[offset : offset + 1]
        return V1Page[Campaign](
            result=batch,
            pagination=V1Pagination(offset=offset, pageSize=1, totalCount=2),
        )

    client.campaigns.query.side_effect = paged_campaigns_query

    # 100-day range: 2026-08-01..2026-11-08 -> 2 windows:
    # Window 1: 2026-08-01..2026-10-29 (campaign named "Alpha Brand - Aug Name")
    # Window 2: 2026-10-30..2026-11-08 (campaign named "Alpha Brand - Oct Name")
    def paged_campaigns_report(req: AppsReportingRequest) -> AppsCampaignReportResponse:
        assert req.time_range is not None
        win_start = req.time_range.start
        if win_start == date(2026, 8, 1):
            row = AppsCampaignReportRow(
                metadata=AppsReportingCampaign(id=101, name="Alpha Brand - Aug Name"),
                granularMetrics=[
                    _campaign_metrics(
                        date(2026, 9, 1),
                        impressions=200,
                        taps=20,
                        spend="40.00",
                        tap_installs=5,
                        total_installs=5,
                    )
                ],
            )
        else:
            row = AppsCampaignReportRow(
                metadata=AppsReportingCampaign(id=101, name="Alpha Brand - Oct Name"),
                granularMetrics=[
                    _campaign_metrics(
                        date(2026, 11, 1),
                        impressions=300,
                        taps=30,
                        spend="60.00",
                        tap_installs=15,
                        total_installs=15,
                    )
                ],
            )
        return AppsCampaignReportResponse(
            result=AppsCampaignResultContainer(rows=[row]),
            pagination=V1Pagination(offset=0, pageSize=1, totalCount=1),
        )

    # Keywords report spans 3 pages (page_size=1, totalCount=3) per window
    def paged_keywords_report(req: AppsReportingRequest) -> AppsKeywordReportResponse:
        assert req.pagination is not None
        assert req.time_range is not None
        offset = req.pagination.offset or 0
        if req.time_range.start == date(2026, 8, 1):
            all_kw = [
                AppsKeywordReportRow(
                    metadata=ReportingKeyword(id=701 + i, campaignId=101, adGroupId=1010, text=f"kw-{i}"),
                    granularMetrics=[
                        _metrics(
                            date(2026, 9, 1),
                            impressions=50,
                            taps=5,
                            spend="10.00",
                            tap_installs=1,
                            total_installs=1,
                        )
                    ],
                )
                for i in range(3)
            ]
            batch = all_kw[offset : offset + 1]
            return AppsKeywordReportResponse(
                result=AppsKeywordResultContainer(rows=batch),
                pagination=V1Pagination(offset=offset, pageSize=1, totalCount=3),
            )
        return AppsKeywordReportResponse(
            result=AppsKeywordResultContainer(rows=[]),
            pagination=V1Pagination(offset=0, pageSize=1, totalCount=0),
        )

    client.reports.campaigns.side_effect = paged_campaigns_report
    client.reports.ad_groups.return_value = AppsAdGroupReportResponse(
        result=AppsAdGroupResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=1, totalCount=0),
    )
    client.reports.ads.return_value = AppsAdReportResponse(
        result=AppsAdResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=1, totalCount=0),
    )
    client.reports.keywords.side_effect = paged_keywords_report
    client.reports.search_terms.return_value = AppsSearchTermReportResponse(
        result=AppsSearchTermResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=1, totalCount=0),
    )

    payload = collect_app_report_export(
        client,
        adam_id=APP_A_ID,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 11, 8),
        page_size=1,
        campaign_page_size=1,
        today=FIXED_TODAY,
        collected_at=FIXED_NOW,
    )

    assert payload["status"] == "complete"
    assert payload["complete"] is True
    assert payload["scope"]["pagination"]["pages_fetched"] == 2
    assert payload["scope"]["pagination"]["rows_fetched"] == 2

    # Renamed campaign reconciled into ONE period_totals row keyed by campaign_id=101
    camp_totals = payload["levels"]["campaigns"]["period_totals"]
    assert len(camp_totals) == 1
    assert camp_totals[0]["campaign_id"] == 101
    assert camp_totals[0]["local_spend"] == "100.00"
    assert camp_totals[0]["tap_installs"] == 20
    assert set(camp_totals[0]["observed_names"]) == {
        "Alpha Brand - Nov Name",
        "Alpha Brand - Aug Name",
        "Alpha Brand - Oct Name",
    }

    # Keywords fetched 3 pages in window 1 + 1 empty page in window 2 = 4 pages, 3 rows
    kw_pag = payload["levels"]["keywords"]["pagination"]
    assert kw_pag["complete"] is True
    assert kw_pag["pages_fetched"] == 4
    assert kw_pag["api_rows_fetched"] == 3
    assert len(payload["levels"]["keywords"]["period_totals"]) == 3

    # Search terms clipped to trailing 90 days (2026-08-18..2026-11-08) with explicit reason
    st_sec = payload["levels"]["search_terms"]
    assert st_sec["reason"] == "clipped_to_lookback_window"
    assert st_sec["actual_start"] == "2026-08-18"
    assert payload["reconciliation"]["levels"]["search_terms"]["vs_campaigns"]["comparable"] is False


def test_missing_metrics_and_partial_day_null_safety() -> None:
    """Partially missing daily metrics remain null in period totals rather than fabricating zeroes."""
    client = _mock_client("123456")
    client.campaigns.query.return_value = V1Page[Campaign](
        result=[_campaign(101, "Alpha", APP_A_ID)],
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=1),
    )

    # Day 1 has view_installs=2, Day 2 has view_installs=None (missing), total_installs=None on both
    client.reports.campaigns.return_value = AppsCampaignReportResponse(
        result=AppsCampaignResultContainer(
            rows=[
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(id=101, name="Alpha"),
                    granularMetrics=[
                        _campaign_metrics(
                            date(2026, 11, 1),
                            impressions=100,
                            taps=10,
                            spend="15.00",
                            tap_installs=3,
                            view_installs=2,
                            total_installs=None,
                        ),
                        _campaign_metrics(
                            date(2026, 11, 2),
                            impressions=100,
                            taps=10,
                            spend="15.00",
                            tap_installs=3,
                            view_installs=None,
                            total_installs=None,
                        ),
                    ],
                )
            ]
        ),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=1),
    )
    client.reports.ad_groups.return_value = AppsAdGroupReportResponse(
        result=AppsAdGroupResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    client.reports.ads.return_value = AppsAdReportResponse(
        result=AppsAdResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    client.reports.keywords.return_value = AppsKeywordReportResponse(
        result=AppsKeywordResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    client.reports.search_terms.return_value = AppsSearchTermReportResponse(
        result=AppsSearchTermResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )

    payload = collect_app_report_export(
        client,
        adam_id=APP_A_ID,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        today=FIXED_TODAY,
        collected_at=FIXED_NOW,
    )

    summary = payload["levels"]["campaigns"]["summary"]
    assert summary["tap_installs"] == 6
    assert summary["tap_install_cpi"] == "5.00"
    # view_installs was missing on 1 of 2 days -> period total is null + note recorded
    assert summary["view_installs"] is None
    # total_installs was missing on all days -> total_installs, total_avg_cpi, total_install_rate all null
    assert summary["total_installs"] is None
    assert summary["total_avg_cpi"] is None
    assert summary["total_install_rate"] is None
    assert any("view_installs" in n for n in payload["levels"]["campaigns"]["notes"])


def test_timezone_utc_prevents_mixing_ortz_search_terms_and_lookback_unavailable() -> None:
    """UTC export marks search_terms unavailable rather than silently mixing ORTZ daily rows."""
    client = _mock_client("123456")
    client.campaigns.query.return_value = V1Page[Campaign](
        result=[_campaign(101, "Alpha", APP_A_ID)],
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=1),
    )
    client.reports.campaigns.return_value = AppsCampaignReportResponse(
        result=AppsCampaignResultContainer(
            rows=[
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(id=101, name="Alpha"),
                    granularMetrics=[
                        _campaign_metrics(
                            date(2026, 1, 10),
                            impressions=100,
                            taps=10,
                            spend="12.00",
                            tap_installs=2,
                            total_installs=2,
                        )
                    ],
                )
            ]
        ),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=1),
    )
    client.reports.ad_groups.return_value = AppsAdGroupReportResponse(
        result=AppsAdGroupResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    client.reports.ads.return_value = AppsAdReportResponse(
        result=AppsAdResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    client.reports.keywords.return_value = AppsKeywordReportResponse(
        result=AppsKeywordResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )

    utc_payload = collect_app_report_export(
        client,
        adam_id=APP_A_ID,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        requested_timezone=ReportTimeZone.UTC,
        today=FIXED_TODAY,
        collected_at=FIXED_NOW,
    )
    assert utc_payload["status"] == "complete"
    assert utc_payload["levels"]["campaigns"]["timezone"] == "UTC"
    assert utc_payload["levels"]["search_terms"]["status"] == "unavailable"
    assert utc_payload["levels"]["search_terms"]["reason"] == "timezone_incompatible"
    client.reports.search_terms.assert_not_called()

    # Historical ORTZ range older than 90 days marks search_terms unavailable due to outside_lookback_window
    old_ortz_payload = collect_app_report_export(
        client,
        adam_id=APP_A_ID,
        start_date=date(2026, 1, 10),
        end_date=date(2026, 1, 15),
        requested_timezone=ReportTimeZone.ORTZ,
        today=FIXED_TODAY,
        collected_at=FIXED_NOW,
    )
    assert old_ortz_payload["status"] == "complete"
    assert old_ortz_payload["levels"]["search_terms"]["status"] == "unavailable"
    assert old_ortz_payload["levels"]["search_terms"]["reason"] == "outside_lookback_window"
    client.reports.search_terms.assert_not_called()


def test_empty_partial_and_failed_collections_are_distinguishable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty, partial, and failed runs overwrite stale files and set distinct status + exit codes."""
    out_file = tmp_path / "export.json"
    out_file.write_text('{"stale": true}', encoding="utf-8")

    # Case A: Legitimate empty (no_campaigns_for_app) -> exit 0, status="empty", overwrites stale file
    empty_client = _mock_client("123456")
    empty_client.campaigns.query.return_value = V1Page[Campaign](
        result=[_campaign(201, "Beta", APP_B_ID)],
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=1),
    )
    monkeypatch.setattr(reports, "get_client", lambda: empty_client)

    res_empty_app = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(EMPTY_APP_ID),
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-07",
            "-o",
            str(out_file),
        ],
    )
    assert res_empty_app.exit_code == 0
    empty_doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert "stale" not in empty_doc
    assert empty_doc["status"] == "empty"
    assert empty_doc["complete"] is True
    assert empty_doc["empty_reason"] == "no_campaigns_for_app"

    # Case B: Legitimate empty (no_metrics_in_period) -> exit 0, status="empty"
    no_metrics_client = _mock_client("123456")
    no_metrics_client.campaigns.query.return_value = V1Page[Campaign](
        result=[_campaign(101, "Alpha", APP_A_ID)],
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=1),
    )
    no_metrics_client.reports.campaigns.return_value = AppsCampaignReportResponse(
        result=AppsCampaignResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    no_metrics_client.reports.ad_groups.return_value = AppsAdGroupReportResponse(
        result=AppsAdGroupResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    no_metrics_client.reports.ads.return_value = AppsAdReportResponse(
        result=AppsAdResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    no_metrics_client.reports.keywords.return_value = AppsKeywordReportResponse(
        result=AppsKeywordResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    no_metrics_client.reports.search_terms.return_value = AppsSearchTermReportResponse(
        result=AppsSearchTermResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    monkeypatch.setattr(reports, "get_client", lambda: no_metrics_client)

    res_no_metrics = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-07",
            "-o",
            str(out_file),
        ],
    )
    assert res_no_metrics.exit_code == 0
    no_metrics_doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert no_metrics_doc["status"] == "empty"
    assert no_metrics_doc["complete"] is True
    assert no_metrics_doc["empty_reason"] == "no_metrics_in_period"

    # Case C: Partial failure (campaigns succeeds, keywords raises RateLimitError, ads truncates pagination)
    # Must exit 1 and mark status="partial", complete=False!
    partial_client = _mock_client("123456")
    partial_client.campaigns.query.return_value = V1Page[Campaign](
        result=[_campaign(101, "Alpha", APP_A_ID)],
        pagination=V1Pagination(offset=0, pageSize=500, totalCount=1),
    )
    partial_client.reports.campaigns.return_value = AppsCampaignReportResponse(
        result=AppsCampaignResultContainer(
            rows=[
                AppsCampaignReportRow(
                    metadata=AppsReportingCampaign(id=101, name="Alpha"),
                    granularMetrics=[
                        _campaign_metrics(
                            date(2026, 11, 1),
                            impressions=100,
                            taps=10,
                            spend="10.00",
                            tap_installs=2,
                            total_installs=2,
                        )
                    ],
                )
            ]
        ),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=1),
    )
    partial_client.reports.ad_groups.return_value = AppsAdGroupReportResponse(
        result=AppsAdGroupResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    # Ads reports totalCount=3 on page 1 (1 row) then returns [] on page 2 -> IncompletePaginationError!
    partial_client.reports.ads.side_effect = [
        AppsAdReportResponse(
            result=AppsAdResultContainer(
                rows=[
                    AppsAdReportRow(
                        metadata=AppsReportingAd(id=501, campaignId=101, adGroupId=1010, name="Ad 1"),
                        granularMetrics=[_metrics(date(2026, 11, 1), impressions=50, taps=5, spend="5.00")],
                    )
                ]
            ),
            pagination=V1Pagination(offset=0, pageSize=1, totalCount=3),
        ),
        AppsAdReportResponse(
            result=AppsAdResultContainer(rows=[]),
            pagination=V1Pagination(offset=1, pageSize=1, totalCount=3),
        ),
    ]
    partial_client.reports.keywords.side_effect = RateLimitError("Rate limited", status_code=429)
    partial_client.reports.search_terms.return_value = AppsSearchTermReportResponse(
        result=AppsSearchTermResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    monkeypatch.setattr(reports, "get_client", lambda: partial_client)

    res_partial = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-07",
            "--page-size",
            "1",
            "-o",
            str(out_file),
        ],
    )
    assert res_partial.exit_code == utils.EXIT_ERROR
    partial_doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert partial_doc["status"] == "partial"
    assert partial_doc["complete"] is False
    assert partial_doc["levels"]["ads"]["status"] == "partial"
    assert partial_doc["levels"]["keywords"]["status"] == "failed"
    err_types = {e["error_type"] for e in partial_doc["errors"]}
    assert {"IncompletePaginationError", "RateLimitError"} <= err_types

    # Case D: Full failure (campaigns.query raises ServerError) -> exit 1, status="failed"
    failed_client = _mock_client("123456")
    failed_client.campaigns.query.side_effect = ServerError("Upstream 503", status_code=503)
    monkeypatch.setattr(reports, "get_client", lambda: failed_client)

    res_failed = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-07",
            "-o",
            str(out_file),
        ],
    )
    assert res_failed.exit_code == utils.EXIT_ERROR
    failed_doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert failed_doc["status"] == "failed"
    assert failed_doc["complete"] is False
    assert failed_doc["errors"][0]["error_type"] == "ServerError"


def test_basic_reports_attribution_consistency_and_empty_file_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Basic report commands align avg_cpa with tap_installs and overwrite stale files on empty data."""
    row = AppsCampaignReportRow(
        metadata=AppsReportingCampaign(
            id=101,
            adAccountId=123456,
            name="Alpha Brand",
            promotedObjectType="APPSTORE_APP",
            promotedObjectId=str(APP_A_ID),
        ),
        totalMetrics=_campaign_metrics(
            date(2026, 11, 1),
            impressions=100,
            taps=10,
            spend="20.00",
            tap_installs=2,
            view_installs=3,
            total_installs=5,
        ),
    )
    flat = reports.report_row_to_dict(row, time_zone=ReportTimeZone.UTC)
    assert flat["campaign_id"] == 101
    assert flat["adam_id"] == APP_A_ID
    assert flat["time_zone"] == "UTC"
    assert flat["currency"] == "AUD"
    assert flat["attribution_basis"] == "tap"
    # Tap basis: 20.00 / 2 = "10.00" (never total_avg_cpi = "4.00"!)
    assert flat["installs"] == 2
    assert flat["avg_cpa"] == "10.00"
    assert flat["tap_install_cpi"] == "10.00"
    assert flat["total_installs"] == 5
    assert flat["total_avg_cpi"] == "4.00"

    # Empty basic report with -o overwrites pre-existing JSON and CSV files
    client = _mock_client("123456")
    client.reports.campaigns.return_value = AppsCampaignReportResponse(
        result=AppsCampaignResultContainer(rows=[]),
        pagination=V1Pagination(offset=0, pageSize=5000, totalCount=0),
    )
    monkeypatch.setattr(reports, "get_client", lambda: client)

    stale_json = tmp_path / "basic.json"
    stale_csv = tmp_path / "basic.csv"
    stale_json.write_text('[{"old": 1}]', encoding="utf-8")
    stale_csv.write_text("old_col\nold_val\n", encoding="utf-8")

    r_json = runner.invoke(
        main_mod.app,
        ["reports", "campaigns", "-s", "2026-11-01", "-e", "2026-11-07", "-o", str(stale_json)],
    )
    assert r_json.exit_code == 0
    assert json.loads(stale_json.read_text(encoding="utf-8")) == []

    r_csv = runner.invoke(
        main_mod.app,
        ["reports", "campaigns", "-s", "2026-11-01", "-e", "2026-11-07", "-o", str(stale_csv)],
    )
    assert r_csv.exit_code == 0
    with stale_csv.open(encoding="utf-8") as handle:
        parsed_csv = list(csv.reader(handle))
    assert len(parsed_csv) == 1
    assert "campaign_id" in parsed_csv[0]


def test_reporting_path_invokes_only_read_only_http_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end transport test proving zero mutating HTTP endpoints are called during export."""
    observed_requests: list[tuple[str, str, dict[str, str]]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        observed_requests.append((request.method, path, dict(request.headers)))

        if request.method == "GET" and path == "/v1/ad-accounts/123456":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "id": 123456,
                        "name": "ReadOnly Test Account",
                        "orgId": 999,
                        "currency": "USD",
                        "timezone": "America/Los_Angeles",
                        "systemStatus": "ACTIVE",
                    }
                },
            )
        if request.method == "POST" and path == "/v1/campaigns/query":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 101,
                            "adAccountId": 123456,
                            "name": "ReadOnly Campaign",
                            "status": "ENABLED",
                            "promotedObjectType": "APPSTORE_APP",
                            "promotedObjectId": str(APP_A_ID),
                        }
                    ],
                    "pagination": {"offset": 0, "pageSize": 500, "totalCount": 1},
                },
            )
        if request.method == "POST" and path == "/v1/search/apps":
            return httpx.Response(
                200,
                json={"result": [{"adamId": APP_A_ID, "appName": "Alpha Budget"}]},
            )
        if request.method == "POST" and path == "/v1/reports/apps/campaigns/query":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "rows": [
                            {
                                "metadata": {
                                    "id": 101,
                                    "adAccountId": 123456,
                                    "name": "ReadOnly Campaign",
                                    "promotedObjectType": "APPSTORE_APP",
                                    "promotedObjectId": str(APP_A_ID),
                                },
                                "granularMetrics": [
                                    {
                                        "date": "2026-11-01",
                                        "impressions": 100,
                                        "taps": 10,
                                        "localSpend": {"amount": "15.00", "currency": "USD"},
                                        "tapInstalls": 3,
                                        "viewInstalls": 1,
                                        "totalInstalls": 4,
                                    }
                                ],
                            }
                        ]
                    },
                    "pagination": {"offset": 0, "pageSize": 5000, "totalCount": 1},
                },
            )
        if request.method == "POST" and path.startswith("/v1/reports/apps/") and path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "result": {"rows": []},
                    "pagination": {"offset": 0, "pageSize": 5000, "totalCount": 0},
                },
            )
        raise AssertionError(f"Unexpected or mutating HTTP call: {request.method} {path}")

    # Instantiate a real AppleAdsClient with fake token + MockTransport (no real keys/network)
    real_client = AppleAdsClient.__new__(AppleAdsClient)
    real_client.ad_account_id = "123456"
    real_client._base_url = "https://api.ads.apple.com/v1"
    real_client._timeout = 10.0
    real_client.max_retries = 0
    real_client.retry_initial_delay = 0.0
    real_client.retry_max_delay = 0.0
    real_client.retry_jitter = False
    fake_auth = MagicMock()
    fake_auth.get_access_token.return_value = MagicMock(authorization_header="Bearer test-token")
    real_client._authenticator = fake_auth
    real_client._http_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    real_client._async_http_client = None
    real_client._owns_http_clients = True
    real_client._parent_client = None
    real_client._init_resources()

    monkeypatch.setattr(reports, "get_client", lambda: real_client)

    res = runner.invoke(
        main_mod.app,
        [
            "reports",
            "export",
            "--app",
            str(APP_A_ID),
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-01",
        ],
    )
    assert res.exit_code == 0, res.output
    parsed = json.loads(res.stdout)
    assert parsed["status"] == "complete"
    assert parsed["levels"]["campaigns"]["summary"]["tap_install_cpi"] == "5.00"
    assert parsed["levels"]["campaigns"]["summary"]["total_avg_cpi"] == "3.75"

    # Every single HTTP request was strictly read-only (GET or POST */query or /v1/search/apps)
    assert len(observed_requests) == 8
    for method, path, headers in observed_requests:
        assert method in ("GET", "POST")
        if method == "POST":
            assert path.endswith("/query") or path == "/v1/search/apps"
        assert headers.get("x-ap-context") == "adAccountId=123456"
