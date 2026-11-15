"""Command-line interface for Apple Ads Platform API v1.

This module provides a CLI for interacting with the Apple Ads Platform API v1.

Usage:
    asa --help
    asa campaigns list
    asa reports export --app 654321987 --start 2026-01-01 --end 2026-01-07 -o app-report.json
"""

from importlib.metadata import PackageNotFoundError, version

from asa_api_cli.main import app

__all__ = ["app"]

try:
    __version__ = version("asa-api-cli")
except PackageNotFoundError:  # pragma: no cover - only when running from a non-installed checkout
    __version__ = "0.0.0"
