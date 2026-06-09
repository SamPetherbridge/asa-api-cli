"""Command-line interface for Apple Search Ads.

This module provides a CLI for interacting with the Apple Search Ads API.

Usage:
    asa --help
    asa campaigns list
    asa reports campaigns --start 2024-01-01 --end 2024-01-31
"""

from importlib.metadata import PackageNotFoundError, version

from asa_api_cli.main import app

__all__ = ["app"]

try:
    __version__ = version("asa-api-cli")
except PackageNotFoundError:  # pragma: no cover - only when running from a non-installed checkout
    __version__ = "0.0.0"
