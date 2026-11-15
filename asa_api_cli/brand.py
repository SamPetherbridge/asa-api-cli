"""Brand campaign CLI commands for Apple Ads Platform API v1.

This module provides the `brand` command for creating brand protection campaigns.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.ad_groups import AdGroupCreate, AdGroupStatus, PricingModel
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.bulk import BulkKeywordCreate
from asa_api_client.v1.models.campaigns import (
    BillingEvent,
    CampaignCreate,
    CampaignStatus,
    CampaignTargeting,
    DailyBudget,
    PromotedObjectType,
    SupplyPlacement,
    SupplySource,
)
from asa_api_client.v1.models.keywords import KeywordMatchType, KeywordStatus
from asa_api_client.v1.models.shared import TargetingData
from asa_api_client.v1.query import Query
from rich.table import Table

from asa_api_cli.optimize import CampaignNameParts, wait_for_resource
from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    ad_group_bid_money,
    campaign_adam_id,
    campaign_currency,
    campaign_daily_budget,
    console,
    enum_value,
    get_client,
    handle_api_error,
    manual_cpt_bid_strategy,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
    require_id,
    spinner,
)

app = typer.Typer(
    name="brand",
    help="Create brand protection campaigns.",
)

# All 91 Apple Ads supported countries
# Organized by region for easier maintenance
ALL_COUNTRIES = {
    # Africa, Middle East, and India (19)
    "africa_middle_east_india": [
        "DZ",  # Algeria
        "AM",  # Armenia
        "BH",  # Bahrain
        "EG",  # Egypt
        "GH",  # Ghana
        "IN",  # India
        "IQ",  # Iraq
        "IL",  # Israel
        "JO",  # Jordan
        "KE",  # Kenya
        "KW",  # Kuwait
        "LB",  # Lebanon
        "MA",  # Morocco
        "OM",  # Oman
        "PK",  # Pakistan
        "QA",  # Qatar
        "SA",  # Saudi Arabia
        "ZA",  # South Africa
        "AE",  # United Arab Emirates
    ],
    # Asia Pacific (18)
    "asia_pacific": [
        "AU",  # Australia
        "KH",  # Cambodia
        "CN",  # China mainland (requires special documentation)
        "HK",  # Hong Kong
        "ID",  # Indonesia
        "JP",  # Japan
        "MO",  # Macau
        "MY",  # Malaysia
        "MN",  # Mongolia
        "NP",  # Nepal
        "NZ",  # New Zealand
        "PH",  # Philippines
        "SG",  # Singapore
        "KR",  # South Korea
        "LK",  # Sri Lanka
        "TW",  # Taiwan
        "TH",  # Thailand
        "VN",  # Vietnam
    ],
    # Europe (37)
    "europe": [
        "AL",  # Albania
        "AT",  # Austria
        "AZ",  # Azerbaijan
        "BE",  # Belgium
        "BG",  # Bulgaria
        "HR",  # Croatia
        "CY",  # Cyprus
        "CZ",  # Czech Republic
        "DK",  # Denmark
        "EE",  # Estonia
        "FI",  # Finland
        "FR",  # France
        "DE",  # Germany
        "GR",  # Greece
        "HU",  # Hungary
        "IS",  # Iceland
        "IE",  # Ireland
        "IT",  # Italy
        "KZ",  # Kazakhstan
        "KG",  # Kyrgyzstan
        "LV",  # Latvia
        "LU",  # Luxembourg
        "NL",  # Netherlands
        "NO",  # Norway
        "PL",  # Poland
        "PT",  # Portugal
        "RO",  # Romania
        "RU",  # Russia
        "SK",  # Slovakia
        "SI",  # Slovenia
        "ES",  # Spain
        "SE",  # Sweden
        "CH",  # Switzerland
        "TR",  # Türkiye
        "GB",  # UK
        "UA",  # Ukraine
        "UZ",  # Uzbekistan
    ],
    # Latin America and the Caribbean (15)
    "latin_america": [
        "AR",  # Argentina
        "BO",  # Bolivia
        "BR",  # Brazil
        "CL",  # Chile
        "CO",  # Colombia
        "CR",  # Costa Rica
        "DO",  # Dominican Republic
        "EC",  # Ecuador
        "SV",  # El Salvador
        "GT",  # Guatemala
        "HN",  # Honduras
        "MX",  # Mexico
        "PA",  # Panamá
        "PY",  # Paraguay
        "PE",  # Peru
    ],
    # North America (2)
    "north_america": [
        "CA",  # Canada
        "US",  # United States
    ],
}

# China requires special business documentation
CHINA_COUNTRIES = ["CN"]

# Preset groups
COUNTRY_PRESETS = {
    "english": ["US", "GB", "CA", "AU", "NZ", "IE"],
    "tier1": ["US", "GB", "CA", "AU"],
    "europe": ALL_COUNTRIES["europe"],
    "asia": [c for c in ALL_COUNTRIES["asia_pacific"] if c != "CN"],
    "latam": ALL_COUNTRIES["latin_america"],
}


def get_all_countries(include_china: bool = False) -> list[str]:
    """Get all supported countries."""
    countries = []
    for region_countries in ALL_COUNTRIES.values():
        for code in region_countries:
            if code == "CN" and not include_china:
                continue
            countries.append(code)
    return countries


def get_country_count() -> int:
    """Get total number of supported countries (excluding China)."""
    return len(get_all_countries(include_china=False))


COUNTRY_NAMES = {
    "DZ": "Algeria",
    "AM": "Armenia",
    "BH": "Bahrain",
    "EG": "Egypt",
    "GH": "Ghana",
    "IN": "India",
    "IQ": "Iraq",
    "IL": "Israel",
    "JO": "Jordan",
    "KE": "Kenya",
    "KW": "Kuwait",
    "LB": "Lebanon",
    "MA": "Morocco",
    "OM": "Oman",
    "PK": "Pakistan",
    "QA": "Qatar",
    "SA": "Saudi Arabia",
    "ZA": "South Africa",
    "AE": "UAE",
    "AU": "Australia",
    "KH": "Cambodia",
    "CN": "China",
    "HK": "Hong Kong",
    "ID": "Indonesia",
    "JP": "Japan",
    "MO": "Macau",
    "MY": "Malaysia",
    "MN": "Mongolia",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "PH": "Philippines",
    "SG": "Singapore",
    "KR": "South Korea",
    "LK": "Sri Lanka",
    "TW": "Taiwan",
    "TH": "Thailand",
    "VN": "Vietnam",
    "AL": "Albania",
    "AT": "Austria",
    "AZ": "Azerbaijan",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IS": "Iceland",
    "IE": "Ireland",
    "IT": "Italy",
    "KZ": "Kazakhstan",
    "KG": "Kyrgyzstan",
    "LV": "Latvia",
    "LU": "Luxembourg",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RU": "Russia",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
    "CH": "Switzerland",
    "TR": "Türkiye",
    "GB": "UK",
    "UA": "Ukraine",
    "UZ": "Uzbekistan",
    "AR": "Argentina",
    "BO": "Bolivia",
    "BR": "Brazil",
    "CL": "Chile",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "DO": "Dominican Republic",
    "EC": "Ecuador",
    "SV": "El Salvador",
    "GT": "Guatemala",
    "HN": "Honduras",
    "MX": "Mexico",
    "PA": "Panamá",
    "PY": "Paraguay",
    "PE": "Peru",
    "CA": "Canada",
    "US": "United States",
}


@dataclass
class BrandCampaignPlan:
    """Plan for creating a brand campaign."""

    app_name: str
    adam_id: int
    country: str
    name: str
    keywords: list[str]
    daily_budget: Decimal
    currency: str
    default_bid: Decimal


def _select_countries_interactive(include_china: bool = False) -> list[str]:
    """Interactive country selection."""
    from rich.rule import Rule

    console.print()
    console.print(Rule("Select Target Countries"))
    console.print()

    table = Table(title="Country Presets", show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Preset")
    table.add_column("Countries")
    table.add_column("Count", justify="right")

    presets_list = [
        ("all", "All countries (excl. China)", get_country_count()),
        ("english", "English-speaking", len(COUNTRY_PRESETS["english"])),
        ("tier1", "Tier 1 (US, GB, CA, AU)", len(COUNTRY_PRESETS["tier1"])),
        ("europe", "Europe", len(COUNTRY_PRESETS["europe"])),
        ("asia", "Asia Pacific (excl. China)", len(COUNTRY_PRESETS["asia"])),
        ("latam", "Latin America", len(COUNTRY_PRESETS["latam"])),
    ]

    for i, (key, desc, count) in enumerate(presets_list, 1):
        table.add_row(str(i), key, desc, str(count))

    console.print(table)
    console.print()
    console.print("[dim]Or enter country codes separated by commas (e.g., US,GB,AU)[/dim]")
    console.print()

    selection = typer.prompt("Select preset (1-6) or enter country codes").strip()

    try:
        preset_idx = int(selection) - 1
        if 0 <= preset_idx < len(presets_list):
            preset_key = presets_list[preset_idx][0]
            if preset_key == "all":
                return get_all_countries(include_china=include_china)
            return COUNTRY_PRESETS[preset_key].copy()
    except ValueError:
        pass

    selection_lower = selection.lower()
    if selection_lower == "all":
        return get_all_countries(include_china=include_china)
    if selection_lower in COUNTRY_PRESETS:
        return COUNTRY_PRESETS[selection_lower].copy()

    countries: list[str] = []
    all_valid = get_all_countries(include_china=True)

    for code in selection.upper().replace(" ", "").split(","):
        if not code:
            continue
        if code not in all_valid:
            print_warning(f"Unknown country code: {code}")
            continue
        if code in CHINA_COUNTRIES and not include_china:
            print_warning(
                f"Skipping {code} - China requires special business documentation. Use --include-china to include."
            )
            continue
        if code not in countries:
            countries.append(code)

    return countries


def _select_app_interactive(client: AppleAdsClient) -> tuple[int, str, str]:
    """Interactive app selection returning ``(adam_id, app_name, currency)``."""
    console.print()
    print_info("Select an app to create brand campaigns for:")

    with spinner("Loading campaigns..."):
        all_campaigns = list(client.campaigns.iter_all())

    apps: dict[int, tuple[str, str]] = {}
    for camp in all_campaigns:
        aid = campaign_adam_id(camp)
        if aid and aid not in apps:
            camp_name = camp.name or f"App {aid}"
            parsed = CampaignNameParts.parse(camp_name)
            name = parsed.app_name if parsed else camp_name.split(" - ")[0]
            currency = campaign_currency(camp)
            apps[aid] = (name, currency)

    if not apps:
        # Fallback to owned apps from App Store search + account currency
        try:
            account_currency = "USD"
            if client.ad_account_id is not None:
                acct = client.ad_accounts.get(int(client.ad_account_id))
                account_currency = enum_value(acct.currency) or "USD"
            owned = client.apps.search("", return_owned_apps=True, limit=50)
            for app_info in owned.result:
                if app_info.adam_id and app_info.adam_id not in apps:
                    apps[app_info.adam_id] = (app_info.app_name or f"App {app_info.adam_id}", account_currency)
        except AppleSearchAdsError:
            pass

    if not apps:
        print_error("No apps", "No campaigns or owned apps found to get app information from")
        raise typer.Exit(EXIT_ERROR)

    table = Table(show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("App Name")
    table.add_column("Adam ID", style="dim")

    app_list = list(apps.items())
    for i, (aid, (name, _)) in enumerate(app_list, 1):
        table.add_row(str(i), name, str(aid))

    console.print(table)
    console.print()

    selection = typer.prompt("Select app number", default="1")
    try:
        idx = int(selection) - 1
        if 0 <= idx < len(app_list):
            adam_id, (app_name, currency) = app_list[idx]
            return adam_id, app_name, currency
        print_error("Invalid selection", f"Please enter 1-{len(app_list)}")
        raise typer.Exit(EXIT_USAGE)
    except ValueError:
        print_error("Invalid selection", "Please enter a number")
        raise typer.Exit(EXIT_USAGE) from None


def _get_brand_keywords_interactive(brand_name: str | None = None) -> list[str]:
    """Interactive brand keyword entry."""
    console.print()

    if brand_name:
        keywords = [brand_name.lower()]
        print_info(f"Brand name: {brand_name}")
    else:
        brand = typer.prompt("Enter your brand name").strip()
        keywords = [brand.lower()]

    console.print()
    console.print("[dim]Enter brand name variants (common misspellings, abbreviations)[/dim]")
    console.print("[dim]Press Enter with no input when done[/dim]")
    console.print()

    while True:
        variant = typer.prompt("Add variant (or press Enter to continue)", default="").strip()
        if not variant:
            break
        variant_lower = variant.lower()
        if variant_lower not in keywords:
            keywords.append(variant_lower)
            print_success(f"Added: {variant_lower}")
        else:
            print_warning(f"Already added: {variant_lower}")

    return keywords


def _get_budget_bid_interactive(
    ref_budget: Decimal | None = None,
    ref_bid: Decimal | None = None,
    ref_currency: str = "USD",
) -> tuple[Decimal, Decimal, str]:
    """Interactive budget and bid entry."""
    console.print()

    default_budget = str(ref_budget) if ref_budget else "50.00"
    budget_input = typer.prompt(
        f"Daily budget per campaign ({ref_currency})",
        default=default_budget,
    )
    budget = Decimal(budget_input)

    default_bid = f"{ref_bid:.2f}" if ref_bid else "1.00"
    bid_input = typer.prompt(
        f"Default bid ({ref_currency})",
        default=default_bid,
    )
    bid = Decimal(bid_input)

    return budget, bid, ref_currency


@app.callback(invoke_without_command=True)
def create_brand_campaigns(
    ctx: typer.Context,
    brand_name: Annotated[
        str | None,
        typer.Argument(help="Brand name (optional, will prompt if not provided)"),
    ] = None,
    variants: Annotated[
        list[str] | None,
        typer.Option("--variant", "-v", help="Brand name variants"),
    ] = None,
    countries: Annotated[
        list[str] | None,
        typer.Option("--country", "-c", help="Target countries"),
    ] = None,
    reference_campaign: Annotated[
        int | None,
        typer.Option("--reference", "-r", help="Campaign ID to copy settings from"),
    ] = None,
    daily_budget: Annotated[
        float | None,
        typer.Option("--budget", "-b", help="Daily budget"),
    ] = None,
    default_bid: Annotated[
        float | None,
        typer.Option("--bid", help="Default bid amount"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Preview without creating"),
    ] = False,
    paused: Annotated[
        bool,
        typer.Option("--paused", "-p", help="Create in PAUSED status"),
    ] = False,
    include_china: Annotated[
        bool,
        typer.Option("--include-china", help="Include China (requires special docs)"),
    ] = False,
) -> None:
    """Create brand protection campaigns.

    Interactive mode (recommended):
        asa brand

    With arguments:
        asa brand "Chippy Tools" -v "Chippy Tool" -c US -c GB

    This creates single-keyword ad groups (SKAGs) for your brand name
    and variants, helping protect your brand from competitors.
    """
    if ctx.invoked_subcommand is not None:
        return
    from rich.rule import Rule

    client = get_client()

    try:
        with client:
            ad_account_id = int(client.ad_account_id or client.resolve_ad_account())

            # Step 1: Get brand keywords
            if brand_name or variants:
                all_keywords = [brand_name.lower()] if brand_name else []
                if variants:
                    for v in variants:
                        kw = v.lower().strip()
                        if kw and kw not in all_keywords:
                            all_keywords.append(kw)
            else:
                all_keywords = _get_brand_keywords_interactive()

            if not all_keywords:
                print_error("No keywords", "At least one brand keyword is required")
                raise typer.Exit(EXIT_USAGE)

            print_info(f"Brand keywords ({len(all_keywords)}): {', '.join(all_keywords)}")

            # Step 2: Get target countries
            if countries:
                target_countries = []
                all_valid = get_all_countries(include_china=True)
                for code in countries:
                    code_upper = code.upper()
                    if code_upper not in all_valid:
                        print_warning(f"Unknown country code: {code_upper}")
                        continue
                    if code_upper in CHINA_COUNTRIES and not include_china:
                        print_warning(f"Skipping {code_upper} - requires --include-china")
                        continue
                    target_countries.append(code_upper)
            else:
                target_countries = _select_countries_interactive(include_china)

            if not target_countries:
                print_error("No countries", "No valid target countries selected")
                raise typer.Exit(EXIT_USAGE)

            countries_preview = ", ".join(target_countries[:10])
            suffix = "..." if len(target_countries) > 10 else ""
            print_info(f"Target countries ({len(target_countries)}): {countries_preview}{suffix}")

            # Step 3: Get reference campaign or select app
            ref_budget: Decimal | None = None
            ref_bid: Decimal | None = None
            ref_currency = "USD"
            adam_id: int | None = None
            app_name: str | None = None

            if reference_campaign:
                with spinner("Loading reference campaign..."):
                    ref_camp = client.campaigns.get(reference_campaign)
                    adam_id = campaign_adam_id(ref_camp)
                    ref_name = ref_camp.name or f"Campaign {reference_campaign}"
                    parsed_name = CampaignNameParts.parse(ref_name)
                    app_name = parsed_name.app_name if parsed_name else ref_name.split(" - ")[0]

                    budget_money = campaign_daily_budget(ref_camp)
                    if budget_money and budget_money.amount is not None:
                        ref_budget = Decimal(budget_money.amount)
                        ref_currency = budget_money.currency or "USD"
                    else:
                        ref_currency = campaign_currency(ref_camp)

                    ad_groups = list(
                        client.ad_groups.iter_all(Query().where("campaignId", "EQUALS", reference_campaign))
                    )
                    if ad_groups:
                        bids: list[Decimal] = []
                        for ag in ad_groups:
                            ag_bid = ad_group_bid_money(ag)
                            if ag_bid and ag_bid.amount is not None:
                                bids.append(Decimal(ag_bid.amount))
                        if bids:
                            ref_bid = Decimal(sum(bids) / len(bids))

                print_info(f"Reference: {ref_camp.name}")
                if ref_budget:
                    bid_str = f"{ref_bid:.2f}" if ref_bid else "N/A"
                    print_info(f"  Budget: {ref_budget} {ref_currency}, Avg bid: {bid_str} {ref_currency}")
            else:
                adam_id, app_name, ref_currency = _select_app_interactive(client)

            if adam_id is None:
                print_error("No app selected", "Cannot continue without an App Store app (adamId)")
                return

            # Step 4: Get budget and bid
            final_budget = Decimal(str(daily_budget)) if daily_budget is not None else ref_budget
            final_bid = Decimal(str(default_bid)) if default_bid is not None else ref_bid

            if final_budget is None or final_bid is None:
                final_budget, final_bid, ref_currency = _get_budget_bid_interactive(
                    ref_budget=final_budget,
                    ref_bid=final_bid,
                    ref_currency=ref_currency,
                )

            # Step 5: Build campaign plans
            campaign_plans: list[BrandCampaignPlan] = []
            resolved_app_name = app_name or all_keywords[0].title()

            for country in target_countries:
                campaign_name = f"{resolved_app_name} - {country} - Brand - EM"
                campaign_plans.append(
                    BrandCampaignPlan(
                        app_name=resolved_app_name,
                        adam_id=adam_id,
                        country=country,
                        name=campaign_name,
                        keywords=all_keywords,
                        daily_budget=final_budget,
                        currency=ref_currency,
                        default_bid=final_bid,
                    )
                )

            # Step 6: Display plan
            console.print()
            console.print(Rule("Brand Campaign Plan"))
            console.print()

            console.print(f"[bold]App:[/bold] {resolved_app_name} (Adam ID: {adam_id})")
            console.print(f"[bold]Keywords:[/bold] {', '.join(all_keywords)}")
            console.print(f"[bold]Daily Budget:[/bold] {final_budget} {ref_currency}")
            console.print(f"[bold]Default Bid:[/bold] {final_bid:.2f} {ref_currency}")
            console.print(f"[bold]Status:[/bold] {'PAUSED' if paused else 'ENABLED'}")
            console.print()

            if len(campaign_plans) > 10:
                console.print(f"[bold]Campaigns to create:[/bold] {len(campaign_plans)}")
                console.print(f"[bold]Ad groups per campaign:[/bold] {len(all_keywords)}")
                console.print(f"[bold]Total ad groups:[/bold] {len(campaign_plans) * len(all_keywords)}")
                console.print()

                console.print("[dim]Countries by region:[/dim]")
                for region, codes in ALL_COUNTRIES.items():
                    region_countries = [c for c in target_countries if c in codes]
                    if region_countries:
                        region_name = region.replace("_", " ").title()
                        console.print(f"  {region_name}: {', '.join(region_countries)}")
            else:
                table = Table(title=f"Campaigns to Create ({len(campaign_plans)})")
                table.add_column("#", style="dim", width=4)
                table.add_column("Campaign Name")
                table.add_column("Country")
                table.add_column("Ad Groups")

                for i, plan in enumerate(campaign_plans, 1):
                    country_name = COUNTRY_NAMES.get(plan.country, plan.country)
                    table.add_row(str(i), plan.name, f"{plan.country} ({country_name})", str(len(plan.keywords)))

                console.print(table)

            console.print()

            if dry_run:
                print_info("Dry run - no campaigns created")
                return

            # Step 7: Confirm and create
            total_budget = final_budget * len(campaign_plans)
            console.print(f"[yellow]Total daily budget: {total_budget} {ref_currency}[/yellow]")
            console.print()

            if not typer.confirm(f"Create {len(campaign_plans)} brand campaign(s)?", default=True):
                print_info("Cancelled")
                return

            # Step 8: Create campaigns
            created_campaigns = 0
            created_ad_groups = 0
            created_keywords = 0

            for plan_idx, plan in enumerate(campaign_plans, 1):
                console.print()
                console.print(f"[dim][{plan_idx}/{len(campaign_plans)}][/dim] Creating {plan.name}...")

                with spinner("Creating campaign..."):
                    new_campaign = client.campaigns.create(
                        CampaignCreate(
                            ad_account_id=ad_account_id,
                            name=plan.name,
                            billing_event=BillingEvent.TAPS,
                            promoted_object_type=PromotedObjectType.APPSTORE_APP,
                            promoted_object_id=str(plan.adam_id),
                            daily_budget=DailyBudget(
                                value=Money(amount=str(plan.daily_budget), currency=plan.currency),
                            ),
                            targeting=CampaignTargeting(
                                country_or_region=TargetingData(include=[plan.country]),
                                supply_source=TargetingData(include=[SupplySource.APPSTORE.value]),
                                supply_placement=TargetingData(include=[SupplyPlacement.APPSTORE_SEARCH_RESULTS.value]),
                            ),
                            status=CampaignStatus.PAUSED if paused else CampaignStatus.ENABLED,
                        )
                    )

                new_campaign_id = require_id(new_campaign.id, "Campaign")
                print_success(f"Created campaign (ID: {new_campaign_id})")
                created_campaigns += 1

                def get_campaign(cid: int = new_campaign_id) -> Any:
                    return client.campaigns.get(cid)

                wait_for_resource(get_campaign, max_attempts=10, delay=0.5)

                for keyword in plan.keywords:
                    ag_name = f"Exact - {keyword.title()}"[:200]

                    with spinner(f"  Creating: {ag_name}..."):
                        new_ag = client.ad_groups.create(
                            AdGroupCreate(
                                campaign_id=new_campaign_id,
                                name=ag_name,
                                pricing_model=PricingModel.CPT,
                                bid_strategy=manual_cpt_bid_strategy(plan.default_bid, plan.currency),
                                automated_keywords_opt_in=False,
                                status=AdGroupStatus.ENABLED,
                            )
                        )
                        new_ag_id = require_id(new_ag.id, "Ad group")
                        created_ad_groups += 1

                    client.bulk.create_keywords(
                        [
                            BulkKeywordCreate(
                                ad_group_id=new_ag_id,
                                text=keyword,
                                match_type=KeywordMatchType.EXACT,
                                bid=Money(amount=str(plan.default_bid), currency=plan.currency),
                                status=KeywordStatus.ENABLED,
                            )
                        ]
                    )
                    created_keywords += 1

                    print_success(f"  {ag_name} → '{keyword}'")

            console.print()
            print_result_panel(
                "Brand Campaigns Created",
                {
                    "Campaigns": str(created_campaigns),
                    "Ad Groups": str(created_ad_groups),
                    "Keywords": str(created_keywords),
                    "Status": "PAUSED" if paused else "ENABLED",
                },
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
