"""Language utilities for keyword translation.

This module provides:
- Country-to-language mappings for App Store markets
- Ad group name parsing for language detection
- Utilities for building language-tagged ad group names
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Pattern to match language tags at end of ad group names: [EN], [ES], etc.
LANGUAGE_TAG_PATTERN = re.compile(r"\s*\[([A-Z]{2})\]\s*$")

# Country code to primary language(s) mapping
# Based on App Store storefronts and primary languages
COUNTRY_LANGUAGES: dict[str, list[str]] = {
    # North America
    "US": ["en"],
    "CA": ["en", "fr"],
    # Europe - Western
    "GB": ["en"],
    "IE": ["en"],
    "DE": ["de"],
    "AT": ["de"],
    "CH": ["de", "fr", "it"],
    "FR": ["fr"],
    "BE": ["fr", "nl"],
    "NL": ["nl"],
    "LU": ["fr", "de"],
    # Europe - Southern
    "ES": ["es"],
    "IT": ["it"],
    "PT": ["pt"],
    "GR": ["el"],
    # Europe - Northern
    "SE": ["sv"],
    "NO": ["no"],
    "DK": ["da"],
    "FI": ["fi"],
    "IS": ["is"],
    # Europe - Eastern
    "PL": ["pl"],
    "CZ": ["cs"],
    "SK": ["sk"],
    "HU": ["hu"],
    "RO": ["ro"],
    "BG": ["bg"],
    "HR": ["hr"],
    "SI": ["sl"],
    "RS": ["sr"],
    "UA": ["uk"],
    "RU": ["ru"],
    # Europe - Baltic
    "LT": ["lt"],
    "LV": ["lv"],
    "EE": ["et"],
    # Latin America
    "MX": ["es"],
    "AR": ["es"],
    "CL": ["es"],
    "CO": ["es"],
    "PE": ["es"],
    "VE": ["es"],
    "EC": ["es"],
    "GT": ["es"],
    "CR": ["es"],
    "PA": ["es"],
    "DO": ["es"],
    "PR": ["es"],
    "UY": ["es"],
    "PY": ["es"],
    "BO": ["es"],
    "HN": ["es"],
    "SV": ["es"],
    "NI": ["es"],
    "BR": ["pt"],
    # Asia Pacific
    "AU": ["en"],
    "NZ": ["en"],
    "JP": ["ja"],
    "KR": ["ko"],
    "CN": ["zh"],
    "TW": ["zh"],
    "HK": ["zh", "en"],
    "SG": ["en", "zh"],
    "MY": ["ms", "en"],
    "TH": ["th"],
    "VN": ["vi"],
    "ID": ["id"],
    "PH": ["en", "tl"],
    "IN": ["en", "hi"],
    # Middle East
    "IL": ["he"],
    "AE": ["ar", "en"],
    "SA": ["ar"],
    "EG": ["ar"],
    "TR": ["tr"],
    # Africa
    "ZA": ["en", "af"],
    "NG": ["en"],
    "KE": ["en", "sw"],
}

# Language code to full name mapping
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "he": "Hebrew",
    "tr": "Turkish",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "tl": "Filipino",
    "hi": "Hindi",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "is": "Icelandic",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sl": "Slovenian",
    "sr": "Serbian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "el": "Greek",
    "af": "Afrikaans",
    "sw": "Swahili",
}


@dataclass
class AdGroupNameParts:
    """Parsed components of an ad group name."""

    match_type: str  # "Exact" or "Broad"
    keyword: str  # The English keyword
    language: str | None  # Language code (lowercase) or None if not tagged


def parse_ad_group_name(name: str) -> AdGroupNameParts:
    """Parse an ad group name to extract keyword and language.

    Expected format: "{Match Type} - {Keyword} [{Language}]"
    Examples:
        "Exact - Running Shoes [EN]" -> ("Exact", "Running Shoes", "en")
        "Broad - Fitness App" -> ("Broad", "Fitness App", None)
        "Some Other Format" -> ("Exact", "Some Other Format", None)

    Args:
        name: The ad group name to parse.

    Returns:
        AdGroupNameParts with extracted components.
    """
    # Check for language tag
    match = LANGUAGE_TAG_PATTERN.search(name)
    language = match.group(1).lower() if match else None
    name_without_tag = LANGUAGE_TAG_PATTERN.sub("", name).strip()

    # Split by " - " to get match type and keyword
    parts = name_without_tag.split(" - ", 1)

    if len(parts) == 2:
        match_type = parts[0].strip()
        keyword = parts[1].strip()
    else:
        # Fallback: assume entire name is the keyword
        match_type = "Exact"
        keyword = name_without_tag

    return AdGroupNameParts(match_type=match_type, keyword=keyword, language=language)


def build_ad_group_name(english_keyword: str, match_type: str, language: str) -> str:
    """Build an ad group name with language tag.

    The ad group name always uses the English keyword for consistency.
    The actual translated keyword is stored as the targeting keyword.

    Args:
        english_keyword: The keyword in English.
        match_type: "Exact" or "Broad".
        language: Language code (e.g., "en", "es").

    Returns:
        Ad group name like "Exact - Running Shoes [ES]".
    """
    return f"{match_type} - {english_keyword.title()} [{language.upper()}]"[:200]


def get_primary_language(country: str) -> str | None:
    """Get the primary language for a country.

    Args:
        country: ISO 3166-1 alpha-2 country code (e.g., "US", "ES").

    Returns:
        Primary language code or None if unknown.
    """
    languages = COUNTRY_LANGUAGES.get(country.upper())
    return languages[0] if languages else None


def get_languages_for_country(country: str) -> list[str]:
    """Get all languages for a country.

    Args:
        country: ISO 3166-1 alpha-2 country code.

    Returns:
        List of language codes (may be empty if country unknown).
    """
    return COUNTRY_LANGUAGES.get(country.upper(), [])


def get_language_name(code: str) -> str:
    """Get the full name of a language from its code.

    Args:
        code: ISO 639-1 language code (e.g., "en", "es").

    Returns:
        Full language name or the code if unknown.
    """
    return LANGUAGE_NAMES.get(code.lower(), code.upper())


def detect_source_language(country: str, ad_group_name: str) -> str:
    """Detect the source language from country and ad group name.

    If the ad group has a language tag, use that.
    Otherwise, infer from the campaign's country.

    Args:
        country: Campaign's country code.
        ad_group_name: Name of the ad group.

    Returns:
        Detected language code (defaults to "en" if unknown).
    """
    parts = parse_ad_group_name(ad_group_name)

    if parts.language:
        return parts.language

    # Infer from country
    primary = get_primary_language(country)
    return primary if primary else "en"
