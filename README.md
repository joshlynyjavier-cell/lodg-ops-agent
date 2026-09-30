# Maintenance exception monitor (prototype)

Flags maintenance requests that need a person's attention and ranks them by severity.
It only reads the data: it never changes a priority, cost or any other field.

```
python3 exception_monitor.py maintenance_requests.csv --as-of "2026-09-30 09:00"
python3 exception_monitor.py --output exceptions.csv        # also write one row per flag
python3 exception_monitor.py --config my_thresholds.toml     # use different thresholds
python3 -m unittest test_exception_monitor
```

`--as-of` defaults to the current time. The sample data is built around 2026-09-30 09:00.

## Changing thresholds

Every threshold lives in [`monitor_config.toml`](monitor_config.toml), with a comment explaining each one.
Operations can edit values there and rerun the monitor without touching the code.
All settings are required: a missing, misspelled or non-positive setting stops the monitor with an error naming it, rather than silently falling back to a default.

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
| `PRIORITY_MISMATCH` | Issue text suggests urgent (gas, carbon monoxide, sparking…) but priority is Medium/Low/missing, or routine (lightbulb, paint…) but High/Emergency | critical / medium |
| `MISSING_INFO` | Blank, placeholder (TBD, N/A…) or invalid issue, priority, created_at, status or property (high, since other rules can't run); missing unit; invalid `last_progress_at`; no cost estimate after 24 h | high → low |
| `INVALID_HOLD` / `HOLD_EXPIRED` | On Hold without `hold_reason` and `hold_until`, past its end date, or an Emergency on hold | medium / high |

### Emergencies: response vs. resolution

An untouched emergency and one with a confirmed vendor actively working are different problems, so emergencies are checked in stages rather than against one resolution limit.
Dispatch comes first: is a vendor confirmed? Then response: has any progress been recorded? Then active work: is progress still recent? Only when all of those are healthy does a long-running emergency show up, as a medium-severity item for review.
Progress comes from `last_progress_at`: the latest time real work happened (vendor on site, diagnosis, parts ordered, repair attempt).

### Priority mismatches

Mismatches are flagged for human review only. The assigned priority still drives every other rule, so an Emergency is never downgraded automatically: a lightbulb labeled Emergency is still checked as an Emergency, with the mismatch flagged beside it.
For ordering the list only, an issue that looks urgent but is labeled lower ranks with emergencies.

V1 matches keywords (regular expressions in `exception_monitor.py`). It is simple and predictable, but misses wording it doesn't know.

## Future improvements

- **AI-based issue classification.** Replace the keyword lists with a model that reads the issue description and suggests a priority with a short reason. Suggestions would still only be flagged for review, never applied automatically.
- **Progress-based limits for all priorities.** Use `last_progress_at` for High, Medium and Low requests too, not just emergencies.
- **Duplicate and repeat-problem detection.** Deliberately not implemented yet.
