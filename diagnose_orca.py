"""Print a bounded, allowlisted Orca account/schema diagnostic. Read-only CLI calls."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from app.orca_adapter import OrcaAdapter, OrcaError

# Field names are also allowlisted: native maps may be keyed by account names,
# email addresses or credentials. Unknown keys are counted, never printed.
FIELDS = set("""
ok result data error code version appVersion orcaVersion orca app runtime ready
connectionState connected status state available installed agents agent agentId
agentType agentName provider providerId providerType providers name id type kind
accounts account accountId accountType groups accountGroups entries items
auth authType authMethod authenticationMethod authenticationType authentication
isAuthenticated authenticated loggedIn loginStatus method mode source service
plan planType planName subscription subscriptionType subscriptionPlan tier
billing billingType billingMode cost costType route free isFree paid metered
usage usages quota quotas quotaUsed quota_used usagePercent usedPercent
usedPercentage utilization percent percentage remainingPercent remainingPercentage
remaining used consumed total limit limits remainingRequests usedRequests
requests requestLimit requestsUsed requestsRemaining count
weekly weeklyWindow weekly_window weeklyLimit daily dailyWindow dailyLimit
primary secondary windows window usageWindows rateLimits rateLimit
short long shortTerm longTerm fiveHour sevenDay fiveHourWindow sevenDayWindow
resetAt resetTime resetsAt resetAfter resetIn windowMinutes windowSeconds
unit duration interval period bucket exhausted unlimited included allowance
allowances includedAllowance enabled active expiresAt updatedAt checkedAt
details info metadata summary model modelId models effectiveModel selectedModel
defaultModel modelName modelType pricing price inputPrice outputPrice currency
quotaStatus usageStatus usageInfo usageData quotaInfo quotaData balance credits
capabilities supported supportedAgents native agentAccounts remainingQuota
usedQuota maxQuota current max maximum minimum value percentageUsed
percentageRemaining limitReached hasQuota hasUsage hasAccount accountStatus
organizations organization workspaces workspace project projects lastUpdated
claude codex copilot cursor freebuff grok hermes kilo kilocode opencode opencode2
""".split())
NUMERIC_FIELDS = set("""
quotaUsed quota_used usagePercent usedPercent usedPercentage utilization percent
percentage remainingPercent remainingPercentage remaining used consumed total limit
remainingRequests usedRequests requests requestLimit requestsUsed requestsRemaining
count weeklyLimit dailyLimit windowMinutes windowSeconds duration interval period
resetAfter resetIn balance credits remainingQuota usedQuota maxQuota current max
maximum minimum value percentageUsed percentageRemaining inputPrice outputPrice price
""".split())
ENUMS = set("""
claude codex copilot cursor freebuff grok hermes kilo kilocode opencode opencode2
github-copilot github_copilot free trial pro pro+ plus premium enterprise business
individual team teams personal student education hobby unlimited included
included_plan included_allowance free_models quota paid metered api_key_metered
ok ready connected disconnected error unknown active inactive available unavailable
authenticated unauthenticated logged_in logged_out exhausted blocked valid invalid
oauth oauth2 api_key api-key apikey subscription oauth-token token_based
local cloud native healthy unhealthy success succeeded failed warning enabled disabled
requests request percent percentage weekly daily monthly hourly tokens credits
""".split())


def _canonical(value: str) -> str:
    return value.replace("_", "").replace("-", "").lower()


SAFE_FIELDS = {_canonical(key) for key in FIELDS}
SAFE_NUMBERS = {_canonical(key) for key in NUMERIC_FIELDS}


def project(payload: Any) -> Any:
    """Retain schema and selected enums/numbers; omit arbitrary text and keys."""
    remaining_nodes = 1800

    def visit(value: Any, key: str = "", depth: int = 0) -> Any:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if depth > 10 or remaining_nodes < 0:
            return "<truncated>"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            omitted = 0
            for field, child in value.items():
                if not isinstance(field, str) or len(field) > 64 or _canonical(field) not in SAFE_FIELDS:
                    omitted += 1
                    continue
                if len(result) >= 60:
                    result["_truncated"] = True
                    break
                result[field] = visit(child, field, depth + 1)
            if omitted:
                result["_omitted_fields"] = omitted
            return result
        if isinstance(value, list):
            rows = [visit(item, key, depth + 1) for item in value[:12]]
            if len(value) > 12:
                rows.append({"_truncated_items": len(value) - 12})
            return rows
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if _canonical(key) in SAFE_NUMBERS and math.isfinite(value):
                return value
            return "<number>"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ENUMS:
                return normalized
            if _canonical(key) in ("version", "appversion", "orcaversion") and re.fullmatch(r"v?\d{1,5}\.\d{1,5}\.\d{1,5}", value):
                return value
            return "<text omitted>"
        return "<unsupported>"

    return visit(payload)


def collect(adapter: OrcaAdapter) -> dict[str, Any]:
    report: dict[str, Any] = {"diagnostic_version": 1, "read_only": True,
                              "note": "Field names, enums and quota numbers are allowlisted. Account identities and arbitrary text are omitted."}
    for label, operation in (("status", adapter.status), ("installed", adapter.agent_installed), ("accounts", adapter.account_list)):
        try:
            report[label] = project(operation())
        except OrcaError as exc:
            # Error strings/details can contain paths, command output or secrets.
            known_codes = {"timeout", "not_found", "os_error", "cli_error", "invalid_json", "native_error"}
            report[label] = {"error": exc.code if exc.code in known_codes else "cli_error"}
    return report


if __name__ == "__main__":
    print(json.dumps(collect(OrcaAdapter()), ensure_ascii=True, indent=2, allow_nan=False))
