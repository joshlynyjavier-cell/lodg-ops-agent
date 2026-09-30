#!/usr/bin/env python3
"""Exception monitor for property maintenance requests.

Reads a maintenance request CSV, applies the exception rules below, and
prints a ranked review queue. The monitor is read-only: it flags requests
for a person to review and never changes a priority, cost, or other field.
Thresholds live in monitor_config.toml and issue categories (with their
escalation procedures and standard actions) in issue_categories.toml.

The monitor makes no external calls: it never contacts emergency services,
vendors or residents. It only surfaces and prioritizes issues for a person.
"""

import argparse
import csv
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from issue_categories import CategoryError, classify, load_categories

DEFAULT_CONFIG_PATH = Path(__file__).with_name("monitor_config.toml")

PRIORITIES = ["Low", "Medium", "High", "Emergency"]
KNOWN_STATUSES = {"Open", "In Progress", "Pending Vendor", "On Hold", "Completed"}

# Values that look filled in but carry no information.
PLACEHOLDERS = {"", "tbd", "n/a", "na", "none", "unknown", "pending", "-", "?"}

SEVERITIES = ["low", "medium", "high", "critical"]
RESOLUTION_SEVERITY = {"High": "high", "Medium": "medium", "Low": "low"}
UPDATE_SEVERITY = {"Emergency": "high", "High": "medium", "Medium": "low", "Low": "low"}

TIME_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d"]


# --- Configuration ---------------------------------------------------------

@dataclass
class Config:
    resolved_statuses: set
    dispatch_grace: dict
    emergency_response: timedelta
    emergency_stalled_after: timedelta
    emergency_long_running_review: timedelta
    resolution_limits: dict
    resident_update_limits: dict
    high_cost_threshold: float
    missing_estimate_grace: timedelta
    resolution_multiplier: float


class ConfigError(ValueError):
    pass


def load_config(path=DEFAULT_CONFIG_PATH):
    """Load and validate thresholds from a TOML file."""
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    def section(name, keys):
        values = raw.get(name)
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: missing section [{name}]")
        unknown = set(values) - set(keys)
        if unknown:
            raise ConfigError(f"{path}: unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
        for key in keys:
            if key not in values:
                raise ConfigError(f"{path}: missing setting '{key}' in [{name}]")
        return values

    def number(name, key, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"{path}: [{name}] {key} must be a positive number, got {value!r}")
        return value

    def hours(name, keys):
        values = section(name, keys)
        return {k: timedelta(hours=number(name, k, values[k])) for k in keys}

    resolved = section("statuses", ["resolved"])["resolved"]
    if not resolved or not all(isinstance(s, str) and s.strip() for s in resolved):
        raise ConfigError(f"{path}: [statuses] resolved must be a non-empty list of status names")
    emergency = hours("emergency_hours", ["response", "stalled_after", "long_running_review"])
    cost = section("cost", ["high_cost_threshold", "missing_estimate_grace_hours"])
    escalation = section("escalation", ["resolution_multiplier"])

    return Config(
        resolved_statuses={s.strip() for s in resolved},
        dispatch_grace=hours("dispatch_grace_hours", ["Emergency", "High"]),
        emergency_response=emergency["response"],
        emergency_stalled_after=emergency["stalled_after"],
        emergency_long_running_review=emergency["long_running_review"],
        resolution_limits=hours("resolution_limit_hours", ["High", "Medium", "Low"]),
        resident_update_limits=hours("resident_update_limit_hours", PRIORITIES),
        high_cost_threshold=number("cost", "high_cost_threshold", cost["high_cost_threshold"]),
        missing_estimate_grace=timedelta(hours=number("cost", "missing_estimate_grace_hours",
                                                      cost["missing_estimate_grace_hours"])),
        resolution_multiplier=number("escalation", "resolution_multiplier",
                                     escalation["resolution_multiplier"]),
    )


# --- Helpers ---------------------------------------------------------------

@dataclass
class Flag:
    rule: str
    severity: str
    detail: str  # why the request was flagged
    action: str  # recommended next step for a person; the monitor takes no action itself


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


# --- Rules -----------------------------------------------------------------

def procedure_first(category_match):
    """Safety-critical issues start from the predefined escalation procedure."""
    return "Follow the escalation procedure above. " if category_match.is_safety_critical else ""


def dispatch_step(category_match):
    """How to get someone assigned when no vendor is recorded."""
    if category_match.vendor:
        return f"Assign {category_match.vendor} and get confirmation of dispatch."
    if category_match.hazard_indicators:
        return "Have a supervisor identify the possible hazard now and dispatch the right vendor."
    return "Assign an appropriate vendor once a supervisor has reviewed the issue."


def emergency_flags(age, last_progress, now, config, category_match, vendor):
    """Stage checks for an unresolved Emergency: response, then active work,
    then long-running review. Dispatch is checked separately. Actions only
    mention contacting a vendor when one is actually assigned."""
    first = procedure_first(category_match)
    if last_progress is None:
        if age > config.emergency_response:
            if vendor:
                action = (f"Call {vendor} for an arrival time; escalate to the property manager "
                          "if no one is on the way.")
            else:
                action = ("No vendor is assigned, so no one is on the way. Escalate to the property "
                          "manager now. " + dispatch_step(category_match))
            return [Flag("NO_RESPONSE", "critical",
                         f"No progress recorded {fmt_duration(age)} after the request "
                         f"(limit {fmt_duration(config.emergency_response)}).", first + action)]
        return []
    idle = now - last_progress
    if idle > config.emergency_stalled_after:
        if vendor:
            action = f"Contact {vendor} for a status and next step; escalate to the property manager if work has stopped."
        else:
            action = ("No vendor is assigned. Escalate to the property manager and find out who did the "
                      "earlier work. " + dispatch_step(category_match))
        return [Flag("STALLED_PROGRESS", "high",
                     f"Last progress {fmt_duration(idle)} ago (limit {fmt_duration(config.emergency_stalled_after)}); "
                     f"emergency unresolved for {fmt_duration(age)}.", first + action)]
    if age > config.emergency_long_running_review:
        if vendor:
            action = (f"Review the repair timeline with {vendor} and decide whether the resident "
                      "needs temporary arrangements.")
        else:
            action = ("No vendor is recorded although work is in progress. Confirm who is doing the work, "
                      "then review the timeline and whether the resident needs temporary arrangements.")
        return [Flag("LONG_RUNNING_EMERGENCY", "medium",
                     f"Work is active (last progress {fmt_duration(idle)} ago) but the emergency is "
                     f"unresolved after {fmt_duration(age)} (review after "
                     f"{fmt_duration(config.emergency_long_running_review)}).", action)]
    return []


def evaluate(row, now, config, categories):
    """Return the list of Flags raised for one request row."""
    status = clean(row.get("status"))
    if status in config.resolved_statuses:
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
    last_progress, progress_invalid = parse_time(row.get("last_progress_at"))
    scheduled, _ = parse_time(row.get("scheduled_date"))
    hold_until, _ = parse_time(row.get("hold_until"))
    cost, cost_invalid = parse_cost(row.get("estimated_cost"))
    category_match = classify(issue, categories)

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
    elif status not in KNOWN_STATUSES | config.resolved_statuses:
        blocking.append(f"status (invalid: {status!r})")
    if blocking:
        skipped = []
        if not priority or not created:
            skipped.append("time limits")
        if not priority:
            skipped.append("dispatch and resident-update checks")
        note = f" Can't check {' or '.join(skipped)} until fixed." if skipped else ""
        flags.append(Flag("MISSING_INFO", "high", f"Missing or invalid: {', '.join(blocking)}.{note}",
                          "Contact the requester or check the original request to fill in the missing "
                          "details, then triage it."))
    if not clean(row.get("unit")):
        flags.append(Flag("MISSING_INFO", "medium", "No unit recorded.",
                          "Add the unit number, or 'Common Area' for shared spaces."))
    if cost_invalid:
        flags.append(Flag("MISSING_INFO", "medium", f"Invalid estimated_cost: {row.get('estimated_cost')!r}.",
                          "Correct the cost estimate so the approval check can run."))
    if progress_invalid:
        flags.append(Flag("MISSING_INFO", "medium", f"Invalid last_progress_at: {row.get('last_progress_at')!r}.",
                          "Correct the progress timestamp (YYYY-MM-DD HH:MM)."))

    age = now - created if created else None

    # Holds pause the clock only with a reason and a future end date, and
    # never for emergencies.
    paused = False
    if status == "On Hold":
        if not hold_reason or not hold_until:
            flags.append(Flag("INVALID_HOLD", "medium",
                              "On Hold without a reason and end date; time limits still apply.",
                              "Record why the request is on hold and when it ends, or take it off hold."))
        elif hold_until <= now:
            flags.append(Flag("HOLD_EXPIRED", "medium",
                              f"Hold ended {hold_until:%Y-%m-%d}; time limits apply again.",
                              "Resume the work, or record a new hold reason and end date."))
        elif priority == "Emergency":
            flags.append(Flag("INVALID_HOLD", "high", "Emergency requests can't be put on hold.",
                              "Take the emergency off hold and confirm a vendor is responding."))
        else:
            paused = True

    # Emergency and High requests need a vendor who has confirmed.
    grace = config.dispatch_grace.get(priority)
    if grace and age is not None and age > grace and not paused and not (vendor and vendor_confirmed):
        severity = "critical" if priority == "Emergency" else "high"
        if vendor:
            detail = f"{vendor} assigned but has not confirmed; {priority} request open {fmt_duration(age)}."
            action = f"Call {vendor} to confirm dispatch; line up a backup vendor if they can't respond."
        else:
            detail = f"No vendor assigned; {priority} request open {fmt_duration(age)}."
            action = dispatch_step(category_match)
        flags.append(Flag("NO_CONFIRMED_DISPATCH", severity, detail, procedure_first(category_match) + action))

    # Emergencies are checked by stage (response, active work, long-running);
    # an emergency with a confirmed vendor actively working is not treated
    # like an untouched one.
    if priority == "Emergency" and age is not None:
        flags.extend(emergency_flags(age, last_progress, now, config, category_match, vendor))

    # Resolution time limit for other priorities. A future appointment pauses
    # the clock for Medium and Low requests only.
    scheduled_ahead = scheduled is not None and scheduled > now and priority in ("Medium", "Low")
    if priority in config.resolution_limits and age is not None and not paused and not scheduled_ahead:
        limit = config.resolution_limits[priority]
        if age > limit:
            severity = RESOLUTION_SEVERITY[priority]
            if age > limit * config.resolution_multiplier:
                severity = escalate(severity)
            detail = f"{priority} request unresolved for {fmt_duration(age)} (limit {fmt_duration(limit)})."
            if vendor:
                action = f"Get a status from {vendor} and set a completion date; escalate if it is stuck."
            else:
                action = (f"No vendor has been assigned in {fmt_duration(age)}. Escalate to the property manager. "
                          + dispatch_step(category_match))
            if scheduled is not None and scheduled <= now:
                detail += f" Scheduled visit on {scheduled:%Y-%m-%d %H:%M} has passed."
                action = "Confirm whether the scheduled visit happened, and reschedule if it didn't."
            flags.append(Flag("OVER_TIME_LIMIT", severity, detail, procedure_first(category_match) + action))

    # Priority mismatch, from the issue category's expected priority range.
    # Flagged for review, never changed: the assigned priority still drives
    # every other rule, so an Emergency label is always respected.
    if issue:
        minimums = [c.min_priority for c in category_match.categories if c.min_priority]
        maximums = [c.max_priority for c in category_match.categories if c.max_priority]
        rank = PRIORITIES.index(priority) if priority else -1
        if minimums and rank < max(PRIORITIES.index(m) for m in minimums):
            expected = max(minimums, key=PRIORITIES.index)
            flags.append(Flag("PRIORITY_MISMATCH", "critical",
                              f"Issue looks like {category_match.categories[0].label} (safety-critical, expected at "
                              f"least {expected}) but priority is {priority or 'missing'}.",
                              "Have a supervisor review the priority now. The monitor has not changed it."))
        elif maximums and priority and rank > min(PRIORITIES.index(m) for m in maximums):
            expected = min(maximums, key=PRIORITIES.index)
            flags.append(Flag("PRIORITY_MISMATCH", "medium",
                              f"Issue looks like {category_match.categories[0].label} (routine, usually at most "
                              f"{expected}) but priority is {priority}.",
                              f"Have a supervisor review the priority. Until then it is handled as {priority}."))

    # Issues that can't be confidently categorized go to a person rather than
    # getting a guessed category. A blank issue is already flagged above.
    if issue and category_match.hazard_indicators:
        # A possible hazard with no confirmed safety category. Critical when the
        # assigned priority is below High, since it may be an under-prioritized
        # danger; the assigned priority is never lowered.
        below_high = not priority or PRIORITIES.index(priority) < PRIORITIES.index("High")
        flags.append(Flag("NEEDS_HUMAN_REVIEW", "critical" if below_high else "high", category_match.review_reason,
                          "Have a supervisor review this now: decide whether it is a safety hazard, which "
                          "escalation procedure applies and what the priority should be. The monitor has not "
                          "guessed a category or changed the priority."))
    elif issue and category_match.review_reason:
        severity = "high" if len(category_match.categories) > 1 else "medium"
        guidance = next((c.review_guidance for c in category_match.categories if c.review_guidance), "")
        if category_match.is_safety_critical:
            guidance = "Have a supervisor confirm which escalation procedure applies; all matching procedures are shown above."
        flags.append(Flag("NEEDS_HUMAN_REVIEW", severity, category_match.review_reason,
                          guidance or "Have a supervisor choose a category and how to handle it. "
                                      "The monitor has not guessed."))

    # Cost review. Never a reason to hold up emergency work.
    if cost is not None and cost > config.high_cost_threshold and not cost_approved:
        detail = f"Estimate ${cost:,.0f} exceeds ${config.high_cost_threshold:,.0f} and is not approved."
        action = "Send the estimate to the approver for review."
        if priority == "Emergency":
            action += " Do not hold up emergency work while it is reviewed."
        flags.append(Flag("HIGH_COST_REVIEW", "medium", detail, action))
    elif cost is None and not cost_invalid and age is not None and age > config.missing_estimate_grace:
        severity = "medium" if priority in ("High", "Emergency") else "low"
        flags.append(Flag("MISSING_INFO", severity,
                          f"No cost estimate after {fmt_duration(age)}; approval threshold can't be checked.",
                          "Ask the vendor or maintenance team for a cost estimate."))

    # Resident updates. Staff-reported requests have no resident to update;
    # a blank reporter is treated as a resident to stay on the safe side.
    if reported_by != "staff" and priority and created:
        limit = config.resident_update_limits[priority]
        since = now - max(created, last_update) if last_update else age
        if since > limit:
            if last_update:
                detail = f"Resident last updated {fmt_duration(since)} ago (limit {fmt_duration(limit)})."
            else:
                detail = f"Resident never updated, {fmt_duration(since)} since request (limit {fmt_duration(limit)})."
            flags.append(Flag("RESIDENT_UPDATE_OVERDUE", UPDATE_SEVERITY[priority], detail,
                              "Send the resident an update with the current status, next step and "
                              "expected timing."))

    return flags


def financial_review(row, config):
    """Completed work over the cost threshold without recorded approval.
    Financial oversight only: not part of the maintenance escalation queue."""
    if clean(row.get("status")) not in config.resolved_statuses:
        return None
    cost, _ = parse_cost(row.get("estimated_cost"))
    approval = clean(row.get("cost_approved"))
    if cost is None or cost <= config.high_cost_threshold or approval.lower() == "yes":
        return None
    recorded = f"approval recorded as {approval!r}" if approval else "no approval recorded"
    return Flag("FINANCIAL_REVIEW", "n/a",
                f"Completed work cost ${cost:,.0f} (over ${config.high_cost_threshold:,.0f}) with {recorded}.",
                "Send to finance or the property manager for after-the-fact approval review. "
                "No maintenance action is needed.")


def build_financial_review(rows, config):
    """Return [(row, flag)] for completed work that needs financial review."""
    return [(row, flag) for row in rows if (flag := financial_review(row, config))]


def top_severity(flags):
    return max(SEVERITIES.index(f.severity) for f in flags)


def ranking_priority(row, flags):
    """Priority used only to order the queue: a possible safety issue labeled
    lower ranks with emergencies. The row itself is not changed."""
    if any(f.rule in ("PRIORITY_MISMATCH", "NEEDS_HUMAN_REVIEW") and f.severity == "critical" for f in flags):
        return PRIORITIES.index("Emergency")
    priority = clean(row.get("priority"))
    return PRIORITIES.index(priority) if priority in PRIORITIES else -1


def build_queue(rows, now, config, categories):
    """Return [(row, flags)] for flagged requests, most urgent first."""
    queue = []
    for row in rows:
        flags = evaluate(row, now, config, categories)
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

PROTOTYPE_NOTICE = ("PROTOTYPE: escalation procedures are examples only and must be replaced with "
                    "company-approved procedures, contacts and jurisdiction-specific requirements "
                    "before real-world use.")


def category_lines(category_match, priority):
    """Report lines describing how the issue's category says to handle it.
    A routine standard action is withheld when the assigned priority is above
    the category's usual maximum: the assigned priority is respected until a
    person changes it, and the mismatch is flagged for review instead."""
    lines = []
    if category_match.hazard_indicators:
        lines.append("  Category: not confirmed - possible hazard ("
                     + ", ".join(f"'{h}'" for h in category_match.hazard_indicators)
                     + "), needs human review now")
    elif not category_match.categories:
        lines.append("  Category: not recognized - needs human review")
    for c in category_match.categories:
        tag = {"safety_critical": "safety-critical", "human_review": "needs human review"}.get(c.handling, "routine")
        lines.append(f"  Category: {c.label} ({tag})")
        if c.procedure:
            lines.append("  Escalation procedure (PROTOTYPE EXAMPLE - not approved for real-world use):")
            lines.extend(f"    - {step}" for step in c.procedure)
        if c.standard_action:
            if c.max_priority and priority in PRIORITIES and \
                    PRIORITIES.index(priority) > PRIORITIES.index(c.max_priority):
                lines.append(f"  Standard action: not shown - the routine {c.label} action conflicts with the "
                             f"assigned {priority} priority. See the priority-mismatch flag.")
            else:
                lines.append(f"  Standard action: {c.standard_action}")
    if category_match.also_matched:
        lines.append("  Also mentions: " + ", ".join(c.label for c in category_match.also_matched))
    return lines


def category_label(category_match):
    return ", ".join(c.label for c in category_match.categories) or "Uncategorized"


def print_report(rows, queue, now, categories, financial=None, out=sys.stdout):
    counts = {s: 0 for s in SEVERITIES}
    for _, flags in queue:
        counts[SEVERITIES[top_severity(flags)]] += 1
    print(f"Maintenance exception report - as of {now:%Y-%m-%d %H:%M}", file=out)
    print(f"{len(rows)} request{'' if len(rows) == 1 else 's'} checked, {len(queue)} need"
          f"{'s' if len(queue) == 1 else ''} attention: "
          + ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES)), file=out)
    print("Nothing below has been changed or acted on. Each item needs a person to review and decide.", file=out)
    print(PROTOTYPE_NOTICE, file=out)
    for row, flags in queue:
        where = ", ".join(v for v in (row.get("property"), row.get("unit")) if clean(v))
        print(f"\n[{SEVERITIES[top_severity(flags)].upper()}] {row['request_id']}", file=out)
        print(f"  Property: {where or '(missing)'}", file=out)
        print(f"  Issue:    {clean(row.get('issue')) or '(missing)'}", file=out)
        print(f"  Priority: {clean(row.get('priority')) or '(missing)'}", file=out)
        for line in category_lines(classify(clean(row.get("issue")), categories), clean(row.get("priority"))):
            print(line, file=out)
        for i, f in enumerate(flags, 1):
            print(f"  {i}. Reason ({f.severity}): {f.detail}", file=out)
            print(f"     Next action: {f.action}", file=out)

    if financial is None:
        return
    print(f"\n=== Financial review: completed work ({len(financial)}) ===", file=out)
    print("Financial oversight only. These are not active maintenance issues.", file=out)
    if not financial:
        print("None.", file=out)
    for row, f in financial:
        where = ", ".join(v for v in (row.get("property"), row.get("unit")) if clean(v))
        print(f"\n{row['request_id']} | {where or '(missing)'} | {clean(row.get('issue')) or '(missing)'}", file=out)
        print(f"  Reason: {f.detail}", file=out)
        print(f"  Next action: {f.action}", file=out)


def write_flags_csv(queue, path, categories, financial=()):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["request_id", "property", "unit", "issue", "category", "priority", "status",
                         "severity", "rule", "reason", "recommended_action"])
        for row, flags in queue:
            category = category_label(classify(clean(row.get("issue")), categories))
            for f in flags:
                writer.writerow([row["request_id"], row.get("property", ""), row.get("unit", ""),
                                 row.get("issue", ""), category, row.get("priority", ""), row.get("status", ""),
                                 f.severity, f.rule, f.detail, f.action])
        for row, f in financial:
            writer.writerow([row["request_id"], row.get("property", ""), row.get("unit", ""),
                             row.get("issue", ""), "", row.get("priority", ""), row.get("status", ""),
                             f.severity, f.rule, f.detail, f.action])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_path", nargs="?", default="maintenance_requests.csv")
    parser.add_argument("--as-of", help="evaluate as of this time (YYYY-MM-DD HH:MM); default is now")
    parser.add_argument("--output", help="also write one row per flag to this CSV file")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="threshold settings file (default: monitor_config.toml)")
    parser.add_argument("--categories", default=None,
                        help="issue categories file (default: issue_categories.toml)")
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
    financial = build_financial_review(rows, config)
    print_report(rows, queue, now, categories, financial)
    if args.output:
        write_flags_csv(queue, args.output, categories, financial)


if __name__ == "__main__":
    main()
