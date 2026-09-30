#!/usr/bin/env python3
"""Exception monitor for property maintenance requests.

Reads a maintenance request CSV, applies the exception rules below, and
prints a ranked review queue. The monitor is read-only: it flags requests
for a person to review and never changes a priority, cost, or other field.
"""

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

# --- Rule configuration ----------------------------------------------------

PRIORITIES = ["Low", "Medium", "High", "Emergency"]
KNOWN_STATUSES = {"Open", "In Progress", "Pending Vendor", "On Hold", "Completed"}
RESOLVED_STATUSES = {"Completed"}

# Longest a request may stay unresolved, by priority.
RESOLUTION_LIMITS = {
    "Emergency": timedelta(hours=1),
    "High": timedelta(hours=24),
    "Medium": timedelta(hours=48),
    "Low": timedelta(days=7),
}

# Longest a resident may go without an update, by priority.
RESIDENT_UPDATE_LIMITS = {
    "Emergency": timedelta(hours=2),
    "High": timedelta(hours=24),
    "Medium": timedelta(hours=48),
    "Low": timedelta(days=7),
}

# How long an Emergency or High request may go without a confirmed vendor.
DISPATCH_GRACE = {
    "Emergency": timedelta(minutes=15),
    "High": timedelta(hours=4),
}

HIGH_COST_THRESHOLD = 1000

# A missing estimate is normal on a new request; flag it after this long.
COST_ESTIMATE_GRACE = timedelta(hours=24)

# A request this many times over its resolution limit escalates one level.
ESCALATION_MULTIPLIER = 4

# Values that look filled in but carry no information.
PLACEHOLDERS = {"", "tbd", "n/a", "na", "none", "unknown", "pending", "-", "?"}

# Keyword signals for the priority-mismatch check. Matches are suggestions
# for a reviewer, never grounds to change the priority automatically.
URGENT_ISSUE_PATTERNS = [
    r"\bgas\b.*\b(smell|leak|odou?r)\b",
    r"\b(smell|odou?r) of gas\b",
    r"carbon monoxide",
    r"\bco (alarm|detector)\b",
    r"\bfire\b",
    r"\bsmoke\b(?!\s+detector)",
    r"\bsparking\b",
    r"\bburst pipe\b",
    r"\bflooding\b",
    r"\boverflowing\b",
    r"\bsewage\b",
    r"\bno heat\b",
    r"\bexposed wir",
    r"\bno (water|power|electricity)\b",
]
ROUTINE_ISSUE_PATTERNS = [
    r"light ?bulb",
    r"\bpaint",
    r"\btouch-?up\b",
    r"\bsqueak",
    r"\bcabinet",
    r"\bdrawer",
    r"\bcosmetic",
    r"\bchirp",
    r"\bdrip",
]

SEVERITIES = ["low", "medium", "high", "critical"]
RESOLUTION_SEVERITY = {"Emergency": "critical", "High": "high", "Medium": "medium", "Low": "low"}
UPDATE_SEVERITY = {"Emergency": "high", "High": "medium", "Medium": "low", "Low": "low"}

TIME_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d"]


# --- Helpers ---------------------------------------------------------------

@dataclass
class Flag:
    rule: str
    severity: str
    detail: str


def clean(value):
    value = (value or "").strip()
    return "" if value.lower() in PLACEHOLDERS else value


def parse_time(value):
    """Return (datetime or None, is_invalid)."""
    value = clean(value)
    if not value:
        return None, False
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt), False
        except ValueError:
            pass
    return None, True


def parse_cost(value):
    """Return (float or None, is_invalid)."""
    value = clean(value).replace("$", "").replace(",", "")
    if not value:
        return None, False
    try:
        return float(value), False
    except ValueError:
        return None, True


def fmt_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def escalate(severity):
    return SEVERITIES[min(SEVERITIES.index(severity) + 1, len(SEVERITIES) - 1)]


def classify_issue(issue):
    text = issue.lower()
    if any(re.search(p, text) for p in URGENT_ISSUE_PATTERNS):
        return "urgent"
    if any(re.search(p, text) for p in ROUTINE_ISSUE_PATTERNS):
        return "routine"
    return None


# --- Rules -----------------------------------------------------------------

def evaluate(row, now):
    """Return the list of Flags raised for one request row."""
    status = clean(row.get("status"))
    if status in RESOLVED_STATUSES:
        return []

    flags = []
    issue = clean(row.get("issue"))
    priority = clean(row.get("priority"))
    vendor = clean(row.get("vendor_assigned"))
    vendor_confirmed = clean(row.get("vendor_confirmed")).lower() == "yes"
    cost_approved = clean(row.get("cost_approved")).lower() == "yes"
    reported_by = clean(row.get("reported_by")).lower()
    hold_reason = clean(row.get("hold_reason"))
    created, created_invalid = parse_time(row.get("created_at"))
    last_update, _ = parse_time(row.get("last_resident_update"))
    scheduled, _ = parse_time(row.get("scheduled_date"))
    hold_until, _ = parse_time(row.get("hold_until"))
    cost, cost_invalid = parse_cost(row.get("estimated_cost"))

    # Missing information. Blocking fields switch other rules off, so they
    # are reported at high severity along with what can't be checked.
    blocking = []
    if not clean(row.get("property")):
        blocking.append("property")
    if not issue:
        blocking.append("issue")
    if not priority:
        blocking.append("priority")
    elif priority not in PRIORITIES:
        blocking.append(f"priority (invalid: {priority!r})")
        priority = ""
    if created_invalid:
        blocking.append(f"created_at (invalid: {row.get('created_at')!r})")
    elif not created:
        blocking.append("created_at")
    if not status:
        blocking.append("status")
    elif status not in KNOWN_STATUSES:
        blocking.append(f"status (invalid: {status!r})")
    if blocking:
        skipped = []
        if not priority or not created:
            skipped.append("time limits")
        if not priority:
            skipped.append("dispatch and resident-update checks")
        note = f" Can't check {' or '.join(skipped)} until fixed." if skipped else ""
        flags.append(Flag("MISSING_INFO", "high", f"Missing or invalid: {', '.join(blocking)}.{note}"))
    if not clean(row.get("unit")):
        flags.append(Flag("MISSING_INFO", "medium", "No unit recorded (use 'Common Area' for shared spaces)."))
    if cost_invalid:
        flags.append(Flag("MISSING_INFO", "medium", f"Invalid estimated_cost: {row.get('estimated_cost')!r}."))

    age = now - created if created else None

    # Holds pause the clock only with a reason and a future end date, and
    # never for emergencies.
    paused = False
    if status == "On Hold":
        if not hold_reason or not hold_until:
            flags.append(Flag("INVALID_HOLD", "medium",
                              "On Hold without a reason and end date; time limits still apply."))
        elif hold_until <= now:
            flags.append(Flag("HOLD_EXPIRED", "medium",
                              f"Hold ended {hold_until:%Y-%m-%d}; time limits apply again."))
        elif priority == "Emergency":
            flags.append(Flag("INVALID_HOLD", "high", "Emergency requests can't be put on hold."))
        else:
            paused = True

    # Emergency and High requests need a vendor who has confirmed.
    grace = DISPATCH_GRACE.get(priority)
    if grace and age is not None and age > grace and not paused and not (vendor and vendor_confirmed):
        severity = "critical" if priority == "Emergency" else "high"
        if vendor:
            detail = f"{vendor} assigned but has not confirmed; {priority} request open {fmt_duration(age)}."
        else:
            detail = f"No vendor assigned; {priority} request open {fmt_duration(age)}."
        flags.append(Flag("NO_CONFIRMED_DISPATCH", severity, detail))

    # Resolution time limit by priority. A future appointment pauses the
    # clock for Medium and Low requests only.
    scheduled_ahead = scheduled is not None and scheduled > now and priority in ("Medium", "Low")
    if priority and age is not None and not paused and not scheduled_ahead:
        limit = RESOLUTION_LIMITS[priority]
        if age > limit:
            severity = RESOLUTION_SEVERITY[priority]
            if age > limit * ESCALATION_MULTIPLIER:
                severity = escalate(severity)
            detail = f"{priority} request unresolved for {fmt_duration(age)} (limit {fmt_duration(limit)})."
            if scheduled is not None and scheduled <= now:
                detail += f" Scheduled visit on {scheduled:%Y-%m-%d %H:%M} has passed."
            flags.append(Flag("OVER_TIME_LIMIT", severity, detail))

    # Priority mismatch: flagged for review, never changed.
    kind = classify_issue(issue) if issue else None
    if kind == "urgent" and PRIORITIES.index(priority or "Low") <= PRIORITIES.index("Medium"):
        label = priority or "missing"
        flags.append(Flag("PRIORITY_MISMATCH", "critical",
                          f"Issue suggests an urgent safety problem but priority is {label}. "
                          "Confirm the priority; it has not been changed."))
    elif kind == "routine" and priority in ("High", "Emergency"):
        flags.append(Flag("PRIORITY_MISMATCH", "medium",
                          f"Issue suggests routine work but priority is {priority}. "
                          "Confirm the priority; it has not been changed."))

    # Cost review. Never a reason to hold up emergency work.
    if cost is not None and cost > HIGH_COST_THRESHOLD and not cost_approved:
        detail = f"Estimate ${cost:,.0f} exceeds ${HIGH_COST_THRESHOLD:,} and is not approved. Needs human review."
        if priority == "Emergency":
            detail += " Do not delay emergency work for approval."
        flags.append(Flag("HIGH_COST_REVIEW", "medium", detail))
    elif cost is None and not cost_invalid and age is not None and age > COST_ESTIMATE_GRACE:
        severity = "medium" if priority in ("High", "Emergency") else "low"
        flags.append(Flag("MISSING_INFO", severity,
                          f"No cost estimate after {fmt_duration(age)}; approval threshold can't be checked."))

    # Resident updates. Staff-reported requests have no resident to update;
    # a blank reporter is treated as a resident to stay on the safe side.
    if reported_by != "staff" and priority and created:
        limit = RESIDENT_UPDATE_LIMITS[priority]
        since = now - max(created, last_update) if last_update else age
        if since > limit:
            if last_update:
                detail = f"Resident last updated {fmt_duration(since)} ago (limit {fmt_duration(limit)})."
            else:
                detail = f"Resident never updated, {fmt_duration(since)} since request (limit {fmt_duration(limit)})."
            flags.append(Flag("RESIDENT_UPDATE_OVERDUE", UPDATE_SEVERITY[priority], detail))

    return flags


def top_severity(flags):
    return max(SEVERITIES.index(f.severity) for f in flags)


def ranking_priority(row, flags):
    """Priority used only to order the queue: an issue flagged as urgent but
    labeled lower ranks with emergencies. The row itself is not changed."""
    if any(f.rule == "PRIORITY_MISMATCH" and f.severity == "critical" for f in flags):
        return PRIORITIES.index("Emergency")
    priority = clean(row.get("priority"))
    return PRIORITIES.index(priority) if priority in PRIORITIES else -1


def build_queue(rows, now):
    """Return [(row, flags)] for flagged requests, most urgent first."""
    queue = []
    for row in rows:
        flags = evaluate(row, now)
        if flags:
            flags.sort(key=lambda f: SEVERITIES.index(f.severity), reverse=True)
            queue.append((row, flags))
    oldest = datetime.max

    def sort_key(item):
        row, flags = item
        created, _ = parse_time(row.get("created_at"))
        critical = sum(f.severity == "critical" for f in flags)
        return (-top_severity(flags), -ranking_priority(row, flags), -critical, created or oldest)

    queue.sort(key=sort_key)
    return queue


# --- Output ----------------------------------------------------------------

def print_report(rows, queue, now, out=sys.stdout):
    counts = {s: 0 for s in SEVERITIES}
    for _, flags in queue:
        counts[SEVERITIES[top_severity(flags)]] += 1
    print(f"Maintenance exception report - as of {now:%Y-%m-%d %H:%M}", file=out)
    print(f"{len(rows)} requests checked, {len(queue)} need attention: "
          + ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES)), file=out)
    for row, flags in queue:
        where = " ".join(v for v in (row.get("property"), row.get("unit")) if clean(v))
        header = " | ".join([row["request_id"], where or "(no location)",
                             clean(row.get("priority")) or "(no priority)",
                             clean(row.get("status")) or "(no status)"])
        print(f"\n[{SEVERITIES[top_severity(flags)].upper()}] {header}", file=out)
        print(f"  {clean(row.get('issue')) or '(no issue description)'}", file=out)
        for f in flags:
            print(f"  - {f.severity:<8} {f.rule:<24} {f.detail}", file=out)


def write_flags_csv(queue, path):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["request_id", "property", "unit", "priority", "status", "rule", "severity", "detail"])
        for row, flags in queue:
            for f in flags:
                writer.writerow([row["request_id"], row.get("property", ""), row.get("unit", ""),
                                 row.get("priority", ""), row.get("status", ""), f.rule, f.severity, f.detail])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_path", nargs="?", default="maintenance_requests.csv")
    parser.add_argument("--as-of", help="evaluate as of this time (YYYY-MM-DD HH:MM); default is now")
    parser.add_argument("--output", help="also write one row per flag to this CSV file")
    args = parser.parse_args(argv)

    now = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M") if args.as_of else datetime.now()
    with open(args.csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    queue = build_queue(rows, now)
    print_report(rows, queue, now)
    if args.output:
        write_flags_csv(queue, args.output)


if __name__ == "__main__":
    main()
