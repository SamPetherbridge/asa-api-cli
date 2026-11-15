"""Change history audit log CLI commands for Apple Ads Platform API v1."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

import typer
from asa_api_client.exceptions import AppleSearchAdsError
from asa_api_client.v1.models.change_history import AuditEventType, AuditSummary
from asa_api_client.v1.query import Query

from asa_api_cli.utils import (
    EXIT_ERROR,
    EXIT_USAGE,
    OutputFormat,
    cli_state,
    enum_value,
    get_client,
    handle_api_error,
    output_data,
    output_json,
    parse_date,
    print_error,
    print_result_panel,
    print_warning,
    spinner,
)

app = typer.Typer(help="Inspect ad account change history and field-level audit details")

DEFAULT_ENTITY_TYPES = [
    "Campaign",
    "AdGroup",
    "Keyword",
    "NegativeKeyword",
    "Ad",
    "Creative",
    "SharedBudget",
]


class MetadataMode(StrEnum):
    """Supported ``options.metadata`` values for change-history queries."""

    NONE = "none"
    LATEST = "latest"
    SNAPSHOT = "snapshot"


@app.command("list")
def list_changes(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Lookback window in days when --start is omitted (max 180)"),
    ] = 7,
    start: Annotated[
        str | None,
        typer.Option("--start", "-s", help="Start date (YYYY-MM-DD)"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", "-e", help="End date (YYYY-MM-DD)"),
    ] = None,
    entity_types: Annotated[
        list[str] | None,
        typer.Option(
            "--entity-type",
            "-t",
            help="Filter by entity type(s) (e.g. Campaign, AdGroup, Keyword, NegativeKeyword, Ad)",
        ),
    ] = None,
    event_type: Annotated[
        AuditEventType | None,
        typer.Option("--event-type", help="Filter by event type (CREATE, UPDATE, DELETE)"),
    ] = None,
    metadata: Annotated[
        MetadataMode,
        typer.Option("--metadata", "-m", help="Metadata mode: latest, snapshot, or none"),
    ] = MetadataMode.LATEST,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum audit summary rows to return"),
    ] = 50,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """List account change history grouped by transaction.

    Examples:
        asa change-history list --days 7
        asa change-history list --entity-type Campaign --event-type UPDATE
        asa change-history list --start 2026-08-01 --end 2026-08-15
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format

    if days < 1 or days > 180:
        print_error(
            "Invalid Lookback Window",
            f"--days must be between 1 and 180 (got {days}).",
            hint="Change history supports up to 6 months of audit retention.",
        )
        raise typer.Exit(EXIT_USAGE)

    query = Query()
    if start and end:
        start_dt = f"{parse_date(start).isoformat()}T00:00:00"
        end_dt = f"{parse_date(end).isoformat()}T23:59:59"
        query = query.where("eventTime", "BETWEEN", [start_dt, end_dt])
    elif start:
        start_dt = f"{parse_date(start).isoformat()}T00:00:00"
        query = query.where("eventTime", "GREATER_THAN", start_dt)
    else:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        query = query.where("eventTime", "GREATER_THAN", cutoff.strftime("%Y-%m-%dT%H:%M:%S"))

    effective_entity_types = entity_types or DEFAULT_ENTITY_TYPES
    query = query.where("entityType", "IN", effective_entity_types)

    if event_type is not None:
        query = query.where("eventType", "EQUALS", event_type.value)

    query = query.page(size=limit)

    client = get_client()

    try:
        with client:
            with spinner("Fetching change history..."):
                page = client.change_history.query(
                    query,
                    options={"metadata": metadata.value, "needTotals": "true"},
                )
            summaries: list[AuditSummary] = page.result

            if effective_format == OutputFormat.JSON:
                output_json(summaries)
                return

            rows: list[dict[str, Any]] = []
            for s in summaries:
                first_detail_id = "-"
                if s.metas:
                    for meta_entry in s.metas:
                        if meta_entry.detail_id:
                            first_detail_id = meta_entry.detail_id
                            break
                rows.append(
                    {
                        "event_time": s.event_time.strftime("%Y-%m-%d %H:%M:%S") if s.event_time else "-",
                        "event_type": enum_value(s.event_type),
                        "entity_type": s.entity_type or "-",
                        "count": s.count if s.count is not None else len(s.metas),
                        "user_type": enum_value(s.user_type),
                        "detail_id": first_detail_id,
                    }
                )

            output_data(
                rows,
                columns=["event_time", "event_type", "entity_type", "count", "user_type", "detail_id"],
                format=effective_format,
                title="Change History",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("details")
def get_change_details(
    detail_id: Annotated[
        str,
        typer.Argument(help="Composite detail ID (e.g. Campaign.444555666.txn_abc123)"),
    ],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum field changes to return"),
    ] = 100,
    format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table, json, or csv"),
    ] = OutputFormat.TABLE,
) -> None:
    """Get field-level before/after values for a single change detail ID.

    Examples:
        asa change-history details Campaign.444555666.txn_abc123
    """
    effective_format = OutputFormat.JSON if cli_state.json_output else format
    client = get_client()

    try:
        with client:
            with spinner(f"Fetching change details for {detail_id}..."):
                page = client.change_history.get_details(detail_id, limit=limit)

            if effective_format == OutputFormat.JSON:
                output_json(page.result)
                return

            if not page.result:
                print_warning(f"No change details found for {detail_id}")
                return

            record = page.result[0]
            change_rows: list[dict[str, Any]] = []
            for group in record.details:
                for change in group.changes:
                    change_rows.append(
                        {
                            "field": change.field or "-",
                            "old_values": ", ".join(change.old_values) if change.old_values else "-",
                            "new_values": ", ".join(change.new_values) if change.new_values else "-",
                        }
                    )

            if effective_format == OutputFormat.CSV:
                output_data(
                    change_rows,
                    columns=["field", "old_values", "new_values"],
                    format=OutputFormat.CSV,
                )
                return

            meta_summary = (
                ", ".join(f"{k}={v}" for k, v in record.entity_meta_data.items()) if record.entity_meta_data else "-"
            )
            print_result_panel(
                f"Change Details ({record.detail_id or detail_id})",
                {
                    "Entity Type": record.entity_type or "-",
                    "Entity ID": record.entity_id or "-",
                    "Event Type": enum_value(record.event_type),
                    "Event Time": (record.event_time.strftime("%Y-%m-%d %H:%M:%S UTC") if record.event_time else "-"),
                    "User Type": enum_value(record.user_type),
                    "Modified By": record.modified_by or "-",
                    "Metadata": meta_summary,
                },
                style="cyan",
            )
            output_data(
                change_rows,
                columns=["field", "old_values", "new_values"],
                format=OutputFormat.TABLE,
                title="Field Changes",
            )

    except AppleSearchAdsError as e:
        handle_api_error(e)
        raise typer.Exit(EXIT_ERROR) from None
