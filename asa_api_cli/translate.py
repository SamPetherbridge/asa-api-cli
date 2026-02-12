"""Keyword translation module using PydanticAI.

Provides AI-powered keyword translation with support for:
- Claude (Anthropic) and Gemini (Google) models
- Structured output validation
- SKAG creation with translated keywords
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.models import (
    AdGroupCreate,
    KeywordCreate,
    KeywordMatchType,
    Money,
)
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
    console,
    get_client,
    handle_api_error,
    print_error,
    print_info,
    print_result_panel,
    print_success,
    print_warning,
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
    """Translation configuration loaded from environment/.env file.

    Supports the following environment variables:
    - ANTHROPIC_API_KEY: API key for Claude models
    - GEMINI_API_KEY: API key for Google Gemini models
    - TRANSLATE_PROVIDER: Default provider ("anthropic" or "gemini")
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    translate_provider: Literal["anthropic", "gemini"] = "anthropic"


def get_translate_settings(env_file: Path | None = None) -> TranslateSettings:
    """Load translation settings from environment and .env file.

    Args:
        env_file: Optional path to .env file. Defaults to ".env" in current directory.

    Returns:
        TranslateSettings instance.
    """
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
    """Create a translation agent with the specified provider.

    Args:
        settings: Translation settings. If None, loads from environment/.env.
        provider: Override the provider from settings ("anthropic" or "gemini").

    Returns:
        Configured PydanticAI agent.

    Raises:
        ValueError: If provider is unknown.
        RuntimeError: If API key is not configured.
    """
    if settings is None:
        settings = get_translate_settings()

    # Use provided provider or fall back to settings default
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
    """Translate a list of keywords.

    Args:
        keywords: List of keywords to translate.
        source_language: Source language code (e.g., "en").
        target_language: Target language code (e.g., "es").
        settings: Translation settings. If None, loads from environment/.env.
        provider: Override the provider from settings.

    Returns:
        KeywordTranslations with all translated keywords.
    """
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
    english_keyword: str  # Parsed from ad group name
    existing_translations: list[str]  # Languages already translated
    missing_translations: list[str]  # Languages that need translation


@dataclass
class TranslationPlan:
    """Plan for creating a translated SKAG."""

    english_keyword: str
    translated_keyword: str
    target_language: str
    match_type: str
    bid: Decimal
    currency: str
    ad_group_name: str  # The new ad group name


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
            # Get campaign details
            with spinner("Loading campaign..."):
                campaign = client.campaigns.get(campaign_id)

            campaign_name = campaign.name
            countries = campaign.countries_or_regions or []
            country = countries[0] if countries else "US"

            console.print()
            console.print(f"[bold]Campaign:[/bold] {campaign_name}")
            console.print(f"[bold]Country:[/bold] {country}")

            # Get available languages for this country
            available_languages = get_languages_for_country(country)
            if target_language:
                target_languages = [target_language.lower()]
            else:
                target_languages = available_languages

            console.print(f"[bold]Languages:[/bold] {', '.join(target_languages)}")
            console.print()

            # Get all ad groups
            with spinner("Loading ad groups..."):
                ad_groups = client.campaigns(campaign_id).ad_groups.list(limit=1000)

            if not ad_groups.data:
                print_warning("No ad groups found in this campaign")
                return

            # Analyze each ad group
            keyword_groups: dict[str, list[str]] = {}  # english_keyword -> [existing languages]

            with spinner("Analyzing keywords..."):
                for ag in ad_groups.data:
                    # Get keywords in this ad group
                    keywords = client.campaigns(campaign_id).ad_groups(ag.id).keywords.list(limit=10)

                    if not keywords.data:
                        continue

                    # Parse ad group name
                    parts = parse_ad_group_name(ag.name)
                    english_keyword = parts.keyword.lower()

                    # Track existing translations
                    if english_keyword not in keyword_groups:
                        keyword_groups[english_keyword] = []

                    if parts.language:
                        keyword_groups[english_keyword].append(parts.language)
                    else:
                        # No tag, assume source language
                        source_lang = detect_source_language(country, ag.name)
                        keyword_groups[english_keyword].append(source_lang)

            # Build analysis table
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

            # Summary
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
    """Translate keywords and create SKAGs.

    Translates keywords that don't have a translation in the target language,
    then creates new ad groups with the translated keywords.

    API keys can be set in a .env file:
        ANTHROPIC_API_KEY=sk-ant-...
        GEMINI_API_KEY=...
        TRANSLATE_PROVIDER=anthropic

    Examples:
        asa translate keywords 123456789 --target-language es
        asa translate keywords 123456789 -t fr --provider gemini
        asa translate keywords 123456789 -t de --dry-run
        asa translate keywords 123456789 -t es --env-file .env.production
    """
    import asyncio

    # Load settings from .env file
    settings = get_translate_settings(env_file)
    active_provider = provider or settings.translate_provider

    target_lang = target_language.lower()
    target_name = get_language_name(target_lang)

    client = get_client()

    try:
        with client:
            # Get campaign details
            with spinner("Loading campaign..."):
                campaign = client.campaigns.get(campaign_id)

            campaign_name = campaign.name
            currency = campaign.daily_budget_amount.currency if campaign.daily_budget_amount else "USD"

            console.print()
            console.print(f"[bold]Campaign:[/bold] {campaign_name}")
            console.print(f"[bold]Target language:[/bold] {target_name} ({target_lang})")
            console.print(f"[bold]Provider:[/bold] {active_provider}")
            console.print()

            # Get ad groups
            with spinner("Loading ad groups..."):
                if ad_group_id:
                    ad_groups_data = [client.campaigns(campaign_id).ad_groups.get(ad_group_id)]
                else:
                    result = client.campaigns(campaign_id).ad_groups.list(limit=1000)
                    ad_groups_data = result.data or []

            if not ad_groups_data:
                print_warning("No ad groups found")
                return

            # Find keywords needing translation
            keywords_to_translate: list[tuple[str, str, Decimal]] = []  # (english_kw, match_type, bid)
            existing_translations: set[str] = set()  # english keywords already translated

            with spinner("Analyzing keywords..."):
                for ag in ad_groups_data:
                    parts = parse_ad_group_name(ag.name)
                    english_kw = parts.keyword.lower()

                    # Check if this is already the target language
                    if parts.language == target_lang:
                        existing_translations.add(english_kw)
                        continue

                    # Get the keyword's bid
                    keywords = client.campaigns(campaign_id).ad_groups(ag.id).keywords.list(limit=1)
                    if not keywords.data:
                        continue

                    kw = keywords.data[0]
                    bid = Decimal(kw.bid_amount.amount) if kw.bid_amount else Decimal("1.00")

                    # Only add if not already translated
                    if english_kw not in existing_translations:
                        # Check if we already have this keyword queued
                        if not any(k[0] == english_kw for k in keywords_to_translate):
                            keywords_to_translate.append((english_kw, parts.match_type, bid))

            # Filter out already translated
            keywords_to_translate = [
                (kw, mt, bid) for kw, mt, bid in keywords_to_translate if kw not in existing_translations
            ]

            if not keywords_to_translate:
                print_success(f"All keywords already translated to {target_name}")
                return

            console.print(f"Found {len(keywords_to_translate)} keywords to translate")
            console.print()

            # Translate keywords
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

            # Build translation mapping
            translation_map: dict[str, str] = {}
            for t in translations.translations:
                translation_map[t.original.lower()] = t.translated.lower()

            # Create translation plans
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

            # Display preview
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

            # Confirm creation
            if not typer.confirm(f"Create {len(plans)} new SKAGs?", default=True):
                print_info("Cancelled")
                return

            # Create SKAGs
            created_count = 0
            for plan in plans:
                with spinner(f"Creating: {plan.ad_group_name}"):
                    # Create ad group
                    new_ag = client.campaigns(campaign_id).ad_groups.create(
                        AdGroupCreate(
                            name=plan.ad_group_name,
                            default_bid_amount=Money(
                                amount=str(plan.bid),
                                currency=plan.currency,
                            ),
                            automated_keywords_opt_in=False,
                        )
                    )

                    # Add translated keyword
                    match_type = (
                        KeywordMatchType.EXACT if plan.match_type.lower() == "exact" else KeywordMatchType.BROAD
                    )

                    client.campaigns(campaign_id).ad_groups(new_ag.id).keywords.create_bulk(
                        [
                            KeywordCreate(
                                text=plan.translated_keyword,
                                match_type=match_type,
                                bid_amount=Money(
                                    amount=str(plan.bid),
                                    currency=plan.currency,
                                ),
                            )
                        ]
                    )

                created_count += 1
                print_success(f"Created: {plan.ad_group_name}")

            # Summary
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
    """List supported languages and country mappings.

    Examples:
        asa translate languages         # List all languages
        asa translate languages US      # Show languages for US
        asa translate languages ES      # Show languages for Spain
    """
    if country:
        # Show languages for specific country
        languages = get_languages_for_country(country)
        if not languages:
            print_warning(f"No language mapping found for country: {country}")
            return

        console.print(f"[bold]Languages for {country.upper()}:[/bold]")
        for lang in languages:
            console.print(f"  {lang} - {get_language_name(lang)}")
    else:
        # List all supported languages
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
    """Show current translation configuration.

    Displays API keys and default provider loaded from .env file
    or environment variables.

    Examples:
        asa translate config
        asa translate config --env-file .env.production
    """
    settings = get_translate_settings(env_file)

    source = f"from {env_file}" if env_file else "from .env / environment"

    table = Table(
        title=f"Translation Configuration ({source})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Setting")
    table.add_column("Value")

    # Mask API keys for display
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
