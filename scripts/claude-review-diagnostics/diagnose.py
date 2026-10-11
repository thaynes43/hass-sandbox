#!/usr/bin/env python3
"""Classify private Claude SDK failures without publishing execution content."""

import json
import os
from pathlib import Path
import re


MAX_EXECUTION_BYTES = 16 * 1024 * 1024
# Only these fixed categories can reach logs. Execution text stays in memory.
SIGNATURES = (
    ("oauth_expired", (
        "oauth token has expired", "oauth token expired", "expired oauth token",
        "access token has expired", "access token expired",
    )),
    ("oauth_invalid", (
        "invalid oauth token", "invalid bearer token", "invalid access token",
        "oauth authentication failed", "oauth token is invalid", "invalid api key",
        "invalid_grant", "oauth token has been revoked", "oauth token revoked",
        "not logged in", "authentication_error",
    )),
    ("quota_or_rate_limit", (
        "rate_limit_error", "rate limit", "usage limit",
        "usage limit reached", "you've hit your limit", "you have hit your limit",
        "insufficient credits", "credit balance is too low", "out of usage credits",
        "weekly limit", "extra usage",
    )),
    ("model_unavailable", (
        "model_not_found", "unknown model", "invalid model name",
    )),
)


def classify(text):
    normalized = text.casefold().replace("\u2019", "'")
    for category, signatures in SIGNATURES:
        if any(signature in normalized for signature in signatures):
            return category
    if re.search(r"\bmodel\b[^\n]{0,160}\b(?:does not exist|doesn't exist|may not exist|not found|not available|unavailable)\b", normalized):
        return "model_unavailable"
    if "not_found_error" in normalized and re.search(r"\bmodel\s*:", normalized):
        return "model_unavailable"
    return "other"


def diagnose(path):
    report = {
        "cause": "other",
        "execution_status": "unreadable",
        "result_present": False,
        "reported_error": False,
        "success_subtype": False,
        "first_turn": False,
        "zero_cost": False,
        "empty_model_usage": False,
        "permission_denials_present": False,
        "error_text_present": False,
    }
    try:
        with path.open("rb") as execution:
            content = execution.read(MAX_EXECUTION_BYTES + 1)
    except (OSError, ValueError):
        return report
    if len(content) > MAX_EXECUTION_BYTES:
        report["execution_status"] = "too_large"
        return report
    try:
        messages = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        report["execution_status"] = "invalid_json"
        return report
    if not isinstance(messages, list):
        report["execution_status"] = "invalid_shape"
        return report
    report["execution_status"] = "parsed"
    result = next((message for message in reversed(messages)
                   if isinstance(message, dict) and message.get("type") == "result"), None)
    if result is None:
        return report
    report.update({
        "result_present": True,
        "reported_error": result.get("is_error") is True,
        "success_subtype": result.get("subtype") == "success",
        "first_turn": type(result.get("num_turns")) is int and result["num_turns"] == 1,
        "zero_cost": type(result.get("total_cost_usd")) in (int, float) and result["total_cost_usd"] == 0,
        "empty_model_usage": isinstance(result.get("modelUsage"), dict) and not result["modelUsage"],
        "permission_denials_present": isinstance(result.get("permission_denials"), list) and bool(result["permission_denials"]),
    })
    errors = result.get("errors", [])
    texts = [error for error in errors if isinstance(error, str)] if isinstance(errors, list) else []
    if isinstance(result.get("result"), str):
        texts.append(result["result"])
    report["error_text_present"] = any(texts)
    # Never classify ordinary successful review text as an authentication failure.
    subtype = result.get("subtype")
    if report["reported_error"] or (isinstance(subtype, str) and subtype.startswith("error")):
        report["cause"] = classify("\n".join(texts))
    return report


def main():
    # The action may throw before exposing its execution_file output. Its pinned
    # implementation writes this fixed RUNNER_TEMP filename before that throw.
    path = Path(os.environ.get("CLAUDE_EXECUTION_FILE") or
                str(Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "claude-execution-output.json"))
    output = json.dumps(diagnose(path), sort_keys=True)
    print(output)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as destination:
                destination.write("### Claude failure diagnostic\n\n```json\n" + output + "\n```\n")
        except OSError:
            # Do not expose paths or exception text from this private-file reader.
            print("Claude diagnostic summary could not be written.")


if __name__ == "__main__":
    main()
