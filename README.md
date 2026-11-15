# ASA API CLI

[![PyPI version](https://img.shields.io/pypi/v/asa-api-cli.svg)](https://pypi.org/project/asa-api-cli/)
[![Python](https://img.shields.io/pypi/pyversions/asa-api-cli.svg)](https://pypi.org/project/asa-api-cli/)
[![License](https://img.shields.io/github/license/SamPetherbridge/asa-api-cli.svg)](https://github.com/SamPetherbridge/asa-api-cli/blob/main/LICENSE)
[![CI](https://github.com/SamPetherbridge/asa-api-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/SamPetherbridge/asa-api-cli/actions/workflows/ci.yml)

A command-line interface for managing Apple Ads campaigns on the **Apple Ads Platform API v1**.

Built on top of [asa-api-client](https://github.com/SamPetherbridge/asa-api-client).

## Installation

Using [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install asa-api-cli

# Include Excel workbook analysis support (pandas + xlsxwriter)
uv tool install "asa-api-cli[analyze]"
```

Using pip:

```bash
pip install asa-api-cli

# Or with Excel workbook analysis support
pip install "asa-api-cli[analyze]"
```

Using pipx:

```bash
pipx install asa-api-cli
```

## Setup

Configure your Apple Ads Platform API v1 OAuth2 credentials via environment variables or a `.env` file:

```bash
export ASA_CLIENT_ID="SEARCHADS.your-client-id"
export ASA_TEAM_ID="SEARCHADS.your-team-id"
export ASA_KEY_ID="your-key-id"
export ASA_AD_ACCOUNT_ID="123456"
export ASA_PRIVATE_KEY_PATH="/path/to/private-key.pem"
```

If your API user only accesses a single ad account (or `ASA_ORG_ID` is set), `asa` will resolve the ad account automatically. For multi-account setups, discover accounts and override per command with `--ad-account` / `-A`:

```bash
# List accessible ad accounts and roles
asa auth accounts

# Test Platform API v1 authentication
asa auth test

# Override the active ad account for any command
asa --ad-account 123456 campaigns list
```

## Commands

### Campaigns, Ad Groups, Keywords & Ads

```bash
# List enabled campaigns (or --all, --with-spend, --json)
asa campaigns list
asa campaigns list --all --with-spend
asa campaigns get 123456789
asa campaigns set-budget 123456789 --daily 100.00 --currency USD

# Manage ad groups (flat ad group IDs in Platform API v1)
asa ad-groups list --campaign 123456789
asa ad-groups get 987654321
asa ad-groups set-bid 987654321 --bid 2.50
asa ad-groups set-bid 987654321 --target-cpa 8.00

# Manage keywords and negative keywords
asa keywords list --ad-group 987654321
asa keywords add "budget tracker" "expense app" --ad-group 987654321 --match EXACT --bid 2.00
asa keywords negatives list --campaign 123456789

# Manage ads and reusable creatives
asa ads list --ad-group 987654321
asa ads creatives --adam-id 654321987
asa ads create --campaign 123456789 --ad-group 987654321 --name "Holiday CPP" \
  --type CUSTOM_PRODUCT_PAGE --product-page pp-uuid
```

### Apps, Product Pages, Geo, Countries & Shared Budgets

```bash
# App Store lookup, metadata, and ad placement eligibility
asa apps search "habit tracker"
asa apps get 654321987
asa apps eligibilities 654321987

# Product pages (DPPs, CPPs, PPO variants) and localized metadata
asa product-pages list --adam-id 654321987
asa product-pages locales <page-id> --adam-id 654321987
asa product-pages app-locales 654321987

# Geo targeting, supported storefronts/languages, and shared budgets
asa geo search "New York" --country US
asa countries list
asa budget-orders list
```

### Reports & Per-App Exports

```bash
# Reconcilable, read-only per-app JSON exports for separate apps over a fixed period
asa --ad-account 123456 reports export \
  --app 654321987 --start 2026-01-01 --end 2026-01-07 --output app-654321987.json
asa --ad-account 123456 reports export \
  --app 789123456 --start 2026-01-01 --end 2026-01-07 --output app-789123456.json

# Campaign, ad group, ad, keyword, and ORTZ search-term reports
asa reports campaigns --start 2026-01-01 --end 2026-01-31
asa reports campaigns --app 654321987 -s 2026-01-01 -e 2026-01-07 --daily -o daily.csv
asa reports ad-groups 123456789 --days 14
asa reports ads 123456789 --days 14
asa reports keywords 123456789 --days 30
asa reports search-terms 123456789 --days 14 --output search-terms.csv
```

#### `asa reports export` Contract

`asa reports export` produces a single structured JSON document per app without invoking any mutating API endpoints:

- **App & account isolation**: Scopes strictly to `--app <adamId>` within the resolved ad account (`--ad-account` / `ASA_AD_ACCOUNT_ID`), recording `account` (`ad_account_id`, `org_id`, `currency`, `account_timezone`), `app` (`adam_id`, `app_name`), `period`, `collected_at`, and `versions`. Repeat runs over the same `--start` / `--end` dates support downstream restatement diffing.
- **Stable entity keys & daily reconciliation**: Emits both `daily` observations and `period_totals` across `campaigns`, `ad_groups`, `ads`, `keywords`, and `search_terms`, keyed by stable entity IDs (and parent IDs + dimensions on `search_terms`) rather than mutable names. Renamed entities reconcile by ID with historical names recorded in `observed_names`.
- **Unambiguous attribution**: Exposes `tap_*` (`tap_installs`, `tap_new_downloads`, `tap_redownloads`, `tap_install_cpi`, `tap_install_rate`), `view_*` (`view_installs`, `view_new_downloads`, `view_redownloads`; CPI/rate remain `null` as Apple Ads does not define view-only CPI), and `total_*` (`total_installs`, `total_new_downloads`, `total_redownloads`, `total_avg_cpi`, `total_install_rate`). Period ratios are calculated from summed period numerators and denominators (never by averaging daily ratios), valid zeroes are preserved, and missing values stay `null`.
- **Timezone policy**: `campaigns`, `ad_groups`, `ads`, and `keywords` support `--timezone ORTZ` (default) or `--timezone UTC`. `search_terms` is fixed to `ORTZ` by Apple Ads Platform API v1 (and limited to the trailing 90 days); under `--timezone UTC`, `search_terms` is marked `"status": "unavailable"` (`reason: "timezone_incompatible"`) by default rather than silently mixed with UTC daily data (pass `--include-ortz-search-terms` to opt into a separate ORTZ-tagged section).
- **Completeness & exit codes**: Top-level `status` is `"complete"` or `"empty"` (`complete: true`, exit code `0`), or `"partial"` / `"failed"` (`complete: false`, exit code `1` with per-scope pagination and error records in `errors`). `--output` writes atomically on every outcome so stale files from earlier runs are never mistaken for current results.

#### Read-Only Live Verification Procedure

To verify per-app export reconciliation against a live account using read-only endpoints only:

```bash
# 1. Confirm read-only Platform API v1 connectivity and inspect accessible accounts/apps
asa v1-smoke
asa auth accounts
asa apps search --own

# 2. Export two apps separately over an explicit completed 7-day range (ORTZ)
asa --ad-account "$ASA_AD_ACCOUNT_ID" reports export \
  --app <APP_1_ADAM_ID> --start 2026-10-01 --end 2026-10-07 -o /tmp/app1.json
asa --ad-account "$ASA_AD_ACCOUNT_ID" reports export \
  --app <APP_2_ADAM_ID> --start 2026-10-01 --end 2026-10-07 -o /tmp/app2.json

# 3. Verify completeness, zero cross-app campaign overlap, and daily-to-period reconciliation
jq '{status, complete, app, reconciliation: .reconciliation.daily_to_period_matches, errors}' /tmp/app1.json /tmp/app2.json
jq -s '(.[0].scope.campaign_ids - (.[0].scope.campaign_ids - .[1].scope.campaign_ids)) | length == 0' /tmp/app1.json /tmp/app2.json
```

### Recommendations, Suggestions & Change History

```bash
# View, apply, or dismiss Apple Ads optimization recommendations
asa recommendations target-cpas
asa recommendations daily-budgets
asa recommendations apply <rec-id> --type target-cpa
asa recommendations dismiss <rec-id>

# Keyword, phrase, category, and Target CPA suggestions
asa suggestions keywords --adam-id 654321987 --country US
asa suggestions phrases --adam-id 654321987 --query-type SEARCH --phrase "budget planner"
asa suggestions categories --adam-id 654321987
asa suggestions target-cpa --adam-id 654321987 --country US

# Account change history and field-level audit details
asa change-history list --days 7
asa change-history details Campaign.444555666.txn_abc123
```

### Impression Share & Search Popularity Insights

```bash
# Daily/weekly search term impression share (synchronous v1 Insights API)
asa impression-share analyze --country US --days 7

# Weekly search term popularity index (1-5)
asa impression-share popularity --country US --search "budget"
```

### Optimization, Brand Campaigns, Translation & Workbook Analysis

```bash
# Optimization workflows
asa optimize bid-check --threshold 0.05
asa optimize bid-review --days 30
asa optimize cpa-impact
asa optimize expand --type Generic --match EM --dry-run
asa optimize negatives --days 30

# Brand protection campaigns & multi-language keyword translation
asa brand "My App Name" -c US -c GB --dry-run
asa translate detect

# Excel performance analysis workbook & Platform API v1 smoke test
asa analyze --period 30d --output asa-30d.xlsx
asa v1-smoke
```

## Shell Completion

Install shell completion for your shell:

```bash
# Bash
asa --install-completion bash

# Zsh
asa --install-completion zsh

# Fish
asa --install-completion fish
```

## License

MIT License - Copyright (c) 2025 Peth Pty Ltd
