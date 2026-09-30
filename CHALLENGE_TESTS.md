# V1 challenge tests

Eight cases in `challenge_cases.csv`, evaluated as of 2026-09-30 09:00:

```
python3 exception_monitor.py challenge_cases.csv --as-of "2026-09-30 09:00"
```

Expectations were written and committed before the cases were run.
"Correct" is what the agreed rules should produce. "Prediction" is what I expect the current code to do; where the two differ, I expect a discrepancy.

| Case | Scenario | Correct behavior | Prediction |
|---|---|---|---|
| CH-01 | Emergency, no vendor: "Water pouring through ceiling light fixture" (1.5 h old) | Safety-critical (water plus electrical). Critical `NO_CONFIRMED_DISPATCH` and `NO_RESPONSE` with the escalation procedure. No priority mismatch. | **Discrepancy likely.** No flooding keyword matches ("pouring" and "through ceiling" aren't listed), but "light fixture" matches general maintenance. Expect it to be categorized as routine and wrongly flagged as over-prioritized, with no escalation procedure shown. The dispatch and response flags should still fire because they use the assigned Emergency priority. |
| CH-02 | Emergency, vendor confirmed and actively working: "Sewage backing up into bathtub" (6 h old, progress 45 min ago, resident updated 40 min ago) | Not flagged. | Not flagged. Categorized as flooding (safety-critical) through "sewage"; "tub" won't match inside "bathtub". |
| CH-03 | Routine issue labeled Emergency: "Kitchen cabinet door hinge loose", vendor confirmed, no progress in 2 h | Respect Emergency: critical `NO_RESPONSE` naming HandyPro. Medium `PRIORITY_MISMATCH`. Routine standard action withheld. | Same as correct. |
| CH-04 | Dangerous issue labeled Low: "Resident smells something like rotten eggs near water heater" (1 h old, no vendor) | Gas category, critical `PRIORITY_MISMATCH`, gas escalation procedure, ranked with emergencies. | **Discrepancy likely.** The keyword is "rotten egg" and whole-word matching won't match "rotten eggs"; "smell gas" doesn't match "smells something". Expect it to be categorized as plumbing through "water heater" and **not flagged at all**. |
| CH-05 | Unfamiliar issue: "Bees nest above unit entrance" | `NEEDS_HUMAN_REVIEW` (medium) only. Nothing is overdue yet. | Same as correct. |
| CH-06 | Resolved high-cost issue: completed $14,500 boiler replacement, cost never approved | Not flagged. Resolved requests are skipped by design. | Not flagged. **Design gap to note:** an unapproved $14,500 spend on completed work is invisible to the monitor. |
| CH-07 | Missing critical data: no property or unit, priority "urgent", date in MM/DD/YYYY format, vendor "TBD" | High `MISSING_INFO` naming property, invalid priority "urgent" and invalid created_at. Medium `MISSING_INFO` for unit. `NEEDS_HUMAN_REVIEW` because "Ceiling fan" matches no category. No time-based flags. | Same as correct. Note: a US-format date is common in real exports, and it's treated as invalid rather than parsed. |
| CH-08 | Overdue with a valid hold: Medium dishwasher repair open 10 days, on hold for a part until Oct 6, resident last updated 4 days ago | The hold pauses the time limit, so no `OVER_TIME_LIMIT`. The hold doesn't pause resident communication, so low `RESIDENT_UPDATE_OVERDUE`. | Same as correct. |
