"""Authentication CLI commands (Platform API v1)."""

from pathlib import Path
from typing import Annotated, Any, Self

import typer
from asa_api_client.exceptions import AppleSearchAdsError, ConfigurationError
from asa_api_client.v1.client import AppleAdsClient
from asa_api_client.v1.query import Query
from pydantic import SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.table import Table

from asa_api_cli.utils import (
    EXIT_AUTH,
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    cli_state,
    console,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    print_error,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="Manage authentication and credentials")


class AuthSettings(BaseSettings):  # type: ignore[misc]
    """Platform API v1 authentication settings loaded from environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="ASA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id: str
    team_id: str
    key_id: str
    ad_account_id: str | None = None
    org_id: int | None = None
    private_key: SecretStr | None = None
    private_key_path: Path | None = None

    @model_validator(mode="after")
    def _validate_private_key_source(self) -> Self:
        has_key = self.private_key is not None
        has_path = self.private_key_path is not None
        if not has_key and not has_path:
            raise ValueError(
                "Either private_key or private_key_path must be provided (ASA_PRIVATE_KEY or ASA_PRIVATE_KEY_PATH)"
            )
        if has_key and has_path:
            raise ValueError("Provide only one of private_key or private_key_path, not both")
        return self


def _load_auth_settings(env_file: Path | None) -> AuthSettings:
    """Load ``AuthSettings`` or exit with ``EXIT_USAGE`` on validation error."""
    try:
        if env_file is not None:
            effective = env_file if env_file.exists() else None
            return AuthSettings(_env_file=effective)  # type: ignore[call-arg]
        default_env = Path(".env")
        return AuthSettings(_env_file=default_env if default_env.exists() else None)  # type: ignore[call-arg]
    except ValidationError as e:
        missing = []
        for err in e.errors():
            if err["type"] == "missing" and err["loc"]:
                field = str(err["loc"][0]).upper()
                missing.append(f"ASA_{field}")
        if missing:
            print_error(
                "Missing Configuration",
                f"Required variables not set: {', '.join(missing)}",
                hint="Set environment variables or create a .env file.",
            )
        else:
            details = "; ".join(str(err.get("msg", err)) for err in e.errors())
            print_error("Invalid Configuration", details or str(e))
        raise typer.Exit(EXIT_USAGE) from None


@app.command("test")
def test_auth(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file"),
    ] = None,
) -> None:
    """Test authentication against the Apple Ads Platform API v1.

    Verifies your OAuth2 credentials, inspects accessible ad accounts via
    ``/v1/me`` and ``/v1/acls``, and runs a lightweight campaign query on
    the resolved ad account.

    Examples:
        asa auth test
        asa auth test --env-file .env.production
    """
    _load_auth_settings(env_file)

    try:
        client = AppleAdsClient.from_env(env_file=env_file or ".env")
        if cli_state.ad_account_id is not None:
            client = client.with_account(cli_state.ad_account_id)
    except ConfigurationError as e:
        print_error("Configuration Error", str(e))
        raise typer.Exit(EXIT_CONFIG) from None

    try:
        with client:
            with spinner("Testing Platform API v1 authentication..."):
                me = client.ad_accounts.me()
                acls = client.acls.list()

                total_campaigns: str = "-"
                resolved_account: str = client.ad_account_id or "<multiple - set ASA_AD_ACCOUNT_ID>"
                try:
                    resolved_account = client.resolve_ad_account()
                    page = client.campaigns.query(Query().page(size=1, fetch_total_count=True))
                    total = page.pagination.total_count if page.pagination else None
                    total_campaigns = f"{total:,}" if total is not None else str(len(page))
                except ConfigurationError:
                    pass

            print_result_panel(
                "Authentication Successful",
                {
                    "API": "Apple Ads Platform API v1",
                    "User ID": str(me.user_id) if me.user_id is not None else "-",
                    "Organization ID": str(me.org_id) if me.org_id is not None else "-",
                    "Ad Account ID": resolved_account,
                    "Accessible Accounts": str(len(acls)),
                    "Total Campaigns": total_campaigns,
                },
            )
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_AUTH) from None


@app.command("show")
def show_config(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", "-e", help="Path to .env file"),
    ] = None,
) -> None:
    """Show current authentication configuration.

    Displays configured credentials (with sensitive values masked)
    and verifies private key accessibility.

    Examples:
        asa auth show
        asa auth show --env-file .env.staging
    """
    settings = _load_auth_settings(env_file)
    source = f"from {env_file}" if env_file else "from .env / environment"

    table = Table(
        title=f"Configuration ({source})",
        show_header=True,
        header_style="header",
        border_style="muted",
    )
    table.add_column("Variable", style="key")
    table.add_column("Value")

    client_id_display = settings.client_id[:25] + "..." if len(settings.client_id) > 25 else settings.client_id
    table.add_row("ASA_CLIENT_ID", client_id_display)
    table.add_row("ASA_TEAM_ID", settings.team_id)
    table.add_row("ASA_KEY_ID", settings.key_id)

    effective_account = str(cli_state.ad_account_id) if cli_state.ad_account_id is not None else settings.ad_account_id
    if effective_account:
        table.add_row("ASA_AD_ACCOUNT_ID", effective_account)
    else:
        table.add_row("ASA_AD_ACCOUNT_ID", "[muted]<auto-discover via /v1/acls>[/muted]")

    if settings.org_id is not None:
        table.add_row("ASA_ORG_ID", str(settings.org_id))

    if settings.private_key_path:
        exists = settings.private_key_path.exists()
        if exists:
            status = "[success]✓ file exists[/success]"
        else:
            status = "[error]✗ file not found[/error]"
        table.add_row("ASA_PRIVATE_KEY_PATH", f"{settings.private_key_path} ({status})")
    elif settings.private_key:
        table.add_row("ASA_PRIVATE_KEY", "[muted]<inline key configured>[/muted]")

    console.print(table)


ACCOUNT_COLUMNS = [
    "ad_account_id",
    "name",
    "org_id",
    "currency",
    "time_zone",
    "payment_model",
    "status",
    "roles",
]

ORG_COLUMNS = [
    "org_id",
    "org_name",
    "currency",
    "payment_model",
    "status",
    "ad_accounts",
    "roles",
    "time_zone",
]


@app.command("accounts")
def list_accounts(
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List ad accounts accessible with current credentials.

    Uses ``GET /v1/acls`` and enriches each account via ``GET /v1/ad-accounts/{id}``
    so you can identify the right ``ASA_AD_ACCOUNT_ID`` (or ``--ad-account``).

    Examples:
        asa auth accounts
        asa auth accounts --format json
    """
    client = get_client(require_account=False)

    try:
        with client:
            with spinner("Fetching accessible ad accounts..."):
                acls = client.acls.list()

                rows: list[dict[str, Any]] = []
                for acl in acls:
                    if acl.ad_account is None or acl.ad_account.id is None:
                        continue
                    acct_id = acl.ad_account.id
                    currency = "-"
                    tz = "-"
                    payment_model = "-"
                    status = "-"
                    name = acl.ad_account.name or "-"
                    org_id: int | str = acl.ad_account.org_id if acl.ad_account.org_id is not None else "-"

                    try:
                        detail = client.with_account(acct_id).ad_accounts.get(acct_id)
                        name = detail.name or name
                        if detail.org_id is not None:
                            org_id = detail.org_id
                        currency = detail.currency or "-"
                        tz = detail.timezone or "-"
                        payment_model = enum_value(detail.payment_model) or "-"
                        status = enum_value(detail.system_status) or "-"
                    except AppleSearchAdsError:
                        pass

                    rows.append(
                        {
                            "ad_account_id": acct_id,
                            "name": name,
                            "org_id": org_id,
                            "currency": currency,
                            "time_zone": tz,
                            "payment_model": payment_model,
                            "status": status,
                            "roles": ", ".join(acl.roles) if acl.roles else "-",
                        }
                    )

            if not rows:
                print_warning("No accessible ad accounts found")
                return

            output_data(rows, ACCOUNT_COLUMNS, format, title="Accessible ad accounts")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("orgs")
def list_orgs(
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List organizations accessible with current credentials.

    Aggregates accessible ad accounts by ``orgId`` from ``GET /v1/acls`` and
    fetches organization metadata via ``GET /v1/orgs/{id}``.

    Examples:
        asa auth orgs
        asa auth orgs --format json
    """
    client = get_client(require_account=False)

    try:
        with client:
            with spinner("Fetching accessible organizations..."):
                acls = client.acls.list()

                by_org: dict[int, dict[str, Any]] = {}
                for acl in acls:
                    if acl.ad_account is None or acl.ad_account.org_id is None:
                        continue
                    oid = acl.ad_account.org_id
                    entry = by_org.setdefault(
                        oid,
                        {
                            "account_ids": [],
                            "roles": set(),
                            "fallback_name": acl.ad_account.name or "-",
                        },
                    )
                    if acl.ad_account.id is not None:
                        entry["account_ids"].append(str(acl.ad_account.id))
                    for role in acl.roles or []:
                        entry["roles"].add(role)

                rows: list[dict[str, Any]] = []
                for oid, info in sorted(by_org.items()):
                    org_name = info["fallback_name"]
                    currency = "-"
                    payment_model = "-"
                    status = "-"
                    tz = "-"
                    try:
                        org = client.orgs.get(oid)
                        org_name = org.name or org_name
                        currency = org.currency or "-"
                        payment_model = enum_value(org.payment_model) or "-"
                        status = enum_value(org.system_status) or "-"
                        tz = org.timezone or "-"
                    except AppleSearchAdsError:
                        pass

                    rows.append(
                        {
                            "org_id": oid,
                            "org_name": org_name,
                            "currency": currency,
                            "payment_model": payment_model,
                            "status": status,
                            "ad_accounts": ", ".join(info["account_ids"]) or "-",
                            "roles": ", ".join(sorted(info["roles"])) or "-",
                            "time_zone": tz,
                        }
                    )

            if not rows:
                print_warning("No accessible organizations found")
                return

            output_data(rows, ORG_COLUMNS, format, title="Accessible organizations")
    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
