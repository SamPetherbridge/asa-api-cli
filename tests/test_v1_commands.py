"""Unit tests for Apple Ads Platform API v1 CLI commands."""

import json
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from asa_api_client.exceptions import ConfigurationError
from asa_api_client.v1.models.ad_accounts import AclAdAccount, AdAccount, Me, UserAcl
from asa_api_client.v1.models.ad_groups import AdGroup
from asa_api_client.v1.models.ads import Ad
from asa_api_client.v1.models.apps import (
    AppDetails,
    AppInfo,
    AppSupportedLanguages,
    EligibilityResponse,
    LocaleInfo,
)
from asa_api_client.v1.models.base import Money, V1Page, V1Pagination
from asa_api_client.v1.models.budget_orders import SharedBudget
from asa_api_client.v1.models.bulk import BulkItemResultKeyword
from asa_api_client.v1.models.campaigns import (
    Campaign,
    CampaignTargeting,
    DailyBudget,
)
from asa_api_client.v1.models.change_history import (
    ActivityChange,
    ActivityDetail,
    AuditSummary,
    AuditSummaryMeta,
    ChangeDetails,
)
from asa_api_client.v1.models.creatives import Creative
from asa_api_client.v1.models.geo import SearchEntity
from asa_api_client.v1.models.insights import ImpressionShareRow, SearchTermPopularityRow
from asa_api_client.v1.models.keywords import Keyword, NegativeKeyword
from asa_api_client.v1.models.product_pages import AppLocaleDetails
from asa_api_client.v1.models.recommendations import (
    DailyCapRecommendation,
    DailyCapRecommendationHistory,
    RecommendationBidStrategy,
    TargetCpaRecommendation,
    TargetCpaRecommendationHistory,
)
from asa_api_client.v1.models.reports import (
    AppsCampaignMetrics,
    AppsCampaignReportRow,
    AppsMetrics,
    AppsReportingCampaign,
    AppsSearchTermReportResponse,
    AppsSearchTermReportRow,
    AppsSearchTermResultContainer,
    ReportingSearchTerm,
    ReportTimeZone,
)
from asa_api_client.v1.models.shared import BidStrategy, BidStrategyGoal, BidStrategyType, TargetingData
from asa_api_client.v1.models.suggestions import (
    CategorySuggestion,
    KeywordSuggestion,
    PhraseSuggestion,
    TargetCpaSuggestion,
)
from typer.testing import CliRunner

from asa_api_cli import (
    ad_groups,
    ads,
    apps,
    auth,
    brand,
    budget_orders,
    campaigns,
    change_history,
    countries,
    geo,
    impression_share,
    keywords,
    optimize,
    product_pages,
    recommendations,
    reports,
    suggestions,
    utils,
)
from asa_api_cli import (
    main as main_mod,
)

runner = CliRunner()


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    return client


def _sample_campaign(campaign_id: int = 101) -> Campaign:
    return Campaign(
        id=campaign_id,
        name="US Brand",
        status="ENABLED",
        displayStatus="RUNNING",
        promotedObjectType="APPSTORE_APP",
        promotedObjectId="999000111",
        dailyBudget=DailyBudget(value=Money(amount="50.00", currency="USD")),
        targeting=CampaignTargeting(countryOrRegion=TargetingData(include=["US", "CA"])),
    )


def _sample_ad_group(
    ad_group_id: int = 202,
    campaign_id: int = 101,
    strategy_type: str = "MANUAL_CPT",
    amount: str = "2.50",
) -> AdGroup:
    goal = BidStrategyGoal.TAP if strategy_type == "MANUAL_CPT" else BidStrategyGoal.INSTALL
    return AdGroup(
        id=ad_group_id,
        campaignId=campaign_id,
        name="Exact AG",
        status="ENABLED",
        displayStatus="RUNNING",
        pricingModel="CPT",
        bidStrategy=BidStrategy(
            bidStrategyType=BidStrategyType(strategy_type),
            bidStrategyGoal=goal,
            bid=Money(amount=amount, currency="USD"),
        ),
        automatedKeywordsOptIn=False,
    )


def test_global_ad_account_override_passed_to_get_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Global --ad-account / -A binds the AppleAdsClient via with_account()."""
    base_client = _mock_client()
    base_client.ad_account_id = None
    scoped_client = _mock_client()
    scoped_client.ad_account_id = "777888"
    scoped_client.campaigns.query.return_value = V1Page[Campaign](
        result=[_sample_campaign()],
        pagination=V1Pagination(totalCount=1, offset=0, limit=100),
    )
    base_client.with_account.return_value = scoped_client

    monkeypatch.setattr(utils.AppleAdsClient, "from_env", lambda **_kwargs: base_client)

    res = runner.invoke(main_mod.app, ["--ad-account", "777888", "--json", "campaigns", "list"])
    assert res.exit_code == 0
    base_client.with_account.assert_called_once_with(777888)
    parsed = json.loads(res.stdout)
    assert parsed[0]["id"] == 101


def test_get_client_multi_account_error_exits_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """When resolve_ad_account raises ConfigurationError, get_client exits with EXIT_CONFIG (6)."""
    base_client = _mock_client()
    base_client.ad_account_id = None
    base_client.resolve_ad_account.side_effect = ConfigurationError("Multiple ad accounts available")
    monkeypatch.setattr(utils.AppleAdsClient, "from_env", lambda **_kwargs: base_client)

    res = runner.invoke(main_mod.app, ["campaigns", "list"])
    assert res.exit_code == utils.EXIT_CONFIG
    base_client.close.assert_called_once()


def test_auth_accounts_and_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth accounts and auth test query Platform API v1 /v1/acls and /v1/me."""
    client = _mock_client()
    client.ad_account_id = "123456"
    client.resolve_ad_account.return_value = "123456"
    client.with_account.return_value = client
    client.acls.list.return_value = [
        UserAcl(
            adAccount=AclAdAccount(id=123456, name="Acme Corp", orgId=123456),
            roles=["API Account Manager"],
        )
    ]
    client.ad_accounts.me.return_value = Me(userId=42, orgId=123456)
    client.ad_accounts.get.return_value = AdAccount(
        id=123456,
        name="Acme Corp",
        orgId=123456,
        currency="USD",
        paymentModel="PAYG",
        timezone="America/Los_Angeles",
        systemStatus="ACTIVE",
    )
    client.campaigns.query.return_value = V1Page[Campaign](
        result=[],
        pagination=V1Pagination(totalCount=4, offset=0, limit=1),
    )

    monkeypatch.setattr(auth, "get_client", lambda **_kwargs: client)
    res_acc = runner.invoke(main_mod.app, ["auth", "accounts", "--format", "json"])
    assert res_acc.exit_code == 0
    acc_data = json.loads(res_acc.stdout)
    assert acc_data[0]["ad_account_id"] == 123456
    assert acc_data[0]["name"] == "Acme Corp"

    monkeypatch.setattr(auth, "_load_auth_settings", lambda _env_file: MagicMock())
    monkeypatch.setattr(auth.AppleAdsClient, "from_env", lambda **_kwargs: client)
    res_test = runner.invoke(main_mod.app, ["auth", "test"])
    assert res_test.exit_code == 0
    assert "123456" in res_test.stdout


def test_campaigns_list_with_spend_and_set_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """campaigns list --with-spend filters report by field='id'; set-budget rejects --total and updates DailyBudget."""
    client = _mock_client()
    client.campaigns.query.return_value = V1Page[Campaign](
        result=[_sample_campaign(101)],
        pagination=V1Pagination(totalCount=1, offset=0, limit=100),
    )
    client.reports.iter_campaigns.return_value = iter(
        [
            AppsCampaignReportRow(
                metadata=AppsReportingCampaign(id=101, name="US Brand"),
                totalMetrics=AppsCampaignMetrics(localSpend=Money(amount="12.34", currency="USD")),
            )
        ]
    )
    monkeypatch.setattr(campaigns, "get_client", lambda: client)

    res = runner.invoke(main_mod.app, ["campaigns", "list", "--with-spend", "--format", "json"])
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert rows[0]["spend"] == "12.34 USD"
    rep_req = client.reports.iter_campaigns.call_args.args[0]
    assert rep_req.filters[0].field == "id"

    # --total is retired in Platform API v1
    res_total = runner.invoke(main_mod.app, ["campaigns", "set-budget", "101", "--total", "1000"])
    assert res_total.exit_code == utils.EXIT_USAGE

    # --daily updates DailyBudget
    client.campaigns.get.return_value = _sample_campaign(101)
    client.campaigns.update.return_value = _sample_campaign(101)
    res_daily = runner.invoke(main_mod.app, ["campaigns", "set-budget", "101", "--daily", "75.00"])
    assert res_daily.exit_code == 0
    upd_payload = client.campaigns.update.call_args.args[1]
    assert upd_payload.daily_budget.value.amount == "75.00"


def test_ad_groups_get_and_set_bid_mutual_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """ad-groups supports 1-arg IDs and enforces mutual exclusion of --bid and --target-cpa."""
    client = _mock_client()
    client.ad_groups.get.return_value = _sample_ad_group(202, 101)
    client.ad_groups.update.return_value = _sample_ad_group(202, 101, strategy_type="MAX_CONVERSIONS", amount="8.00")
    monkeypatch.setattr(ad_groups, "get_client", lambda: client)

    # Both --bid and --target-cpa rejected
    both = runner.invoke(main_mod.app, ["ad-groups", "set-bid", "202", "--bid", "2.0", "--target-cpa", "8.0"])
    assert both.exit_code == utils.EXIT_USAGE

    # Single-arg --target-cpa builds MAX_CONVERSIONS + INSTALL
    ok = runner.invoke(main_mod.app, ["ad-groups", "set-bid", "202", "--target-cpa", "8.0"])
    assert ok.exit_code == 0
    ag_upd = client.ad_groups.update.call_args.args[1]
    assert ag_upd.bid_strategy.bid_strategy_type == BidStrategyType.MAX_CONVERSIONS
    assert ag_upd.bid_strategy.bid_strategy_goal == BidStrategyGoal.INSTALL
    assert ag_upd.bid_strategy.bid.amount == "8.00"

    # Legacy 2-arg get (campaign_id, ad_group_id) resolves ad_group_id=202
    get_res = runner.invoke(main_mod.app, ["ad-groups", "get", "101", "202", "--format", "json"])
    assert get_res.exit_code == 0
    assert json.loads(get_res.stdout)["id"] == 202


def test_keywords_bulk_add_and_negatives_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """keywords add uses client.bulk.create_keywords; negatives list uses IS_NULL for campaign-level negatives."""
    client = _mock_client()
    client.ad_groups.get.return_value = _sample_ad_group(202, 101)
    client.bulk.create_keywords.return_value = [
        BulkItemResultKeyword(
            success=True,
            result=Keyword(id=303, adGroupId=202, text="habit tracker", matchType="EXACT", status="ENABLED"),
        )
    ]
    client.negative_keywords.query.return_value = V1Page[NegativeKeyword](
        result=[NegativeKeyword(id=404, campaignId=101, adGroupId=None, text="free", matchType="BROAD")],
        pagination=V1Pagination(totalCount=1, offset=0, limit=100),
    )
    monkeypatch.setattr(keywords, "get_client", lambda: client)

    add_res = runner.invoke(main_mod.app, ["keywords", "add", "habit tracker", "--ad-group", "202", "--bid", "2.75"])
    assert add_res.exit_code == 0
    client.bulk.create_keywords.assert_called_once()

    neg_res = runner.invoke(main_mod.app, ["keywords", "negatives", "list", "--campaign", "101", "--format", "json"])
    assert neg_res.exit_code == 0
    q_payload = client.negative_keywords.query.call_args.args[0].to_payload()
    assert {"field": "adGroupId", "operator": "IS_NULL"} in q_payload["filters"]
    assert json.loads(neg_res.stdout)[0]["text"] == "free"


def test_ads_create_auto_creates_creative(monkeypatch: pytest.MonkeyPatch) -> None:
    """ads create creates a Creative + Destination first when --creative-id is omitted."""
    client = _mock_client()
    client.campaigns.get.return_value = _sample_campaign(101)
    client.creatives.create.return_value = Creative(
        id=555,
        name="Holiday Ad",
        creativeType="CUSTOM_PRODUCT_PAGE",
    )
    client.ads.create.return_value = Ad(
        id=666,
        adGroupId=202,
        creativeId=555,
        name="Holiday Ad",
        status="ENABLED",
    )
    monkeypatch.setattr(ads, "get_client", lambda: client)

    res = runner.invoke(
        main_mod.app,
        [
            "ads",
            "create",
            "--campaign",
            "101",
            "--ad-group",
            "202",
            "--name",
            "Holiday Ad",
            "--type",
            "CUSTOM_PRODUCT_PAGE",
            "--product-page",
            "pp-holiday",
        ],
    )
    assert res.exit_code == 0
    client.creatives.create.assert_called_once()
    client.ads.create.assert_called_once()
    assert client.ads.create.call_args.args[0].creative_id == 555


def test_apps_geo_countries_product_pages_and_budget_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify metadata, lookup, product-page locale, and shared-budget v1 endpoints."""
    client = _mock_client()

    # apps search --own & eligibilities
    client.apps.search.return_value = V1Page[AppInfo](
        result=[AppInfo(adamId=999, appName="My App", developerName="Dev", countryOrRegionCodes=["US"])]
    )
    client.apps.get.return_value = AppDetails(
        id="999",
        appName="My App",
        artistName="Dev",
        primaryLanguage="en-US",
        availableStorefronts=["US", "GB"],
    )
    client.apps.query_eligibilities.return_value = V1Page[EligibilityResponse](
        result=[
            EligibilityResponse(
                adamId="999",
                state="ELIGIBLE",
                countryOrRegion="US",
                supplySource="APPSTORE",
                supplyPlacement="APPSTORE_SEARCH_RESULTS",
                deviceClass="IPHONE",
            )
        ]
    )
    monkeypatch.setattr(apps, "get_client", lambda: client)

    assert runner.invoke(main_mod.app, ["apps", "search", "--own", "--format", "json"]).exit_code == 0
    assert runner.invoke(main_mod.app, ["apps", "get", "999", "--format", "json"]).exit_code == 0
    elig_res = runner.invoke(main_mod.app, ["apps", "eligibilities", "999", "--format", "json"])
    assert elig_res.exit_code == 0
    assert json.loads(elig_res.stdout)[0]["state"] == "ELIGIBLE"

    # geo search
    client.geo.search.return_value = V1Page[SearchEntity](
        result=[SearchEntity(id="US|CA|SF", displayName="San Francisco", entity="Locality", countryOrRegion="US")]
    )
    monkeypatch.setattr(geo, "get_client", lambda: client)
    geo_res = runner.invoke(main_mod.app, ["geo", "search", "San Francisco", "--country", "US", "--format", "json"])
    assert geo_res.exit_code == 0
    assert json.loads(geo_res.stdout)[0]["id"] == "US|CA|SF"

    # countries list
    client.apps.query_supported_languages.return_value = V1Page[AppSupportedLanguages](
        result=[
            AppSupportedLanguages(
                countryCode="US",
                name="United States",
                adsDefaultLanguages=[LocaleInfo(languageCode="en-US", language="en")],
                adsSupportedLanguages=[LocaleInfo(languageCode="es-MX", language="es")],
            )
        ]
    )
    monkeypatch.setattr(countries, "get_client", lambda: client)
    cnt_res = runner.invoke(main_mod.app, ["countries", "list", "--country", "US", "--format", "json"])
    assert cnt_res.exit_code == 0
    assert json.loads(cnt_res.stdout)[0]["country_code"] == "US"

    # product-pages app-locales
    client.product_pages.iter_app_locale_details.return_value = iter(
        [AppLocaleDetails(languageCode="en-US", language="en", appName="My App", subTitle="Fast")]
    )
    monkeypatch.setattr(product_pages, "get_client", lambda: client)
    loc_res = runner.invoke(main_mod.app, ["product-pages", "app-locales", "999", "--format", "json"])
    assert loc_res.exit_code == 0
    assert json.loads(loc_res.stdout)[0]["language_code"] == "en-US"

    # budget-orders list
    client.budget_orders.query.return_value = V1Page[SharedBudget](
        result=[SharedBudget(id=888, name="FY26 Q1", systemStatus="ACTIVE", value=Money(amount="5000", currency="USD"))]
    )
    monkeypatch.setattr(budget_orders, "get_client", lambda: client)
    bo_res = runner.invoke(main_mod.app, ["budget-orders", "list", "--format", "json"])
    assert bo_res.exit_code == 0
    assert json.loads(bo_res.stdout)[0]["id"] == 888


def test_reports_ads_and_search_terms_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    """reports ads and search-terms reject HOURLY; search-terms forces ORTZ time zone."""
    hourly_ads = runner.invoke(
        main_mod.app,
        ["reports", "ads", "101", "--start", "2026-01-01", "--end", "2026-01-07", "--granularity", "HOURLY"],
    )
    assert hourly_ads.exit_code == utils.EXIT_USAGE

    hourly_st = runner.invoke(
        main_mod.app,
        ["reports", "search-terms", "101", "--start", "2026-01-01", "--end", "2026-01-07", "--granularity", "HOURLY"],
    )
    assert hourly_st.exit_code == utils.EXIT_USAGE

    client = _mock_client()
    client.reports.search_terms.return_value = AppsSearchTermReportResponse(
        result=AppsSearchTermResultContainer(
            rows=[
                AppsSearchTermReportRow(
                    metadata=ReportingSearchTerm(searchTermText="budget planner"),
                    totalMetrics=AppsMetrics(
                        impressions=100,
                        taps=10,
                        tapInstalls=4,
                        ttr=0.10,
                        tapInstallRate=0.40,
                        localSpend=Money(amount="15.00", currency="USD"),
                    ),
                )
            ]
        ),
        pagination=V1Pagination(totalCount=1, offset=0, limit=1000),
    )
    monkeypatch.setattr(reports, "get_client", lambda: client)

    st_res = runner.invoke(
        main_mod.app,
        ["--json", "reports", "search-terms", "101", "--start", "2026-01-01", "--end", "2026-01-07"],
    )
    assert st_res.exit_code == 0
    passed_req = client.reports.search_terms.call_args.args[0]
    assert passed_req.time_range.time_zone == ReportTimeZone.ORTZ
    assert "budget planner" in st_res.stdout


def test_recommendations_list_apply_and_dismiss(monkeypatch: pytest.MonkeyPatch) -> None:
    """recommendations commands query, apply, and dismiss Target CPA and Daily Budget recommendations."""
    client = _mock_client()
    client.campaigns.iter_all.side_effect = lambda: iter([_sample_campaign(101)])
    client.recommendations.iter_target_cpas.return_value = iter(
        [
            TargetCpaRecommendation(
                id="rec_tcpa_1",
                campaignId=101,
                campaignName="US Brand",
                promotedObjectId="999000111",
                state="AVAILABLE",
                bidStrategy=RecommendationBidStrategy(bidAmount=Money(amount="6.00", currency="USD")),
                recommendedTargetCpa=Money(amount="7.50", currency="USD"),
                averageCpa=Money(amount="6.20", currency="USD"),
                expectedInstalls=42,
            )
        ]
    )
    client.recommendations.iter_daily_budgets.return_value = iter(
        [
            DailyCapRecommendation(
                id="rec_bud_1",
                campaignId=101,
                campaignName="US Brand",
                promotedObjectId="999000111",
                state="AVAILABLE",
                dailyBudget=Money(amount="50.00", currency="USD"),
                suggestedDailyBudgetAmount=Money(amount="80.00", currency="USD"),
                expectedInstalls=60,
                expectedSpend=Money(amount="78.00", currency="USD"),
            )
        ]
    )
    client.recommendations.apply_target_cpas.return_value = [
        TargetCpaRecommendationHistory(
            recommendationId="rec_tcpa_1",
            campaignName="US Brand",
            state="APPLIED",
            appliedTargetCpa=Money(amount="7.50", currency="USD"),
        )
    ]
    client.recommendations.dismiss_daily_budgets.return_value = [
        DailyCapRecommendationHistory(
            recommendationId="rec_bud_1",
            campaignName="US Brand",
            state="DISMISSED",
        )
    ]
    monkeypatch.setattr(recommendations, "get_client", lambda: client)

    tcpa_res = runner.invoke(main_mod.app, ["recommendations", "target-cpas", "--format", "json"])
    assert tcpa_res.exit_code == 0
    assert json.loads(tcpa_res.stdout)[0]["id"] == "rec_tcpa_1"

    bud_res = runner.invoke(main_mod.app, ["recommendations", "daily-budgets", "--format", "json"])
    assert bud_res.exit_code == 0
    assert json.loads(bud_res.stdout)[0]["id"] == "rec_bud_1"

    app_res = runner.invoke(main_mod.app, ["recommendations", "apply", "rec_tcpa_1", "--type", "target-cpa"])
    assert app_res.exit_code == 0
    client.recommendations.apply_target_cpas.assert_called_once()

    dis_res = runner.invoke(main_mod.app, ["recommendations", "dismiss", "rec_bud_1", "--type", "daily-budget"])
    assert dis_res.exit_code == 0
    client.recommendations.dismiss_daily_budgets.assert_called_once()


def test_suggestions_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """suggestions keywords, phrases, categories, and target-cpa invoke v1 SuggestionsResource."""
    client = _mock_client()
    client.suggestions.query_keywords.return_value = V1Page[KeywordSuggestion](
        result=[KeywordSuggestion(text="expense tracker", popularity=68)]
    )
    client.suggestions.query_phrases.return_value = V1Page[PhraseSuggestion](
        result=[PhraseSuggestion(phrase="daily budget planner", popularity=54)]
    )
    client.suggestions.query_categories.return_value = V1Page[CategorySuggestion](
        result=[CategorySuggestion(category="Finance", popularity=82)]
    )
    client.suggestions.query_target_cpa.return_value = TargetCpaSuggestion(
        promoted_object_id="999000111",
        app_category="Finance",
        country_or_region=["US"],
        suggested_target_cpa=Money(amount="5.25", currency="USD"),
    )
    monkeypatch.setattr(suggestions, "get_client", lambda: client)

    kw_res = runner.invoke(main_mod.app, ["suggestions", "keywords", "--adam-id", "999000111", "--format", "json"])
    assert kw_res.exit_code == 0
    assert json.loads(kw_res.stdout)[0]["keyword"] == "expense tracker"

    ph_res = runner.invoke(main_mod.app, ["suggestions", "phrases", "--adam-id", "999000111", "--format", "json"])
    assert ph_res.exit_code == 0
    assert json.loads(ph_res.stdout)[0]["phrase"] == "daily budget planner"

    cat_res = runner.invoke(main_mod.app, ["suggestions", "categories", "--adam-id", "999000111", "--format", "json"])
    assert cat_res.exit_code == 0
    assert json.loads(cat_res.stdout)[0]["category"] == "Finance"

    tcpa_res = runner.invoke(
        main_mod.app,
        ["suggestions", "target-cpa", "--adam-id", "999000111", "--country", "US", "--format", "json"],
    )
    assert tcpa_res.exit_code == 0
    assert json.loads(tcpa_res.stdout)["suggestedTargetCPA"]["amount"] == "5.25"


def test_change_history_list_and_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """change-history list enforces eventTime + entityType filters; details renders field changes."""
    client = _mock_client()
    now_dt = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
    client.change_history.query.return_value = V1Page[AuditSummary](
        result=[
            AuditSummary(
                eventTime=now_dt,
                eventType="UPDATE",
                entityType="Campaign",
                count=1,
                userType="CUSTOMER_API",
                metas=[AuditSummaryMeta(detailId="Campaign.101.txn_1")],
            )
        ]
    )
    client.change_history.get_details.return_value = V1Page[ChangeDetails](
        result=[
            ChangeDetails(
                detailId="Campaign.101.txn_1",
                entityType="Campaign",
                entityId="101",
                eventType="UPDATE",
                eventTime=now_dt,
                userType="CUSTOMER_API",
                modifiedBy="api-bot",
                details=[
                    ActivityDetail(
                        changes=[ActivityChange(field="dailyBudget", oldValues=["50.00"], newValues=["75.00"])]
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr(change_history, "get_client", lambda: client)

    list_res = runner.invoke(main_mod.app, ["change-history", "list", "--days", "7", "--format", "csv"])
    assert list_res.exit_code == 0
    assert "Campaign.101.txn_1" in list_res.stdout
    q_payload = client.change_history.query.call_args.args[0].to_payload()
    fields = {c["field"] for c in q_payload["filters"]}
    assert {"eventTime", "entityType"} <= fields

    det_res = runner.invoke(main_mod.app, ["change-history", "details", "Campaign.101.txn_1", "--format", "csv"])
    assert det_res.exit_code == 0
    assert "dailyBudget,50.00,75.00" in det_res.stdout


def test_impression_share_analyze_and_popularity(monkeypatch: pytest.MonkeyPatch) -> None:
    """impression-share analyze and popularity use synchronous client.insights endpoints."""
    client = _mock_client()
    client.campaigns.iter_all.side_effect = lambda: iter([_sample_campaign(101)])
    client.insights.iter_impression_share.return_value = iter(
        [
            ImpressionShareRow(
                date=date(2026, 2, 1),
                countryOrRegion="US",
                searchTerm="budget app",
                appName="US Brand",
                adamId="999000111",
                lowImpressionShare=0.20,
                highImpressionShare=0.40,
                rank=1,
                searchPopularity=4,
            )
        ]
    )
    client.insights.iter_search_term_popularity.return_value = iter(
        [
            SearchTermPopularityRow(
                week=date(2026, 2, 1),
                countryOrRegion="US",
                genre="Finance",
                searchTerm="budget app",
                rankInGenre=3,
                searchPopularityInGenre=85,
                searchPopularity1to100=74,
                searchPopularity1to5=4,
            ),
            SearchTermPopularityRow(
                week=date(2026, 2, 1),
                countryOrRegion="AU",
                genre="Games",
                searchTerm="chess",
                rankInGenre=1,
                searchPopularityInGenre=90,
                searchPopularity1to100=80,
                searchPopularity1to5=5,
            ),
        ]
    )
    monkeypatch.setattr(impression_share, "get_client", lambda: client)

    an_res = runner.invoke(
        main_mod.app,
        ["impression-share", "analyze", "--country", "US", "--days", "7"],
    )
    assert an_res.exit_code == 0
    assert "budget app" in an_res.stdout

    pop_res = runner.invoke(
        main_mod.app,
        ["impression-share", "popularity", "--country", "US", "--search", "budget", "--format", "json"],
    )
    assert pop_res.exit_code == 0
    pop_rows = json.loads(pop_res.stdout)
    assert len(pop_rows) == 1
    assert pop_rows[0]["search_term"] == "budget app"


def test_brand_dry_run_and_optimize_bid_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """brand --dry-run previews v1 campaign plan; optimize bid-check inspects MANUAL_CPT ad groups."""
    client = _mock_client()
    client.ad_account_id = "123456"
    client.campaigns.get.return_value = _sample_campaign(101)
    client.campaigns.iter_all.side_effect = lambda *_a, **_kw: iter([_sample_campaign(101)])
    client.ad_groups.iter_all.side_effect = lambda *_a, **_kw: iter([_sample_ad_group(202, 101, amount="2.50")])
    monkeypatch.setattr(brand, "get_client", lambda: client)

    brand_res = runner.invoke(
        main_mod.app,
        ["brand", "Chippy", "-c", "US", "--reference", "101", "--budget", "50", "--bid", "1.50", "--dry-run"],
    )
    assert brand_res.exit_code == 0
    assert "Chippy" in brand_res.stdout or "chippy" in brand_res.stdout

    # optimize bid-check
    opt_client = _mock_client()
    opt_client.campaigns.iter_all.side_effect = lambda *_a, **_kw: iter([_sample_campaign(101)])
    opt_client.ad_groups.iter_all.side_effect = lambda *_a, **_kw: iter([_sample_ad_group(202, 101, amount="2.50")])
    opt_client.keywords.iter_all.side_effect = lambda *_a, **_kw: iter(
        [
            Keyword(
                id=303,
                campaignId=101,
                adGroupId=202,
                text="chippy",
                matchType="EXACT",
                status="ENABLED",
                bid=Money(amount="3.00", currency="USD"),
            )
        ]
    )
    monkeypatch.setattr(optimize, "get_client", lambda: opt_client)

    chk_res = runner.invoke(main_mod.app, ["optimize", "bid-check", "--threshold", "10", "--dry-run"])
    assert chk_res.exit_code == 0
    assert "Exact AG" in chk_res.stdout

    opt_client.ad_groups.update.return_value = _sample_ad_group(202, 101, amount="3.00")
    fix_res = runner.invoke(main_mod.app, ["optimize", "bid-check", "--threshold", "10", "--auto-fix"])
    assert fix_res.exit_code == 0
    opt_client.ad_groups.update.assert_called_once()
