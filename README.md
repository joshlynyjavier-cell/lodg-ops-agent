# Maintenance exception monitor (prototype)

Flags maintenance requests that need a person's attention and ranks them by severity.

**What the prototype does not do.** It makes no external calls and takes no operational action. It never contacts emergency services, vendors or residents, never assigns vendors or approves costs, and never changes a priority or any other field. It only reads the CSV and prints a report (plus an optional CSV of the flags) for a human operator. A test fails if the code imports networking, email or process-launching modules.

> **The escalation procedures in [`issue_categories.toml`](issue_categories.toml) are prototype examples only.**
> They must be replaced with company-approved procedures, contacts and jurisdiction-specific requirements before any real-world use.

```
python3 exception_monitor.py maintenance_requests.csv --as-of "2026-09-30 09:00"
python3 exception_monitor.py --output exceptions.csv            # also write one row per flag
python3 exception_monitor.py --config my_thresholds.toml         # use different thresholds
python3 exception_monitor.py --categories my_categories.toml     # use different categories
python3 -m unittest test_exception_monitor
```

`--as-of` defaults to the current time. The sample data is built around 2026-09-30 09:00.

## Output

For every flagged request, the report shows the request ID, property, issue, current priority and issue category, then the category's escalation procedure or standard action, then each reason it was flagged with a recommended next action. Requests are ranked most urgent first.

```
[CRITICAL] MR-1023
  Property: Pine Ridge Townhomes, 31
  Issue:    Carbon monoxide alarm going off
  Priority: Low
  Category: Gas / suspected gas leak or carbon monoxide (safety-critical)
  Escalation procedure (PROTOTYPE EXAMPLE - not approved for real-world use):
    - Confirm the resident has left the unit and was told not to use flames, light switches or electrical devices.
    - Confirm 911 and the gas utility's emergency line have been called. If not, the operator calls them.
    ...
  1. Reason (critical): Issue looks like Gas / suspected gas leak or carbon monoxide (safety-critical, expected at least Emergency) but priority is Low.
     Next action: Have a supervisor review the priority now. The monitor has not changed it.
```

`--output exceptions.csv` writes the same information as one row per reason, for use in a spreadsheet.

## Configuration

Two files, both separate from the code:

- [`monitor_config.toml`](monitor_config.toml): every time and cost threshold.
- [`issue_categories.toml`](issue_categories.toml): issue categories, their keywords, expected priority ranges, vendor types, escalation procedures and standard actions.

Operations can edit either file and rerun the monitor without touching the code. Both are validated on load: a missing, misspelled or invalid setting stops the monitor with an error naming it, rather than silently falling back to a default.

## Issue categories

Categories are matched with fixed keywords (whole words, ignoring capitals); no AI model is involved. Every recommended step comes from the category file, so it can be reviewed in advance.

| Handling | Categories | What the report shows |
|---|---|---|
| Safety-critical | gas / carbon monoxide, fire / smoke, flooding / major water leak, electrical hazard, elevator, structural damage / fall hazard | A predefined escalation procedure (prototype example). Actions on the request's flags start with "Follow the escalation procedure above." |
| Human review | mold / moisture | A `NEEDS_HUMAN_REVIEW` flag with review guidance, because severity depends on context the data doesn't have |
| Routine | HVAC, plumbing, appliance, access / lock, general maintenance | A standard action, and vendor-specific wording in dispatch actions |

How a category is chosen: any safety-critical match wins (two or more show all procedures plus a high-severity review flag); otherwise a human-review match; otherwise exactly one routine match. If nothing matches, or several routine categories match, the request gets a `NEEDS_HUMAN_REVIEW` flag instead of a guess. Pest issues such as cockroaches deliberately have no category in V1 and go to human review.

## Rules

Statuses listed as resolved in the config (default: `Completed`) are never flagged. Every other status is unresolved.
Values in the table are the current defaults.

| Rule | Fires when | Severity |
|---|---|---|
| `NO_CONFIRMED_DISPATCH` | Emergency (after 15 min) or High (after 4 h) has no vendor, or the vendor hasn't confirmed | critical / high |
| `NO_RESPONSE` | Emergency with no progress recorded 1 h after the request | critical |
| `STALLED_PROGRESS` | Emergency with progress recorded, but none in the last 4 h | high |
| `LONG_RUNNING_EMERGENCY` | Emergency with active work, still unresolved after 24 h. For review, not escalation | medium |
| `OVER_TIME_LIMIT` | High, Medium or Low request unresolved longer than 24 h / 48 h / 7 d | by priority, +1 level at 4× the limit |
| `RESIDENT_UPDATE_OVERDUE` | No resident update within Emergency 2 h, High 24 h, Medium 48 h, Low 7 d. Never updated counts from creation. Skipped when `reported_by` is Staff | high → low |
| `HIGH_COST_REVIEW` | Estimate > $1,000 and `cost_approved` isn't Yes. Never holds up emergency work | medium |
| `PRIORITY_MISMATCH` | Priority is below a safety-critical category's minimum, or above a routine category's maximum | critical / medium |
| `NEEDS_HUMAN_REVIEW` | Issue is uncategorized, ambiguous, mold / moisture, or matches two safety-critical categories | medium / high |
| `MISSING_INFO` | Blank, placeholder (TBD, N/A…) or invalid issue, priority, created_at, status or property (high, since other rules can't run); missing unit; invalid `last_progress_at`; no cost estimate after 24 h | high → low |
| `INVALID_HOLD` / `HOLD_EXPIRED` | On Hold without `hold_reason` and `hold_until`, past its end date, or an Emergency on hold | medium / high |

### Emergencies: response vs. resolution

An untouched emergency and one with a confirmed vendor actively working are different problems, so emergencies are checked in stages rather than against one resolution limit.
Dispatch comes first: is a vendor confirmed? Then response: has any progress been recorded? Then active work: is progress still recent? Only when all of those are healthy does a long-running emergency show up, as a medium-severity item for review.
Progress comes from `last_progress_at`: the latest time real work happened (vendor on site, diagnosis, parts ordered, repair attempt).

### Priority mismatches

Mismatches are flagged for human review only. The assigned priority still drives every other rule, so an Emergency is never downgraded automatically: a lightbulb labeled Emergency is still checked as an Emergency, with the mismatch flagged beside it.
For ordering the list only, a safety-critical issue labeled below its minimum ranks with emergencies.

## Future improvements

- **AI-assisted categorization.** A model could suggest a category for issues the keywords miss, for a person to confirm. It would never choose or write safety procedures: those stay predefined and company-approved.
- **Progress-based limits for all priorities.** Use `last_progress_at` for High, Medium and Low requests too, not just emergencies.
- **Duplicate and repeat-problem detection.** Deliberately not implemented yet.
