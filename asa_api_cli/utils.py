"""Shared utilities for CLI commands."""

import csv
import json
import re
import sys
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Any, TypeVar

import typer
from asa_api_client.exceptions import (
    AppleSearchAdsError,
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    PartialFailureError,
    RateLimitError,
)
from asa_api_client.exceptions import (
    ValidationError as ApiValidationError,
)
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.models.ad_groups import AdGroup
from asa_api_client.v1.models.base import Money
from asa_api_client.v1.models.campaigns import Campaign, PromotedObjectType
from asa_api_client.v1.models.shared import BidStrategy, BidStrategyGoal, BidStrategyType
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.theme import Theme

# Consistent color theme matching Python's modern CLI aesthetic
APP_THEME = Theme(
    {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red bold",
        "muted": "dim",
        "highlight": "magenta",
        "header": "bold cyan",
        "key": "cyan",
        "value": "white",
        "status.enabled": "green",
        "status.paused": "yellow",
        "status.deleted": "red dim",
        "money": "green",
        "number": "cyan",
        "label": "dim",
    }
)

console = Console(theme=APP_THEME)
error_console = Console(stderr=True, theme=APP_THEME)

T = TypeVar("T")

# Exit codes following Unix conventions
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_RATE_LIMIT = 5
EXIT_CONFIG = 6


# Global state for CLI options (quiet, verbose, no_input, json_output, ad account override)
class CLIState:
    """Global CLI state."""

    quiet: bool = False
    verbose: bool = False
    no_input: bool = False
    json_output: bool = False
    ad_account_id: str | int | None = None


cli_state = CLIState()


def init_consoles(*, no_color: bool = False) -> None:
    """Reinitialize consoles with updated color settings."""
    global console, error_console
    console = Console(theme=APP_THEME, no_color=no_color)
    error_console = Console(stderr=True, theme=APP_THEME, no_color=no_color)


class OutputFormat(str, Enum):
    """Output format options."""

    TABLE = "table"
    JSON = "json"
    CSV = "csv"


def enum_value(val: Enum | str | bool | None) -> str:
    """Extract string value from an enum, bool, or forward-compatible string field."""
    if val is None:
        return ""
    if isinstance(val, Enum):
        return str(val.value)
    if isinstance(val, bool):
        return "OPT_IN" if val else "OPT_OUT"
    return str(val)


def require_id(entity_id: int | None, label: str = "resource") -> int:
    """Ensure a Platform API v1 model carries a non-null integer ID."""
    if entity_id is None:
        raise AppleSearchAdsError(f"API response for {label} did not include an ID")
    return entity_id


def get_client(
    *,
    require_account: bool = True,
    env_file: Path | str | None = ".env",
) -> AppleAdsClient:
    """Create and configure an AppleAdsClient (Platform API v1) from environment.

    Args:
        require_account: When True, ensure an ad account ID is resolved
            (via ``ASA_AD_ACCOUNT_ID``, ``--ad-account``, ``ASA_ORG_ID``,
            or automatic single-account discovery through ``/v1/acls``).
        env_file: Path to ``.env`` file to load credentials from.

    Returns:
        Configured ``AppleAdsClient`` instance.
    """
    try:
        client = AppleAdsClient.from_env(env_file=env_file)
    except ConfigurationError as e:
        print_error(
            "Configuration Error",
            str(e),
            hint="Set ASA_CLIENT_ID, ASA_TEAM_ID, ASA_KEY_ID, and ASA_PRIVATE_KEY_PATH (or ASA_PRIVATE_KEY).",
        )
        raise typer.Exit(EXIT_CONFIG) from None
    except ValidationError as e:
        missing = []
        for err in e.errors():
            if err["type"] == "missing" and err["loc"]:
                field = str(err["loc"][0]).upper()
                missing.append(f"ASA_{field}")
        if missing:
            print_error(
                "Missing Configuration",
                f"Required environment variables not set: {', '.join(missing)}",
                hint="Create a .env file or export the required ASA_* variables. Run 'asa auth --help' for details.",
            )
        else:
            err_details = "; ".join(str(err.get("msg", err)) for err in e.errors())
            print_error("Invalid Configuration", err_details or str(e))
        raise typer.Exit(EXIT_CONFIG) from None

    if cli_state.ad_account_id is not None:
        client = client.with_account(cli_state.ad_account_id)

    if require_account and client.ad_account_id is None:
        try:
            client.resolve_ad_account()
        except ConfigurationError as e:
            client.close()
            print_error(
                "Ad Account Selection Required",
                str(e),
                hint="Set ASA_AD_ACCOUNT_ID, pass --ad-account <id>, or run 'asa auth accounts' to list accounts.",
            )
            raise typer.Exit(EXIT_CONFIG) from None
        except AppleSearchAdsError as e:
            client.close()
            handle_api_error(e)
            raise typer.Exit(EXIT_ERROR) from None

    return client


def print_error(
    title: str,
    message: str,
    hint: str | None = None,
    details: str | None = None,
) -> None:
    """Print a styled error message to stderr."""
    error_console.print(f"[error]✗ {escape(title)}:[/error] {escape(message)}")
    if details:
        error_console.print(f"  [muted]{escape(details)}[/muted]")
    if hint:
        error_console.print(f"  [muted]Hint: {escape(hint)}[/muted]")


def print_success(message: str, details: str | None = None) -> None:
    """Print a styled success message."""
    if not cli_state.quiet:
        console.print(f"[success]✓[/success] {escape(message)}")
        if details:
            console.print(f"  [muted]{escape(details)}[/muted]")


def print_warning(message: str) -> None:
    """Print a styled warning message."""
    if not cli_state.quiet:
        console.print(f"[warning]![/warning] {message}")


def print_info(message: str) -> None:
    """Print a styled info message."""
    if not cli_state.quiet:
        console.print(f"[info]ℹ[/info] {message}")


def print_info_panel(title: str, content: str) -> None:
    """Print information in a styled panel."""
    if cli_state.quiet:
        return
    panel = Panel(
        content,
        title=f"[info]{title}[/info]",
        border_style="cyan",
        padding=(0, 1),
    )
    error_console.print(panel)


def print_hint(command: str) -> None:
    """Print a hint suggesting the next command."""
    if not cli_state.quiet:
        error_console.print(f"[muted]  Hint: {command}[/muted]")


def handle_api_error(error: AppleSearchAdsError) -> None:
    """Format and print API errors with appropriate styling.

    Callers should raise ``typer.Exit`` with the desired exit code after
    invoking ``handle_api_error``.
    """
    if isinstance(error, AuthenticationError):
        print_error(
            "Authentication Failed",
            error.message,
            hint="Check your credentials with 'asa auth show' or test with 'asa auth test'.",
        )
    elif isinstance(error, NotFoundError):
        print_error("Not Found", error.message)
    elif isinstance(error, RateLimitError):
        retry_msg = f" Retry after {error.retry_after}s." if error.retry_after else ""
        print_error(
            "Rate Limit Exceeded",
            f"{error.message}{retry_msg}",
            hint="Wait before retrying or reduce request frequency.",
        )
    elif isinstance(error, (ApiValidationError, PartialFailureError)):
        title = "Partial Failure" if isinstance(error, PartialFailureError) else "Validation Error"
        detail_lines: list[str] = []
        if isinstance(error, ApiValidationError) and error.field_errors:
            for field_name, field_errs in error.field_errors.items():
                for err_msg in field_errs:
                    detail_lines.append(f"  • {field_name}: {err_msg}")
        details_list = getattr(error, "details", None) or getattr(error, "errors", None) or []
        for err in details_list[:10]:
            field = err.get("field") or (err.get("info") or {}).get("field") or ""
            msg = err.get("message", "")
            code = err.get("messageCode") or err.get("code") or ""
            prefix = f"{field}: " if field else ""
            suffix = f" ({code})" if code and code != msg else ""
            detail_lines.append(f"  • {prefix}{msg}{suffix}")
        extra = "\n" + "\n".join(detail_lines) if detail_lines else ""
        print_error(title, f"{error.message}{extra}")
    elif isinstance(error, ConfigurationError):
        print_error("Configuration Error", error.message)
    else:
        status = f" (HTTP {error.status_code})" if error.status_code else ""
        print_error(f"API Error{status}", error.message)


@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Show a spinner while executing an operation."""
    if cli_state.quiet or not sys.stdout.isatty():
        yield
    else:
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(description=message, total=None)
            yield


def create_progress() -> Progress:
    """Create a progress bar for iteration."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=error_console,
    )


def iterate_with_progress(
    items: Iterator[T],
    total: int | None = None,
    description: str = "Processing...",
) -> Iterator[T]:
    """Iterate over items with a progress bar."""
    if cli_state.quiet or not error_console.is_terminal:
        yield from items
        return
    with create_progress() as progress:
        task = progress.add_task(description, total=total)
        for item in items:
            yield item
            progress.advance(task)


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        print_error(
            "Invalid Date",
            f"'{date_str}' is not a valid date",
            hint="Use YYYY-MM-DD format (e.g., 2026-01-15).",
        )
        raise typer.Exit(EXIT_USAGE) from None


def save_to_file(content: str, path: Path) -> None:
    """Save text content to a file and print confirmation."""
    path.write_text(content)
    print_success(f"Saved to {path}")


def confirm_action(message: str, default: bool = False) -> bool:
    """Ask for interactive confirmation, respecting ``--no-input``."""
    if cli_state.no_input:
        print_error(
            "Input Required",
            f"Confirmation needed: {message}",
            details="Use --force / --yes to skip confirmations in non-interactive mode.",
        )
        raise typer.Exit(EXIT_ERROR)
    return typer.confirm(message, default=default)


def format_money(amount: str | None, currency: str | None = "") -> str:
    """Format a monetary amount cleanly."""
    if amount is None:
        return "-"
    try:
        val = float(amount)
        formatted = f"{val:,.2f}"
    except ValueError:
        formatted = amount
    return f"{formatted} {currency}".strip()


def format_money_obj(money: Money | None) -> str:
    """Format a v1 ``Money`` object cleanly."""
    if money is None or money.amount is None:
        return "-"
    return format_money(money.amount, money.currency or "")


def format_bid_strategy(bs: BidStrategy | None) -> str:
    """Format a v1 ``BidStrategy`` for compact display."""
    if bs is None:
        return "-"
    strategy_type = enum_value(bs.bid_strategy_type)
    money = bs.bid
    has_amount = False
    if money and money.amount is not None:
        try:
            has_amount = Decimal(money.amount) > 0 or bool(money.currency)
        except Exception:
            has_amount = True
    if has_amount and money is not None:
        money_str = format_money_obj(money)
        return f"{money_str} ({strategy_type})" if strategy_type else money_str
    return strategy_type or "-"


def campaign_countries(campaign: Campaign) -> list[str]:
    """Extract targeted country/region codes from a v1 ``Campaign``."""
    if campaign.targeting and campaign.targeting.country_or_region and campaign.targeting.country_or_region.include:
        return [str(code) for code in campaign.targeting.country_or_region.include]
    return []


def campaign_daily_budget(campaign: Campaign) -> Money | None:
    """Extract the daily budget ``Money`` from a v1 ``Campaign``."""
    if campaign.daily_budget and campaign.daily_budget.value:
        return campaign.daily_budget.value
    return None


def campaign_currency(campaign: Campaign, default: str = "USD") -> str:
    """Best-effort currency lookup from a v1 ``Campaign``."""
    budget = campaign_daily_budget(campaign)
    if budget and budget.currency:
        return budget.currency
    if campaign.bid_strategy and campaign.bid_strategy.bid and campaign.bid_strategy.bid.currency:
        return campaign.bid_strategy.bid.currency
    return default


def campaign_adam_id(campaign: Campaign) -> int | None:
    """Extract numeric App Store ``adamId`` when campaign promotes an ``APPSTORE_APP``."""
    if enum_value(campaign.promoted_object_type) != PromotedObjectType.APPSTORE_APP.value:
        return None
    if not campaign.promoted_object_id:
        return None
    try:
        return int(campaign.promoted_object_id)
    except ValueError:
        return None


def ad_group_bid_money(ad_group: AdGroup) -> Money | None:
    """Extract the bid ``Money`` from an ad group's ``BidStrategy``, ignoring zero sentinels."""
    if not ad_group.bid_strategy or not ad_group.bid_strategy.bid:
        return None
    bid = ad_group.bid_strategy.bid
    if bid.amount is None:
        return None
    if bid.currency is None:
        try:
            if Decimal(bid.amount) == 0:
                return None
        except Exception:
            pass
    return bid


def manual_cpt_bid_strategy(amount: str | Decimal | float, currency: str) -> BidStrategy:
    """Build a v1 ``MANUAL_CPT`` + ``TAP`` ``BidStrategy``."""
    return BidStrategy(
        bid_strategy_type=BidStrategyType.MANUAL_CPT,
        bid_strategy_goal=BidStrategyGoal.TAP,
        bid=Money(amount=str(amount), currency=currency),
    )


def target_cpa_bid_strategy(
    amount: str | Decimal | float | None,
    currency: str = "USD",
) -> BidStrategy:
    """Build a v1 ``MAX_CONVERSIONS`` + ``INSTALL`` ``BidStrategy``."""
    return BidStrategy(
        bid_strategy_type=BidStrategyType.MAX_CONVERSIONS,
        bid_strategy_goal=BidStrategyGoal.INSTALL,
        bid=Money(amount=str(amount), currency=currency) if amount is not None else None,
    )


def format_status(status: str) -> str:
    """Return status with rich markup for colored display."""
    status_upper = status.upper()
    if status_upper in ("ENABLED", "ACTIVE", "RUNNING", "VALID", "AVAILABLE", "APPLIED", "PUBLISHED"):
        return f"[status.enabled]● {status}[/status.enabled]"
    elif status_upper in ("PAUSED", "NOT_RUNNING", "ON_HOLD", "LIMITED", "PROCESSING", "PENDING", "DISMISSED"):
        return f"[status.paused]◐ {status}[/status.paused]"
    elif status_upper in ("DELETED", "DELETE", "INVALID", "CANCELED", "EXHAUSTED", "DISABLED"):
        return f"[status.deleted]○ {status}[/status.deleted]"
    return status


def format_number(num: int | float | None) -> str:
    """Format a number with thousands separators."""
    if num is None:
        return "-"
    if isinstance(num, float):
        return f"{num:,.2f}"
    return f"{num:,}"


def format_percent(rate: float | None) -> str:
    """Format a decimal rate as a percentage."""
    if rate is None:
        return "-"
    return f"{rate * 100:.2f}%"


RIGHT_ALIGN_COLS = {"id", "budget", "daily_budget", "bid", "impressions", "taps", "spend"}
STATUS_COLS = {"status", "serving_status", "system_status", "display_status", "state"}


def output_table(
    data: Sequence[dict[str, Any]],
    columns: list[str],
    title: str | None = None,
    column_styles: dict[str, str] | None = None,
    column_labels: dict[str, str] | None = None,
) -> None:
    """Output data as a clean, minimal Rich table."""
    table = Table(
        title=title,
        show_header=True,
        header_style="header",
        border_style="muted",
        title_style="bold",
        pad_edge=True,
        collapse_padding=True,
    )

    styles = column_styles or {}
    labels = column_labels or {}
    for col in columns:
        display_name = labels.get(col, col.replace("_", " ").title())
        justify: Any = "right" if col in RIGHT_ALIGN_COLS else "left"
        style = styles.get(col)
        table.add_column(display_name, justify=justify, style=style)

    for row in data:
        values = []
        for col in columns:
            val = row.get(col, "")
            if val is None or val == "":
                values.append("[muted]-[/muted]")
            elif col in STATUS_COLS and isinstance(val, str):
                values.append(format_status(val))
            else:
                values.append(str(val))
        table.add_row(*values)

    console.print(table)


def output_json(data: Any) -> None:
    """Output data as formatted JSON to stdout."""
    if hasattr(data, "model_dump"):
        data = data.model_dump(by_alias=True)
    elif isinstance(data, list) and data and hasattr(data[0], "model_dump"):
        data = [item.model_dump(by_alias=True) for item in data]

    print(json.dumps(data, indent=2, default=str))


def print_json(data: Any, title: str | None = None) -> None:
    """Alias for ``output_json`` preserving optional title signature."""
    del title
    output_json(data)


def output_csv(data: Sequence[dict[str, Any]], columns: list[str]) -> None:
    """Output data as CSV to stdout."""
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in data:
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, str) and "[" in v:
                v = re.sub(r"\[/?[^\]]+\]", "", v)
            clean_row[k] = v
        writer.writerow(clean_row)
    print(output.getvalue(), end="")


def output_data(
    data: Sequence[dict[str, Any]],
    columns: list[str],
    format: OutputFormat = OutputFormat.TABLE,
    title: str | None = None,
    column_styles: dict[str, str] | None = None,
    column_labels: dict[str, str] | None = None,
) -> None:
    """Output data in the specified format (respecting global ``--json``)."""
    if cli_state.json_output:
        format = OutputFormat.JSON

    if not data:
        if format == OutputFormat.JSON:
            print("[]")
        elif format == OutputFormat.CSV:
            print(",".join(columns))
        else:
            print_warning("No results found")
        return

    if format == OutputFormat.TABLE:
        output_table(data, columns, title, column_styles, column_labels)
    elif format == OutputFormat.JSON:
        output_json(list(data))
    elif format == OutputFormat.CSV:
        output_csv(data, columns)


def print_result_panel(
    title: str,
    fields: dict[str, Any],
    style: str = "green",
) -> None:
    """Print a styled result panel for single-item operations."""
    if cli_state.quiet:
        return

    lines = []
    max_key_len = max(len(k) for k in fields) if fields else 0
    for key, val in fields.items():
        padding = " " * (max_key_len - len(key))
        if val is None or val == "":
            formatted_val = "[muted]-[/muted]"
        elif key.lower() in ("status", "serving status", "system status", "display status", "state"):
            formatted_val = format_status(str(val))
        else:
            formatted_val = f"[value]{val}[/value]"
        lines.append(f"[label]{key}:[/label]{padding}  {formatted_val}")

    panel = Panel(
        "\n".join(lines),
        title=f"[{style}]{title}[/{style}]",
        border_style=style,
        padding=(0, 1),
    )
    console.print(panel)
