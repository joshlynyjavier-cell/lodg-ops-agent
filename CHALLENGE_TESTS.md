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

## Results (run after the expectations above were committed)

Five of 8 flagged: 2 critical, 1 high, 1 medium, 1 low. Every prediction matched the actual output, but two cases don't meet the correct behavior.

| Case | Matches correct behavior? | Actual |
|---|---|---|
| CH-01 | **No** | Categorized as General maintenance. Critical dispatch and response flags fired (Emergency respected), but no escalation procedure, a false over-prioritization mismatch, and actions say "Assign maintenance staff" for water coming through a light fixture. |
| CH-02 | Yes | Not flagged; categorized as flooding. |
| CH-03 | Yes | Critical `NO_RESPONSE` naming HandyPro; medium mismatch; routine action withheld. |
| CH-04 | **No** | Categorized as plumbing and **not flagged at all**. A possible gas leak labeled Low produced no output. |
| CH-05 | Yes | `NEEDS_HUMAN_REVIEW` only. |
| CH-06 | Yes (design gap) | Not flagged. The unapproved $14,500 cost on completed work is never surfaced. |
| CH-07 | Yes | High `MISSING_INFO` (property, "urgent", MM/DD/YYYY date), unit missing, needs review. |
| CH-08 | Yes | Hold paused the time limit; low `RESIDENT_UPDATE_OVERDUE` only. The appliance standard action includes a refrigerator sentence that doesn't apply to a dishwasher (not predicted). |

## Final V1 revision and rerun

Changes made in response to the results above:

1. **Hazard screening before routine classification.** Danger signals (smell, odor, smoke, burning, sparking, pouring water, water through the ceiling, sagging, collapse and similar) stop a routine keyword from deciding the category. Without a confirmed safety category, the request goes to human review, at critical severity when labeled below High. An Emergency label is never lowered or challenged.
2. **Plural forms** match ("rotten egg" matches "rotten eggs"). Keyword classification is documented as a V1 limitation.
3. **Financial review** of completed work over $1,000 without recorded approval, in a separate report section outside the maintenance queue.
4. **Refrigerator guidance** moved to its own Refrigerator category, so the generic appliance action no longer mentions refrigerators.

All eight cases are now automated tests (`ChallengeCaseTests` in `test_exception_monitor.py`).

| Case | Now meets correct behavior? | Actual after revision |
|---|---|---|
| CH-01 | **Yes** (was No) | Possible hazard ('pouring', 'through ceiling'): high `NEEDS_HUMAN_REVIEW`; critical dispatch and response flags; no false mismatch; routine action not shown; dispatch step is "have a supervisor identify the possible hazard now and dispatch the right vendor". |
| CH-02 | Yes | Not flagged. |
| CH-03 | Yes | Unchanged. |
| CH-04 | **Yes** (was No) | Gas category via "rotten eggs"; critical `PRIORITY_MISMATCH`; gas escalation procedure; ranked with emergencies. |
| CH-05 | Yes | Unchanged. |
| CH-06 | **Yes** (gap closed) | Not in the maintenance queue; listed under Financial review ("approval recorded as 'No'"). |
| CH-07 | Yes | Unchanged. |
| CH-08 | Yes | Unchanged, and the appliance action no longer mentions refrigerators. |

No challenge case fails. The full suite (85 tests) passes, and the sample-data report is unchanged apart from MR-1006 now being categorized as Refrigerator and an empty Financial review section.
