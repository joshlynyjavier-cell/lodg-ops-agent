#!/usr/bin/env python3
"""Today's Exceptions: a one-line-per-request view for operations staff.

Presentation only. It runs the validated V1 exception monitor unchanged
(same rules, severities and ranking) and shows each unresolved request that
needs attention on one line, with its single most useful reason. The full
rule triggers and recommended actions stay available through --details or
--request, which print the standard V1 report.
"""

import argparse
import csv
import re
import sys
import tomllib
from datetime import datetime

from exception_monitor import (DEFAULT_CONFIG_PATH, SEVERITIES, ConfigError, build_financial_review, build_queue,
                               clean, load_config, parse_time, print_report)
from issue_categories import CategoryError, load_categories

SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}

# Which V1 flag to headline when a request has several at its top severity:
# the one an operator should act on first. This only picks among flags the
# monitor already raised; it never adds or removes one.
HEADLINE_ORDER = [
    "PRIORITY_MISMATCH", "NEEDS_HUMAN_REVIEW", "NO_CONFIRMED_DISPATCH", "NO_RESPONSE",
    "STALLED_PROGRESS", "OVER_TIME_LIMIT", "INVALID_HOLD", "HOLD_EXPIRED", "MISSING_INFO",
    "RESIDENT_UPDATE_OVERDUE", "HIGH_COST_REVIEW", "LONG_RUNNING_EMERGENCY",
]

SYNTHETIC_NOTICE = "SYNTHETIC DATA · thresholds are prototype assumptions, not Lodg policy"


def headline_flag(flags):
    """The single most useful flag: highest severity, then the action order above."""
    top = max(SEVERITIES.index(f.severity) for f in flags)
    candidates = [f for f in flags if SEVERITIES.index(f.severity) == top]
    return min(candidates, key=lambda f: HEADLINE_ORDER.index(f.rule) if f.rule in HEADLINE_ORDER else 99)


def short_age(delta):
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} min"
    if hours < 48:
        return f"{int(hours)} hr" + ("" if int(hours) == 1 else "s")
    return f"{int(hours // 24)} days"


def short_location(row):
    prop = clean(row.get("property"))
    unit = clean(row.get("unit"))
    if unit:
        unit = f"Unit {unit}" if any(ch.isdigit() for ch in unit) else unit
    else:
        unit = "no unit"
    return f"{prop} · {unit}" if prop else f"(no property) · {unit}"


def short_issue(row, limit=40):
    issue = clean(row.get("issue"))
    if not issue:
        return "(no description)"
    issue = issue.split(" - ")[0].strip()
    return issue if len(issue) <= limit else issue[: limit - 1].rstrip() + "…"


def headline_text(flag, row, now):
    """Short wording for a V1 flag, using only what the flag and row already say."""
    vendor = clean(row.get("vendor_assigned"))
    if flag.rule == "PRIORITY_MISMATCH":
        if flag.severity == "critical":
            return f"Possible safety issue labeled {clean(row.get('priority')) or 'without priority'} — review priority now"
        return "Priority looks too high for this issue — review"
    if flag.rule == "NEEDS_HUMAN_REVIEW":
        if flag.detail.startswith("Possible hazard"):
            return "Possible hazard — supervisor review now"
        if "Mold" in flag.detail:
            return "Mold — supervisor assessment needed"
        return "Needs a supervisor to categorize"
    if flag.rule == "NO_CONFIRMED_DISPATCH":
        return f"{vendor} hasn't confirmed" if vendor else "No vendor assigned"
    if flag.rule == "NO_RESPONSE":
        return f"No response from {vendor}" if vendor else "No response, no vendor assigned"
    if flag.rule == "STALLED_PROGRESS":
        last_progress, _ = parse_time(row.get("last_progress_at"))
        return f"Work stalled — last progress {short_age(now - last_progress)} ago" if last_progress else "Work stalled"
    if flag.rule == "OVER_TIME_LIMIT":
        if "Scheduled visit" in flag.detail:
            return "Overdue — scheduled visit missed"
        return "Overdue" + ("" if vendor else ", no vendor assigned")
    if flag.rule == "INVALID_HOLD":
        return "On hold without a reason or end date"
    if flag.rule == "HOLD_EXPIRED":
        return "Hold expired"
    if flag.rule == "MISSING_INFO":
        match = re.match(r"Missing or invalid: (.+?)\.", flag.detail)
        if match:
            fields = re.sub(r" \(invalid: [^)]*\)", "", match.group(1)).replace("created_at", "request date")
            return f"Missing: {fields}"
        if flag.detail.startswith("No unit"):
            return "Missing: unit"
        return "Missing or invalid details"
    if flag.rule == "RESIDENT_UPDATE_OVERDUE":
        if "never updated" in flag.detail:
            return "Resident never updated"
        last_update, _ = parse_time(row.get("last_resident_update"))
        return f"Resident awaiting update — last one {short_age(now - last_update)} ago" if last_update \
            else "Resident awaiting update"
    if flag.rule == "HIGH_COST_REVIEW":
        match = re.search(r"\$[\d,]+", flag.detail)
        return f"{match.group(0) if match else 'High-cost'} estimate — approval required"
    if flag.rule == "LONG_RUNNING_EMERGENCY":
        return "Still open despite active work — review timeline"
    return flag.detail


def exception_line(row, flags, now):
    flag = headline_flag(flags)
    created, _ = parse_time(row.get("created_at"))
    age = f"{short_age(now - created)} open" if created else "open time unknown"
    more = f"  (+{len(flags) - 1} more)" if len(flags) > 1 else ""
    return (f"{SEVERITY_ICON[flag.severity]} {row['request_id']} · {short_location(row)} — {short_issue(row)} — "
            f"{age} — {headline_text(flag, row, now)}{more}")


def render_today(queue, now):
    counts = {s: 0 for s in SEVERITIES}
    for _, flags in queue:
        counts[max(flags, key=lambda f: SEVERITIES.index(f.severity)).severity] += 1
    lines = [
        f"TODAY'S EXCEPTIONS · {now:%a %d %b %Y, %H:%M}",
        SYNTHETIC_NOTICE,
        "",
        f"{len(queue)} requests need attention   🔴 {counts['critical']} Critical   "
        f"🟠 {counts['high']} High   🟡 {counts['medium']} Medium"
        + (f"   ⚪ {counts['low']} Low" if counts["low"] else ""),
        "",
    ]
    lines += [exception_line(row, flags, now) for row, flags in queue] or ["No unresolved requests need attention."]
    lines += ["", "Details: --request <ID> for one request, or --details for every rule trigger and next action."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Today's Exceptions: concise view of the V1 exception monitor.")
    parser.add_argument("csv_path", nargs="?", default="maintenance_requests.csv")
    parser.add_argument("--as-of", help="evaluate as of this time (YYYY-MM-DD HH:MM); default is now")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--categories", default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--details", action="store_true", help="show the full V1 report for every exception")
    group.add_argument("--request", metavar="ID", help="show the full V1 report for one request")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        categories = load_categories(args.categories) if args.categories else load_categories()
    except (OSError, tomllib.TOMLDecodeError, ConfigError, CategoryError) as exc:
        sys.exit(f"Config error: {exc}")
    now = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M") if args.as_of else datetime.now()
    with open(args.csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    queue = build_queue(rows, now, config, categories)

    if args.request:
        selected = [item for item in queue if item[0]["request_id"] == args.request]
        if not selected:
            sys.exit(f"{args.request} is not in today's exceptions.")
        print(SYNTHETIC_NOTICE)
        print_report([selected[0][0]], selected, now, categories)
    elif args.details:
        print(SYNTHETIC_NOTICE)
        print_report(rows, queue, now, categories, build_financial_review(rows, config))
    else:
        print(render_today(queue, now))


if __name__ == "__main__":
    main()
