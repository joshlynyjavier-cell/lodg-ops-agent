# Maintenance exception monitor (prototype)

Flags maintenance requests that need a person's attention and ranks them by severity.
It only reads the data: it never changes a priority, cost or any other field.

```
python3 exception_monitor.py maintenance_requests.csv --as-of "2026-09-30 09:00"
python3 exception_monitor.py --output exceptions.csv   # also write one row per flag
python3 -m unittest test_exception_monitor
```

`--as-of` defaults to the current time. The sample data is built around 2026-09-30 09:00.

## Rules

Any status other than `Completed` is unresolved. Completed requests are never flagged.

| Rule | Fires when | Severity |
|---|---|---|
| `NO_CONFIRMED_DISPATCH` | Emergency (after 15 min) or High (after 4 h) has no vendor, or the vendor hasn't confirmed | critical / high |
| `OVER_TIME_LIMIT` | Unresolved longer than Emergency 1 h, High 24 h, Medium 48 h, Low 7 d | by priority, +1 level at 4× the limit |
| `RESIDENT_UPDATE_OVERDUE` | No resident update within Emergency 2 h, High 24 h, Medium 48 h, Low 7 d. Never updated counts from creation. Skipped when `reported_by` is Staff | high → low |
| `HIGH_COST_REVIEW` | Estimate > $1,000 and `cost_approved` isn't Yes. Never holds up emergency work | medium |
| `PRIORITY_MISMATCH` | Issue text suggests urgent (gas, carbon monoxide, sparking…) but priority is Medium/Low/missing, or routine (lightbulb, paint…) but High/Emergency. Flagged for review only | critical / medium |
| `MISSING_INFO` | Blank, placeholder (TBD, N/A…) or invalid issue, priority, created_at, status or property (high, since other rules can't run); missing unit; no cost estimate after 24 h | high → low |
| `INVALID_HOLD` / `HOLD_EXPIRED` | On Hold without `hold_reason` and `hold_until`, past its end date, or an Emergency on hold | medium / high |

The time limit pauses only for a valid hold, or for a future `scheduled_date` on a Medium or Low request.
All thresholds are constants at the top of `exception_monitor.py`.
