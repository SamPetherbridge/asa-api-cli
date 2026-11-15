"""Keyword translation module using PydanticAI for Apple Ads Platform API v1.

Provides AI-powered keyword translation with support for:
- Claude (Anthropic) and Gemini (Google) models
- Structured output validation
- SKAG creation with translated keywords
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.ad_groups import AdGroupCreate, AdGroupStatus, PricingModel
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.bulk import BulkKeywordCreate
from asa_api_client.v1.models.keywords import Keyword, KeywordMatchType, KeywordStatus
from asa_api_client.v1.query import Query
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.rule import Rule
from rich.table import Table

from asa_api_cli.languages import (
    LANGUAGE_NAMES,
    build_ad_group_name,
    detect_source_language,
    get_language_name,
    get_languages_for_country,
    parse_ad_group_name,
)
from asa_api_cli.utils import (
    EXIT_ERROR,
    ad_group_bid_money,
    campaign_countries,
    campaign_currency,
    console,
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
    name="translate",
    help="Translate keywords to multiple languages and create SKAGs.",
    rich_markup_mode="rich",
)


# ============================================================================
# Settings
# ============================================================================


class TranslateSettings(BaseSettings):  # type: ignore[misc]
    """Translation configuration loaded from environment/.env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    translate_provider: Literal["anthropic", "gemini"] = "anthropic"


def get_translate_settings(env_file: Path | None = None) -> TranslateSettings:
    """Load translation settings from environment and .env file."""
    if env_file is not None:
        return TranslateSettings(_env_file=env_file)  # type: ignore[call-arg]
    return TranslateSettings()


# ============================================================================
# Pydantic Models for Structured Output
# ============================================================================


class TranslatedKeyword(BaseModel):  # type: ignore[misc]
    """A single translated keyword."""

    original: str = Field(description="The original keyword in source language")
    translated: str = Field(description="The translated keyword in target language")
    notes: str | None = Field(
        default=None,
        description="Optional notes about the translation (e.g., cultural context)",
    )


class KeywordTranslations(BaseModel):  # type: ignore[misc]
    """Batch of translated keywords."""

    translations: list[TranslatedKeyword] = Field(description="List of keyword translations")


# ============================================================================
# Translation Agent
# ============================================================================

TRANSLATION_SYSTEM_PROMPT = """You are an expert translator specializing in App Store keyword localization.

Your task is to translate keywords for App Store Search Ads campaigns.

Guidelines:
1. Translate keywords naturally for the target market
2. Consider local search behavior and terminology
3. Keep translations concise (keywords should be short and searchable)
4. Preserve the meaning and intent of the original keyword
5. Do NOT transliterate brand names unless they have established local versions
6. For compound keywords, ensure grammatical correctness in the target language
7. Consider plural/singular forms based on target language conventions

Important: Return translations as lowercase keywords suitable for search ads."""


def get_translation_agent(
    settings: TranslateSettings | None = None,
    provider: str | None = None,
) -> Agent[None, KeywordTranslations]:
    """Create a translation agent with the specified provider."""
    if settings is None:
        settings = get_translate_settings()

    active_provider = provider or settings.translate_provider

    model: Model
    if active_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for Claude translation.\n"
                "Add it to your .env file or set the environment variable."
            )
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key=settings.anthropic_api_key))

    elif active_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is required for Gemini translation.\n"
                "Add it to your .env file or set the environment variable."
            )
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        model = GoogleModel("gemini-2.0-flash", provider=GoogleProvider(api_key=settings.gemini_api_key))
    else:
        raise ValueError(f"Unknown translation provider: {active_provider}")

    return Agent(
        model,
        output_type=KeywordTranslations,
        system_prompt=TRANSLATION_SYSTEM_PROMPT,
    )


async def translate_keywords(
    keywords: list[str],
    source_language: str,
    target_language: str,
    settings: TranslateSettings | None = None,
    provider: str | None = None,
) -> KeywordTranslations:
    """Translate a list of keywords."""
    agent = get_translation_agent(settings=settings, provider=provider)

    source_name = get_language_name(source_language)
    target_name = get_language_name(target_language)

    prompt = f"""Translate the following keywords from {source_name} to {target_name}.

Keywords to translate:
{chr(10).join(f"- {kw}" for kw in keywords)}

Provide translations for each keyword."""

    result = await agent.run(prompt)
    return result.output  # type: ignore[no-any-return]


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class AdGroupAnalysis:
    """Analysis of an ad group for translation."""

    ad_group_id: int
    ad_group_name: str
    keyword_text: str
    keyword_id: int
    match_type: str
    bid: Decimal
    currency: str
    detected_language: str | None
    english_keyword: str
    existing_translations: list[str]
    missing_translations: list[str]


@dataclass
class TranslationPlan:
    """Plan for creating a translated SKAG."""

    english_keyword: str
    translated_keyword: str
    target_language: str
    match_type: str
    bid: Decimal
    currency: str
    ad_group_name: str


# ============================================================================
# CLI Commands
# ============================================================================


@app.command("detect")
def detect_translations(
    campaign_id: Annotated[
        int,
        typer.Argument(help="Campaign ID to analyze"),
    ],
    target_language: Annotated[
        str | None,
        typer.Option("--target-language", "-t", help="Target language code (e.g., es, fr, de)"),
    ] = None,
) -> None:
    """Detect keywords that need translation.

    Analyzes ad groups in a campaign to identify:
    - Current keywords and their detected languages
    - Missing translations based on campaign country

    Example:
        asa translate detect 123456789
        asa translate detect 123456789 --target-language es
    """
    client = get_client()

    try:
        with client:
            with spinner("Loading campaign..."):
                campaign = client.campaigns.get(campaign_id)

            campaign_name = campaign.name or f"Campaign {campaign_id}"
            countries = campaign_countries(campaign)
            country = countries[0] if countries else "US"

            console.print()
            console.print(f"[bold]Campaign:[/bold] {campaign_name}")
            console.print(f"[bold]Country:[/bold] {country}")

            available_languages = get_languages_for_country(country)
            target_languages = [target_language.lower()] if target_language else available_languages

            console.print(f"[bold]Languages:[/bold] {', '.join(target_languages)}")
            console.print()

            with spinner("Loading ad groups and keywords..."):
                ad_groups = list(client.ad_groups.iter_all(Query().where("campaignId", "EQUALS", campaign_id)))
                all_keywords = list(client.keywords.iter_all(Query().where("campaignId", "EQUALS", campaign_id)))

            if not ad_groups:
                print_warning("No ad groups found in this campaign")
                return

            keywords_by_ag: dict[int, list[Keyword]] = defaultdict(list)
            for kw in all_keywords:
                if kw.ad_group_id is not None:
                    keywords_by_ag[kw.ad_group_id].append(kw)

            keyword_groups: dict[str, list[str]] = {}

            for ag in ad_groups:
                if ag.id is None or not keywords_by_ag.get(ag.id):
                    continue

                ag_name = ag.name or ""
                parts = parse_ad_group_name(ag_name)
                english_keyword = parts.keyword.lower()

                if english_keyword not in keyword_groups:
                    keyword_groups[english_keyword] = []

                if parts.language:
                    keyword_groups[english_keyword].append(parts.language)
                else:
                    source_lang = detect_source_language(country, ag_name)
                    keyword_groups[english_keyword].append(source_lang)

            console.print(Rule("Translation Analysis"))
            console.print()

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("English Keyword")
            table.add_column("Existing")
            table.add_column("Missing")

            for english_kw, existing_langs in sorted(keyword_groups.items()):
                existing_set = set(existing_langs)
                missing = [lang for lang in target_languages if lang not in existing_set]

                existing_display = ", ".join(sorted(existing_set)) if existing_set else "-"
                missing_display = ", ".join(missing) if missing else "[green]-[/green]"

                table.add_row(english_kw, existing_display, missing_display)

            console.print(table)
            console.print()

            total_keywords = len(keyword_groups)
            needs_translation = sum(
                1 for existing in keyword_groups.values() if any(lang not in existing for lang in target_languages)
            )

            console.print(f"[bold]Total keywords:[/bold] {total_keywords}")
            console.print(f"[bold]Needing translation:[/bold] {needs_translation}")

            if needs_translation > 0 and target_language:
                console.print()
                console.print(
                    f"[info]Run 'asa translate keywords {campaign_id} --target-language {target_language}' "
                    "to translate[/info]"
                )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("keywords")
def translate_keywords_command(
    campaign_id: Annotated[
        int,
        typer.Argument(help="Campaign ID"),
    ],
    target_language: Annotated[
        str,
        typer.Option("--target-language", "-t", help="Target language code (e.g., es, fr, de)"),
    ],
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="AI provider (anthropic or gemini)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview translations without creating SKAGs"),
    ] = False,
    ad_group_id: Annotated[
        int | None,
        typer.Option("--ad-group", "-a", help="Only translate keywords from this ad group"),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file with API keys"),
    ] = None,
) -> None:
    """Translate keywords and create SKAGs."""
    import asyncio

    settings = get_translate_settings(env_file)
    active_provider = provider or settings.translate_provider

    target_lang = target_language.lower()
    target_name = get_language_name(target_lang)

    client = get_client()

    try:
        with client:
            with spinner("Loading campaign..."):
                campaign = client.campaigns.get(campaign_id)

            campaign_name = campaign.name or f"Campaign {campaign_id}"
            currency = campaign_currency(campaign)

            console.print()
            console.print(f"[bold]Campaign:[/bold] {campaign_name}")
            console.print(f"[bold]Target language:[/bold] {target_name} ({target_lang})")
            console.print(f"[bold]Provider:[/bold] {active_provider}")
            console.print()

            with spinner("Loading ad groups and keywords..."):
                if ad_group_id:
                    ad_groups_data = [client.ad_groups.get(ad_group_id)]
                    kw_query = Query().where("adGroupId", "EQUALS", ad_group_id)
                else:
                    ad_groups_data = list(client.ad_groups.iter_all(Query().where("campaignId", "EQUALS", campaign_id)))
                    kw_query = Query().where("campaignId", "EQUALS", campaign_id)
                all_keywords = list(client.keywords.iter_all(kw_query))

            if not ad_groups_data:
                print_warning("No ad groups found")
                return

            keywords_by_ag: dict[int, list[Keyword]] = defaultdict(list)
            for kw in all_keywords:
                if kw.ad_group_id is not None:
                    keywords_by_ag[kw.ad_group_id].append(kw)

            keywords_to_translate: list[tuple[str, str, Decimal]] = []
            existing_translations: set[str] = set()

            for ag in ad_groups_data:
                if ag.id is None:
                    continue
                parts = parse_ad_group_name(ag.name or "")
                english_kw = parts.keyword.lower()

                if parts.language == target_lang:
                    existing_translations.add(english_kw)
                    continue

                ag_keywords = keywords_by_ag.get(ag.id, [])
                if not ag_keywords:
                    continue

                kw = ag_keywords[0]
                if kw.bid and kw.bid.amount is not None:
                    bid = Decimal(kw.bid.amount)
                else:
                    ag_bid = ad_group_bid_money(ag)
                    bid = Decimal(ag_bid.amount) if ag_bid and ag_bid.amount is not None else Decimal("1.00")

                if english_kw not in existing_translations and not any(
                    k[0] == english_kw for k in keywords_to_translate
                ):
                    keywords_to_translate.append((english_kw, parts.match_type, bid))

            keywords_to_translate = [
                (kw, mt, bid) for kw, mt, bid in keywords_to_translate if kw not in existing_translations
            ]

            if not keywords_to_translate:
                print_success(f"All keywords already translated to {target_name}")
                return

            console.print(f"Found {len(keywords_to_translate)} keywords to translate")
            console.print()

            with spinner(f"Translating to {target_name}..."):
                translations = asyncio.run(
                    translate_keywords(
                        [kw for kw, _, _ in keywords_to_translate],
                        source_language="en",
                        target_language=target_lang,
                        settings=settings,
                        provider=active_provider,
                    )
                )

            translation_map: dict[str, str] = {}
            for t in translations.translations:
                translation_map[t.original.lower()] = t.translated.lower()

            plans: list[TranslationPlan] = []
            for english_kw, match_type, bid in keywords_to_translate:
                translated = translation_map.get(english_kw, english_kw)
                ad_group_name = build_ad_group_name(english_kw, match_type, target_lang)

                plans.append(
                    TranslationPlan(
                        english_keyword=english_kw,
                        translated_keyword=translated,
                        target_language=target_lang,
                        match_type=match_type,
                        bid=bid,
                        currency=currency,
                        ad_group_name=ad_group_name,
                    )
                )

            console.print(Rule("Translation Preview"))
            console.print()

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("English Keyword")
            table.add_column(f"{target_name} Translation")
            table.add_column("Ad Group Name")
            table.add_column("Bid")

            for plan in plans:
                table.add_row(
                    plan.english_keyword,
                    plan.translated_keyword,
                    plan.ad_group_name,
                    f"{plan.bid:.2f} {plan.currency}",
                )

            console.print(table)
            console.print()

            if dry_run:
                print_info("Dry run - no SKAGs created")
                return

            if not typer.confirm(f"Create {len(plans)} new SKAGs?", default=True):
                print_info("Cancelled")
                return

            created_count = 0
            for plan in plans:
                with spinner(f"Creating: {plan.ad_group_name}"):
                    new_ag = client.ad_groups.create(
                        AdGroupCreate(
                            campaign_id=campaign_id,
                            name=plan.ad_group_name,
                            pricing_model=PricingModel.CPT,
                            bid_strategy=manual_cpt_bid_strategy(plan.bid, plan.currency),
                            automated_keywords_opt_in=False,
                            status=AdGroupStatus.ENABLED,
                        )
                    )
                    new_ag_id = require_id(new_ag.id, "Ad group")

                    match_type = (
                        KeywordMatchType.EXACT if plan.match_type.lower() == "exact" else KeywordMatchType.BROAD
                    )

                    client.bulk.create_keywords(
                        [
                            BulkKeywordCreate(
                                ad_group_id=new_ag_id,
                                text=plan.translated_keyword,
                                match_type=match_type,
                                bid=Money(amount=str(plan.bid), currency=plan.currency),
                                status=KeywordStatus.ENABLED,
                            )
                        ]
                    )

                created_count += 1
                print_success(f"Created: {plan.ad_group_name}")

            console.print()
            print_result_panel(
                "Translation Complete",
                {
                    "Campaign": campaign_name,
                    "Target Language": target_name,
                    "SKAGs Created": str(created_count),
                },
            )

    except RuntimeError as e:
        print_error("Configuration Error", str(e))
        console.print()
        console.print("[info]Add API keys to your .env file:[/info]")
        console.print("  ANTHROPIC_API_KEY=sk-ant-...")
        console.print("  GEMINI_API_KEY=...")
        console.print("  TRANSLATE_PROVIDER=anthropic")
        console.print()
        console.print("[info]Or set environment variables directly.[/info]")
        raise typer.Exit(EXIT_ERROR) from None
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("languages")
def list_languages(
    country: Annotated[
        str | None,
        typer.Argument(help="Country code to show languages for (e.g., US, ES, DE)"),
    ] = None,
) -> None:
    """List supported languages and country mappings."""
    if country:
        languages = get_languages_for_country(country)
        if not languages:
            print_warning(f"No language mapping found for country: {country}")
            return

        console.print(f"[bold]Languages for {country.upper()}:[/bold]")
        for lang in languages:
            console.print(f"  {lang} - {get_language_name(lang)}")
    else:
        console.print("[bold]Supported Languages:[/bold]")
        console.print()

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Code")
        table.add_column("Language")

        for code, name in sorted(LANGUAGE_NAMES.items()):
            table.add_row(code, name)

        console.print(table)


@app.command("config")
def show_config(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file"),
    ] = None,
) -> None:
    """Show current translation configuration."""
    settings = get_translate_settings(env_file)

    source = f"from {env_file}" if env_file else "from .env / environment"

    table = Table(
        title=f"Translation Configuration ({source})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Setting")
    table.add_column("Value")

    if settings.anthropic_api_key:
        masked = settings.anthropic_api_key[:10] + "..." + settings.anthropic_api_key[-4:]
        table.add_row("ANTHROPIC_API_KEY", f"[success]{masked}[/success]")
    else:
        table.add_row("ANTHROPIC_API_KEY", "[warning]<not set>[/warning]")

    if settings.gemini_api_key:
        masked = settings.gemini_api_key[:10] + "..." + settings.gemini_api_key[-4:]
        table.add_row("GEMINI_API_KEY", f"[success]{masked}[/success]")
    else:
        table.add_row("GEMINI_API_KEY", "[warning]<not set>[/warning]")

    table.add_row("TRANSLATE_PROVIDER", f"[info]{settings.translate_provider}[/info]")

    console.print(table)
